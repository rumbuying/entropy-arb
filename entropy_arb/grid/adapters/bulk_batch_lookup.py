"""One fresh before/history/after packet set shared by exact-original reads.

Only request-local raw response reuse. No retained coverage, missing-order
proof, global cache or terminal inference is introduced.
"""
import asyncio,copy,json

from .base import AdapterError
from .bulk_read import BulkReply
from .bulk_wire import uint

MAX_ORIGINALS=256
MAX_BYTES=16*1024*1024


class OrderPacketSet:
    def __init__(self,reader):
        self.reader=reader;self.before=None;self.after=None;self.history={};self.bytes=0

    async def fetch(self,kind,params):
        reply=await self.reader.transport.account(kind,self.reader.owner,**params)
        if not isinstance(reply,BulkReply):raise AdapterError('BULK_BATCH_REPLY_INVALID')
        try:size=len(json.dumps(reply.data,allow_nan=False,separators=(',',':')).encode())
        except (TypeError,ValueError):raise AdapterError('BULK_BATCH_REPLY_INVALID') from None
        if size>MAX_BYTES or self.bytes+size>MAX_BYTES:raise AdapterError('BULK_BATCH_SNAPSHOT_LIMIT')
        self.bytes+=size
        return copy.deepcopy(reply)

    def for_original(self):
        packets=self
        class Replay:
            def __init__(self):self.open_reads=0
            async def account(self,kind,owner,**params):
                if owner!=packets.reader.owner or kind not in ('openOrders','orderHistory'):
                    raise AdapterError('BULK_BATCH_QUERY_FORBIDDEN')
                if kind=='openOrders':
                    if params or self.open_reads>=2:raise AdapterError('BULK_BATCH_QUERY_FORBIDDEN')
                    attr='before' if self.open_reads==0 else 'after';self.open_reads+=1
                    if getattr(packets,attr) is None:setattr(packets,attr,await packets.fetch(kind,{}))
                    return copy.deepcopy(getattr(packets,attr))
                key=json.dumps(params,sort_keys=True,separators=(',',':'))
                if key not in packets.history:
                    # Every original uses the same start and native opaque
                    # cursor chain. Existing lookup validation proves anchors,
                    # row ordering, identity and the bounded page budget.
                    packets.history[key]=await packets.fetch(kind,params)
                return copy.deepcopy(packets.history[key])
        return Replay()


async def lookup_original_batch(reader,originals,*,start_slot):
    """Return positive/unknown per-original results from one packet set.

    Each original still traverses the exact existing semantic reader. The
    two open-order reads are distinct network observations, not one cached
    response passed off as a later snapshot.
    """
    from .bulk_bound_read import BulkBoundEvidenceReader
    if not isinstance(reader,BulkBoundEvidenceReader):raise AdapterError('BULK_BATCH_BOUND_READER_REQUIRED')
    uint(start_slot)
    if not isinstance(originals,(tuple,list)) or not 1<=len(originals)<=MAX_ORIGINALS:
        raise AdapterError('BULK_BATCH_SCOPE_LIMIT')
    requested=[];cids=set();oids=set()
    for pair in originals:
        if not isinstance(pair,(tuple,list)) or len(pair)!=2:raise AdapterError('BULK_BATCH_SCOPE_CONFLICT')
        cid,oid=pair
        if (not isinstance(cid,str) or not isinstance(oid,str) or cid in cids or oid in oids
                or reader.bindings.get(oid)!=cid
                or sum(v==cid for v in reader.bindings.values())!=1):
            raise AdapterError('BULK_BATCH_SCOPE_CONFLICT')
        reader._bound(oid);cids.add(cid);oids.add(oid);requested.append((cid,oid))
    packets=OrderPacketSet(reader);proofs={}
    async with asyncio.timeout(90):
        for cid,oid in requested:
            focused=copy.copy(reader);focused.transport=packets.for_original()
            proofs[cid]=await focused.lookup(cid,oid,start_slot=start_slot)
            # Cached replay still yields to account protection and cancellation.
            await asyncio.sleep(0)
    return proofs

"""Positive lookup of one exact original, independent of sibling lifecycles.

All raw rows still constrain paging, scope and ordering. Only the requested
OID is semantically decoded. No absence or filtered page proves coverage.
Account-wide recovery/history readers retain their strict existing behavior.
"""
import copy
import re

from .base import AdapterError,LookupResult
from .bulk_read import BulkEvidenceReader,BulkReply
from .bulk_wire import base58,uint


class OriginalTransport:
    def __init__(self,reader,oid):
        self.reader,self.original,self.oid=reader,reader.transport,oid
        self.last=None;self.root_event=None

    def identity(self,row):
        self.reader._lane(row)
        if (not isinstance(row.get('symbol'),str)
                or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD',row['symbol'])
                or row.get('owner',self.reader.owner)!=self.reader.owner):
            raise AdapterError('BULK_POSITIVE_LOOKUP_ROW_SCOPE_CONFLICT')
        base58(row['orderId'])

    async def account(self,kind,owner,**params):
        if kind not in ('openOrders','orderHistory') or owner!=self.reader.owner:
            raise AdapterError('BULK_POSITIVE_LOOKUP_QUERY_FORBIDDEN')
        reply=await self.original.account(kind,owner,**params)
        data=self.reader._success(reply)
        if kind=='openOrders':
            if reply.status!=200 or not isinstance(data,list):raise AdapterError('BULK_OPEN_ORDERS_INVALID')
            selected=[];seen=set()
            for row in data:
                if not isinstance(row,dict) or set(row)!={'openOrder'}:raise AdapterError('BULK_OPEN_ORDERS_INVALID')
                native=row['openOrder'];self.identity(native)
                if native['orderId'] in seen:raise AdapterError('BULK_DUPLICATE_OPEN_ORDER')
                seen.add(native['orderId'])
                if native['orderId']==self.oid:selected.append(copy.deepcopy(row))
            return BulkReply(reply.status,selected)
        if not isinstance(data,dict) or not isinstance(data.get('data'),list) or not isinstance(data.get('page'),dict):
            raise AdapterError('BULK_HISTORY_COVERAGE_MISSING')
        page=data['page'];bounds=tuple(uint(page[k]) for k in ('startSlot','endSlot','asOfSlot'))
        if not bounds[0]<=bounds[1]<=bounds[2] or len(data['data'])>params.get('limit',5000):
            raise AdapterError('BULK_HISTORY_BOUNDARY_CONFLICT')
        if page.get('coverage') not in ('complete','partial','unknown'):raise AdapterError('BULK_HISTORY_COVERAGE_INVALID')
        selected=[]
        for row in data['data']:
            self.identity(row);position=self.reader._row_position(row)
            if not bounds[0]<=position[0]<=bounds[1] or self.last is not None and position>self.last:
                raise AdapterError('BULK_HISTORY_ORDER_OR_WINDOW_INVALID')
            self.last=position
            if row['orderId']==self.oid:
                selected.append(copy.deepcopy(row))
                if row.get('orderType')=='stop' and row.get('status') in ('triggered','triggerFailed'):
                    self.root_event=row['status']
        # Preserve real anchors/floors/cursors and raw global ordering checks.
        # Filtering can NEVER issue a complete account-history certificate.
        return BulkReply(reply.status,dict(data=selected,page=copy.deepcopy(page)|dict(coverage='unknown')))


async def lookup_original(reader,client_id,exchange_order_id,*,start_slot):
    matches=[oid for oid,cid in reader.bindings.items() if cid==client_id]
    if len(matches)!=1 or exchange_order_id is not None and matches[0]!=exchange_order_id:
        raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
    oid=matches[0];reader._bound(oid)
    focused=copy.copy(reader);transport=OriginalTransport(reader,oid);focused.transport=transport
    try:
        return await BulkEvidenceReader.lookup(focused,client_id,oid,start_slot=start_slot)
    except AdapterError as error:
        if str(error)=='BULK_ORDER_TERMINAL_UNVERIFIED' and transport.root_event:
            # A matched conditional event proves no child execution quantity.
            # Keep the original pending, including when the account is flat.
            return LookupResult('NOT_FOUND_TRANSIENT',oid,detail='conditional_root_execution_unverified',
                cumulative_known=False,native_status=transport.root_event)
        raise

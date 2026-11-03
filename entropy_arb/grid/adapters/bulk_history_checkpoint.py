"""Durable, scope-bound staging for a Bulk history scan.

This stores native pages only; it never marks them complete or advances a
ledger cursor. A caller must replay every page through the evidence reader.
"""
import json
import uuid

from .. import models
from .base import AdapterError
from .bulk_wire import uint

MAX_PAGES = 100
MAX_BYTES = 16 * 1024 * 1024


class BulkHistoryCheckpoint:
    def __init__(self, store, *, account, owner, market, kind, start_slot, end_slot):
        if kind not in ('fills', 'fundingHistory', 'orderHistory'):
            raise AdapterError('BULK_HISTORY_KIND_INVALID')
        uint(start_slot)
        if end_slot is not None: uint(end_slot)
        if end_slot is not None and end_slot < start_slot:
            raise AdapterError('BULK_HISTORY_WINDOW_INVALID')
        self.store = store
        self.scope = dict(account=account, owner=owner, market=market, kind=kind,
                          start_slot=start_slot, end_slot=end_slot)
        self.key = 'bulk-history-scan:' + models.sha256_hex(self.scope)

    def read(self):
        def query(c):
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(self.key,)).fetchone()
            if row is None:return None
            state=json.loads(row[0])
            if state.get('scope')!=self.scope:raise AdapterError('BULK_HISTORY_CHECKPOINT_SCOPE_CONFLICT')
            return state
        return self.store.read(query)

    async def begin(self):
        def write(c):
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(self.key,)).fetchone()
            if row:
                state=json.loads(row[0])
                if state.get('scope')!=self.scope:raise AdapterError('BULK_HISTORY_CHECKPOINT_SCOPE_CONFLICT')
                return state
            state=dict(scope=self.scope, generation=uuid.uuid4().hex, version=0, pages=[], next_cursor=None)
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',(self.key,json.dumps(state,sort_keys=True)))
            return state
        return await self.store.write(write)

    async def append(self, *, generation, version, requested_cursor, response):
        """Atomically append a raw page, rejecting stale or changed writers."""
        if not isinstance(response, dict) or not isinstance(response.get('data'), list) or not isinstance(response.get('page'),dict):
            raise AdapterError('BULK_HISTORY_CHECKPOINT_PAGE_INVALID')
        page=response['page'];following=page.get('nextCursor');more=page.get('hasMore')
        if type(more) is not bool or (more and (not isinstance(following,str) or not 1<=len(following)<=8192)) or (not more and following is not None):
            raise AdapterError('BULK_HISTORY_CHECKPOINT_CURSOR_INVALID')
        if len(response['data']) > 5000:raise AdapterError('BULK_HISTORY_CHECKPOINT_PAGE_INVALID')
        try:
            bounds=[uint(page[k]) for k in ('startSlot','endSlot','asOfSlot')]
            floor=page.get('minAvailableSlot')
            if floor is not None:uint(floor)
            if (bounds[0]!=self.scope['start_slot'] or not bounds[0]<=bounds[1]<=bounds[2]
                    or self.scope['end_slot'] is not None and bounds[1]!=self.scope['end_slot']
                    or page.get('coverage') not in ('complete','partial','unknown')):
                raise AdapterError('BULK_HISTORY_BOUNDARY_CONFLICT')
            encoded=json.dumps(response,sort_keys=True,allow_nan=False,separators=(',',':')).encode()
        except (KeyError,TypeError,ValueError) as error:
            raise AdapterError('BULK_HISTORY_CHECKPOINT_PAGE_INVALID') from error
        if len(encoded)>MAX_BYTES:raise AdapterError('BULK_HISTORY_CHECKPOINT_LIMIT')
        anchor=bounds+[floor]
        def write(c):
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(self.key,)).fetchone()
            if row is None:raise AdapterError('BULK_HISTORY_CHECKPOINT_MISSING')
            state=json.loads(row[0])
            if (state.get('scope')!=self.scope or state.get('generation')!=generation
                    or state.get('version')!=version or state.get('next_cursor')!=requested_cursor
                    or state.get('finished')):
                raise AdapterError('BULK_HISTORY_CHECKPOINT_STALE')
            if following is not None and (following==requested_cursor or following in state.get('seen_cursors', [])):
                raise AdapterError('BULK_HISTORY_CURSOR_STALLED')
            if state.get('anchor') is not None and state['anchor']!=anchor:
                raise AdapterError('BULK_HISTORY_SNAPSHOT_CHANGED')
            if len(state['pages'])>=MAX_PAGES or state.get('bytes',0)+len(encoded)>MAX_BYTES:
                raise AdapterError('BULK_HISTORY_CHECKPOINT_LIMIT')
            state['anchor']=anchor
            state['bytes']=state.get('bytes',0)+len(encoded)
            state['pages'].append(response)
            state['next_cursor']=following
            state.setdefault('seen_cursors',[])
            if following is not None:state['seen_cursors'].append(following)
            state['finished']=not more
            state['version']+=1
            c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(state,sort_keys=True),self.key))
            return state
        return await self.store.write(write)

    async def discard(self, *, generation, version):
        def write(c):
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(self.key,)).fetchone()
            if row is None:return False
            state=json.loads(row[0])
            if state.get('scope')!=self.scope or state.get('generation')!=generation or state.get('version')!=version:
                raise AdapterError('BULK_HISTORY_CHECKPOINT_STALE')
            c.execute('DELETE FROM runtime_state WHERE key=?',(self.key,))
            return True
        return await self.store.write(write)

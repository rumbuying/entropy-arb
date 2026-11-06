"""Bulk native account/history reads with explicit slot coverage evidence.

Separate from the legacy float adapter and from the signed /order endpoint.
Account reads are public: they never prove possession or authority of a key.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import re
import time

import aiohttp

from .. import models
from .base import AdapterError, ExchangeOrder, FundingPage, FundingRecord, HistoryPage, LookupResult
from .bulk_wire import base58, uint, native_decimal, text
from .observation import ReadTransport, market_row

SOURCE = 'bulk-account-slots-v1:b6db6ade99effbfc'
HISTORY_TYPES = frozenset(('fills', 'fundingHistory', 'orderHistory'))
READ_HISTORY_TYPES = HISTORY_TYPES | {'positions'}
REJECTED = frozenset(('rejectedInvalid', 'rejectedRiskLimit', 'rejectedCrossing', 'rejectedDuplicate'))
CANCELED = frozenset(('cancelled', 'cancelledRiskLimit', 'cancelledSelfCrossing', 'cancelledReduceOnly', 'cancelledIoc'))
OPEN = frozenset(('placed', 'resting', 'working', 'modified'))


def flag(value):
    if type(value) is not bool:
        raise AdapterError('BULK_BOOLEAN_INVALID')
    return value


def ns_iso(value):
    uint(value)
    # No float seconds. Stable IDs retain full ns/slot/sequence separately;
    # the shared ledger's ISO representation has microsecond precision.
    return models.format_iso_ts(datetime(1970, 1, 1, tzinfo=timezone.utc)+timedelta(microseconds=value//1000))


@dataclass(frozen=True)
class BulkReply:
    status: int
    data: object


class BulkReadTransport(ReadTransport):
    """Public GET plus strictly typed *unsigned read-only* POST /account."""
    # Recent negative read evidence gates activation: a start/resume refused
    # while reads are failing cannot burn an epoch mid-flap. Absence of any
    # read (fresh process) is healthy — the gate refuses only on observed
    # failures, so it can never deadlock a worker that has not read yet.
    READ_HEALTH_WINDOW_S = 60

    def __init__(self, session, network):
        super().__init__(session, 'bulk', network)
        self.last_fail_mono = None

    def read_health(self, *, now_mono):
        """True unless this transport observed a failed read recently.

        A live 429 backoff also counts as unhealthy. Successes are not
        tracked: only failure recency is negative evidence."""
        if now_mono < self.next_read:
            return False
        return (self.last_fail_mono is None
            or now_mono-self.last_fail_mono > self.READ_HEALTH_WINDOW_S)

    async def account(self, kind, owner, *, limit=None, start_slot=None, end_slot=None, cursor=None, symbol=None):
        if kind not in READ_HISTORY_TYPES | {'fullAccount', 'openOrders', 'feeTier'}:
            raise AdapterError('BULK_ACCOUNT_QUERY_FORBIDDEN')
        base58(owner)
        payload = dict(type=kind, user=owner)
        if symbol is not None:
            if (kind != 'feeTier' or not isinstance(symbol, str)
                    or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', symbol)):
                raise AdapterError('BULK_ACCOUNT_QUERY_FORBIDDEN')
            payload['symbol'] = symbol
        if kind in READ_HISTORY_TYPES:
            if limit is not None:
                uint(limit, 5000)
                if limit == 0: raise AdapterError('BULK_HISTORY_LIMIT_INVALID')
                payload['limit'] = limit
            if cursor is not None:
                if start_slot is not None or end_slot is not None or not isinstance(cursor, str) or not 1 <= len(cursor) <= 8192:
                    raise AdapterError('BULK_HISTORY_CURSOR_INVALID')
                payload['cursor'] = cursor
            else:
                if start_slot is not None: payload['startSlot'] = uint(start_slot)
                if end_slot is not None: payload['endSlot'] = uint(end_slot)
                if start_slot is not None and end_slot is not None and start_slot > end_slot:
                    raise AdapterError('BULK_HISTORY_WINDOW_INVALID')
        elif any(v is not None for v in (limit, start_slot, end_slot, cursor)):
            raise AdapterError('BULK_ACCOUNT_QUERY_FORBIDDEN')
        async with self.lock:
            await asyncio.sleep(max(0, self.next_read-time.monotonic()))
            self.next_read = time.monotonic()+1
            try:
                async with self.session.post(self.origin+'/api/v1/account', json=payload,
                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 429:
                        now = time.monotonic()
                        self.next_read = now+60
                        self.last_fail_mono = now
                        raise AdapterError('BULK_READ_RATE_LIMITED')
                    if response.status not in (200, 206, 400, 404, 503):
                        self.last_fail_mono = time.monotonic()
                        raise AdapterError('BULK_READ_HTTP_FAILED')
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            self.last_fail_mono = time.monotonic()
                            raise AdapterError('BULK_READ_TOO_LARGE')
                    def object_pairs(pairs):
                        result = {}
                        for k, v in pairs:
                            if k in result: raise ValueError()
                            result[k] = v
                        return result
                    data = json.loads(raw, parse_float=str, object_pairs_hook=object_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    return BulkReply(response.status, data)
            except asyncio.CancelledError:
                raise
            except AdapterError:
                raise
            except Exception:
                self.last_fail_mono = time.monotonic()
                raise AdapterError('BULK_READ_UNCERTAIN') from None


@dataclass(frozen=True)
class BulkHistoryWindow:
    records: tuple
    start_slot: int
    end_slot: int
    as_of_slot: int
    min_available_slot: object
    complete: bool
    pages: int
    reason: str


# Certification-failure reasons ordered strictest-first. The ledger layer may
# provisionally accept only COVERAGE_UNCERTIFIED/FLOOR_UNKNOWN (the venue
# declined to certify but returned a bounded, fully paged window). Declared
# gaps — partial coverage or a floor above the requested start — stay fatal.
_CERTIFICATION_PRECEDENCE = (
    'BULK_HISTORY_HTTP_FAILED', 'BULK_HISTORY_PARTIAL_COVERAGE', 'BULK_HISTORY_FLOOR_GAP',
    'BULK_HISTORY_COVERAGE_UNCERTIFIED', 'BULK_HISTORY_FLOOR_UNKNOWN')
_UNCERTIFIED_ACCEPTABLE = frozenset(('BULK_HISTORY_COVERAGE_UNCERTIFIED',
                                     'BULK_HISTORY_FLOOR_UNKNOWN'))


def _stricter_reason(current, candidate):
    order = {name: index for index, name in enumerate(_CERTIFICATION_PRECEDENCE)}
    if not current:
        return candidate
    return min(current, candidate, key=lambda name: order.get(name, len(order)))


class BulkEvidenceReader:
    def __init__(self, transport, *, account, owner, market_ref, client_bindings=None):
        base58(owner)
        parts = market_ref.split('|')
        if (len(parts) != 5 or parts[0] not in ('bulk:mainnet', 'bulk:testnet')
                or parts[1] or parts[3:] != ['linear_perp', 'USDC']
                or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', parts[2])):
            raise AdapterError('BULK_MARKET_SCOPE_INVALID')
        if isinstance(transport, BulkReadTransport):
            from .observation import ORIGINS
            if transport.origin != ORIGINS['bulk', parts[0].split(':')[1]]:
                raise AdapterError('BULK_DEPLOYMENT_MISMATCH')
        self.transport, self.account, self.owner = transport, account, owner
        self.market_ref, self.symbol = market_ref, parts[2]
        self.bindings = dict(client_bindings or {})  # native OID -> original CID
        for oid, cid in self.bindings.items():
            base58(oid)
            if not isinstance(cid, str) or not re.fullmatch('[a-zA-Z0-9:_-]{1,128}', cid):
                raise AdapterError('BULK_CLIENT_BINDING_INVALID')
        if len(set(self.bindings.values())) != len(self.bindings):
            raise AdapterError('BULK_CLIENT_BINDING_CONFLICT')
        self.scope = models.sha256_hex([account, owner, market_ref])

    def cursor(self, slot):
        return f'bulk-slot:{self.scope}:{uint(slot)}'

    def funding_cursor_rank(self, cursor):
        prefix = 'bulk-slot:'+self.scope+':'
        if not isinstance(cursor, str) or not cursor.startswith(prefix) or not re.fullmatch('0|[1-9][0-9]{0,19}', cursor[len(prefix):]):
            raise AdapterError('BULK_CURSOR_SCOPE_INVALID')
        return uint(int(cursor[len(prefix):]))

    def _lane(self, row):
        if not isinstance(row, dict) or flag(row['iso']):
            # Parent responses may aggregate attached isolated accounts.
            # Do not charge those rows to the parent's cross-margin ledger.
            raise AdapterError('BULK_ISOLATED_SCOPE_UNSUPPORTED')
        if row.get('isoPubkey') not in (None, ''):
            raise AdapterError('BULK_ISOLATED_SCOPE_CONFLICT')

    @staticmethod
    def _success(reply):
        if not isinstance(reply, BulkReply): raise AdapterError('BULK_REPLY_INVALID')
        if isinstance(reply.data, dict) and 'error' in reply.data:
            # Unknown account is *not* a zero-position/empty-history proof.
            code = reply.data['error'].get('code') if isinstance(reply.data['error'], dict) else None
            safe = code if code in {'ACCOUNT_NOT_FOUND', 'HISTORY_NOT_COVERED', 'CURSOR_EXPIRED',
                'HISTORY_STATE_NOT_READY', 'HISTORY_BUSY', 'HISTORY_STORAGE_ERROR',
                'INVALID_HISTORY_QUERY', 'CURSOR_INVALID'} else 'READ_FAILED'
            raise AdapterError('BULK_'+safe)
        if reply.status not in (200, 206): raise AdapterError('BULK_READ_HTTP_FAILED')
        return reply.data

    async def fee_quote(self):
        """Account-tier fees for this exact market; not a global schedule guess."""
        reply = await self.transport.account('feeTier', self.owner, symbol=self.symbol)
        body = self._success(reply)
        try:
            if (reply.status != 200 or not isinstance(body, list) or len(body) != 1
                    or not isinstance(body[0], dict) or set(body[0]) != {'feeTier'}):
                raise ValueError()
            row = body[0]['feeTier']
            scope=row['scopeInstrument'];scope_binding='native-explicit'
            if scope is None:
                # Current mainnet encodes the global quote as null. Require
                # matching explicit global state from the same native account;
                # null alone is never permission to use a global fee schedule.
                state=await self.transport.account('fullAccount',self.owner)
                full=self._success(state)
                if (state.status!=200 or not isinstance(full,list) or len(full)!=1
                        or set(full[0])!={'fullAccount'}):raise ValueError()
                account=full[0]['fullAccount']
                if account.get('kind')!='MasterEOA' or account.get('parent'):raise ValueError()
                tiers=account['feeTiers']
                if not isinstance(tiers,list):raise ValueError()
                matches=[r for r in tiers if r['symbol']=='global']
                if len(matches)!=1 or any(r['symbol']==self.symbol for r in tiers):raise ValueError()
                explicit=matches[0]
                for key in ('rollingVolume','tierThreshold','makerBps','takerBps'):
                    if native_decimal(explicit[key])!=native_decimal(row[key]):raise ValueError()
                for key in ('tierIndex','windowDays'):
                    if type(explicit[key]) is not int or explicit[key]!=row[key]:raise ValueError()
                scope='global';scope_binding='native-full-account-global'
            if scope not in ('global', self.symbol):
                raise ValueError()
            base_maker,taker=native_decimal(row['makerBps']),native_decimal(row['takerBps'])
            maker=native_decimal(row.get('effectiveMakerBps',row['makerBps']))
            if any(abs(v)>10000 for v in (maker,base_maker,taker)):
                raise ValueError()
            volume = native_decimal(row['rollingVolume'], negative=False)
            threshold = native_decimal(row['tierThreshold'], negative=False)
            tier, days = uint(row['tierIndex'], 2**32-1), uint(row['windowDays'], 3650)
            if not days or threshold > volume:
                raise ValueError()
            with localcontext() as ctx:
                ctx.prec = 256
                return dict(account=self.account, market=self.market_ref, scope_instrument=scope,scope_binding=scope_binding,
                    maker_fee_rate=text(maker/10000), taker_fee_rate=text(taker/10000),
                    base_maker_fee_rate=text(base_maker/10000),
                    maker_fee_reserve_rate=text(max(base_maker,maker,Decimal(0))/10000),
                    rolling_volume=text(volume), tier_threshold=text(threshold), tier_index=tier,
                    window_days=days, received_ts=models.utc_now_iso(), source='bulk:account-fee-tier')
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise AdapterError('BULK_FEE_QUOTE_UNVERIFIED') from None

    async def full_account(self):
        reply = await self.transport.account('fullAccount', self.owner)
        body = self._success(reply)
        if reply.status != 200: raise AdapterError('BULK_ACCOUNT_SNAPSHOT_INCOMPLETE')
        if not isinstance(body, list) or len(body) != 1 or set(body[0]) != {'fullAccount'}:
            raise AdapterError('BULK_ACCOUNT_RESPONSE_INVALID')
        account = body[0]['fullAccount']
        if not isinstance(account, dict) or account.get('kind') != 'MasterEOA' or account.get('parent'):
            raise AdapterError('BULK_ACCOUNT_KIND_UNVERIFIED')
        for key in ('positions', 'openOrders'):
            if not isinstance(account.get(key), list): raise AdapterError('BULK_ACCOUNT_RESPONSE_INVALID')
            for row in account[key]: self._lane(row)
        margin = account.get('margin')
        if not isinstance(margin, dict): raise AdapterError('BULK_MARGIN_INVALID')
        values = {k: text(native_decimal(margin[k])) for k in
                  ('totalMargin', 'availableMargin', 'marginUsed', 'bufferedMargin', 'transferableBalance')}
        # Report separately; maintenance availableMargin is not initial margin
        # capacity, nor automatically transferable/cross-collateral capital.
        positions = []
        seen = set()
        for row in account['positions']:
            symbol = row['symbol']
            if not isinstance(symbol, str) or symbol in seen: raise AdapterError('BULK_POSITION_SCOPE_CONFLICT')
            seen.add(symbol)
            positions.append(dict(symbol=symbol, qty_base=text(native_decimal(row['size']))))
        return dict(margin=values, positions=positions, open_order_count=len(account['openOrders']),
                    account_kind=account['kind'], identity_verified=False, authorization_verified=False)

    def _row_position(self, row):
        self._lane(row)
        return uint(row['slot']), uint(row['sequence'])

    def fill(self, row):
        slot, seq = self._row_position(row)
        if row['symbol'] != self.symbol: raise AdapterError('BULK_FILL_MARKET_MISMATCH')
        for key in ('maker', 'taker'): base58(row[key])
        maker, taker = row['maker'] == self.owner, row['taker'] == self.owner
        if maker == taker: raise AdapterError('BULK_FILL_OWNER_AMBIGUOUS')
        if type(row['reasonCode']) is not int or row['reasonCode'] != 0:
            raise AdapterError('BULK_RISK_EXECUTION_UNVERIFIED')
        # Mainnet can omit the redundant tradeId. Preserve the native
        # slot/sequence identity and reject any explicitly conflicting ID.
        tid = row.get('tradeId', f'{slot}:{seq}')
        if not isinstance(tid, str) or tid != f'{slot}:{seq}':
            raise AdapterError('BULK_TRADE_ID_CONFLICT')
        oid = row['orderIdMaker'] if maker else row['orderIdTaker']; base58(oid)
        fee = native_decimal(row['fee'])
        if fee != native_decimal(row['makerFee'] if maker else row['takerFee']):
            raise AdapterError('BULK_FILL_FEE_CONFLICT')
        qty, price = native_decimal(row['amount'], negative=False), native_decimal(row['price'], negative=False)
        if qty <= 0 or price <= 0: raise AdapterError('BULK_FILL_AMOUNT_INVALID')
        # Native fees are signed balance changes; the shared ledger uses
        # positive cost and negative rebates. Never abs() away a rebate.
        return models.ExecutionEvent(
            execution_key=models.sha256_hex(['bulk-fill', self.scope, tid]), account=self.account,
            market=self.market_ref, order_client_id=self.bindings.get(oid), exchange_order_id=oid,
            trade_id=tid, side='buy' if flag(row['isBuy']) else 'sell', qty_base=text(qty), price=text(price),
            fee_amount=text(fee.copy_negate()), fee_asset='USDC', is_maker=maker, exchange_ts=ns_iso(row['timestamp']),
            received_ts=models.utc_now_iso(), source='rest_backfill', source_seq=f'{slot}:{seq}', finality='final')

    def funding(self, row):
        slot, seq = self._row_position(row)
        if row['owner'] != self.owner or row['symbol'] != self.symbol:
            raise AdapterError('BULK_FUNDING_SCOPE_CONFLICT')
        # Native payment is positive when received; shared ledger is positive
        # when paid. Unary minus uses a large local precision, never a float.
        with localcontext() as ctx:
            ctx.prec = 256
            paid = -native_decimal(row['payment'])
        return FundingRecord(models.sha256_hex(['bulk-funding', self.scope, slot, seq]), text(paid),
                             'USDC', ns_iso(row['timestamp']), self.cursor(slot))

    def order(self, row, *, terminal=False):
        self._lane(row)
        if row['symbol'] != self.symbol: raise AdapterError('BULK_ORDER_MARKET_MISMATCH')
        oid = row['orderId']; base58(oid)
        if row['orderType'] not in ('limit', 'market') or row.get('trigger') is not None:
            raise AdapterError('BULK_ORDER_PROTECTION_UNVERIFIED')
        status = row['status']
        if status == 'rejectedDuplicate':
            raise AdapterError('BULK_DUPLICATE_REQUIRES_ORIGINAL_PROOF')
        if terminal:
            if status not in REJECTED | CANCELED | {'filled', 'partiallyFilled'}:
                raise AdapterError('BULK_ORDER_TERMINAL_UNVERIFIED')
            side = row['side']
            if side not in ('buy', 'sell'): raise AdapterError('BULK_ORDER_SIDE_INVALID')
            qty = native_decimal(row['originalSize'], negative=False)
            filled = native_decimal(row['executedSize'], negative=False)
        else:
            if status not in OPEN | {'pending'}: raise AdapterError('BULK_OPEN_ORDER_STATUS_INVALID')
            original = native_decimal(row['originalSize'])
            side = 'buy' if original > 0 else 'sell'
            qty = original.copy_abs()
            filled = native_decimal(row['filledSize']).copy_abs()
            remaining = native_decimal(row['size'])
            with localcontext() as ctx:
                ctx.prec = 256
                if remaining.copy_abs()+filled != qty or remaining != 0 and (remaining > 0) != (original > 0):
                    raise AdapterError('BULK_ORDER_QUANTITY_CONFLICT')
        if qty <= 0 or filled > qty or status == 'filled' and filled != qty:
            raise AdapterError('BULK_ORDER_QUANTITY_CONFLICT')
        if status in REJECTED and filled != 0:
            raise AdapterError('BULK_REJECTION_HAS_EXECUTIONS')
        price = native_decimal(row['price'], negative=False)
        if price <= 0: raise AdapterError('BULK_ORDER_PRICE_UNVERIFIED')
        result = ExchangeOrder(self.bindings.get(oid), oid, side, text(qty), text(price), text(filled),
                               flag(row['reduceOnly']), 'rest_query')
        return result, status

    async def open_orders(self):
        reply = await self.transport.account('openOrders', self.owner)
        data = self._success(reply)
        if reply.status != 200: raise AdapterError('BULK_OPEN_ORDERS_INCOMPLETE')
        if not isinstance(data, list): raise AdapterError('BULK_OPEN_ORDERS_INVALID')
        result = []
        seen = set()
        for row in data:
            if not isinstance(row, dict) or set(row) != {'openOrder'}:
                raise AdapterError('BULK_OPEN_ORDERS_INVALID')
            native = row['openOrder']; self._lane(native)
            if native['symbol'] != self.symbol: continue
            order, status = self.order(native)
            if order.exchange_order_id in seen: raise AdapterError('BULK_DUPLICATE_OPEN_ORDER')
            seen.add(order.exchange_order_id); result.append((order, status))
        return tuple(result)

    async def history(self, kind, *, start_slot, end_slot=None, max_pages=10, limit=500):
        if kind not in HISTORY_TYPES: raise AdapterError('BULK_HISTORY_KIND_INVALID')
        uint(start_slot); uint(max_pages, 110); uint(limit, 5000)
        if not max_pages or not limit: raise AdapterError('BULK_HISTORY_LIMIT_INVALID')
        if end_slot is not None:
            uint(end_slot)
            if end_slot < start_slot: raise AdapterError('BULK_HISTORY_WINDOW_INVALID')
        anchor, cursor, seen_cursors, rows, last, safe = None, None, set(), {}, None, True
        uncertified_reason = ''
        parser = {'fills': self.fill, 'fundingHistory': self.funding,
                  'orderHistory': lambda row: self.order(row, terminal=True)}[kind]
        for page_num in range(1, max_pages+1):
            params = dict(limit=limit, cursor=cursor) if cursor is not None else dict(limit=limit, start_slot=start_slot, end_slot=end_slot)
            reply = await self.transport.account(kind, self.owner, **params)
            data = self._success(reply)
            if not isinstance(data, dict) or not isinstance(data.get('data'), list) or len(data['data']) > limit:
                raise AdapterError('BULK_HISTORY_ROWS_INVALID')
            p = data.get('page')
            if not isinstance(p, dict): raise AdapterError('BULK_HISTORY_COVERAGE_MISSING')
            bounds = tuple(uint(p[k]) for k in ('startSlot', 'endSlot', 'asOfSlot'))
            if bounds[0] != start_slot or not bounds[0] <= bounds[1] <= bounds[2] or end_slot is not None and bounds[1] != end_slot:
                raise AdapterError('BULK_HISTORY_BOUNDARY_CONFLICT')
            floor = p.get('minAvailableSlot')
            if floor is not None: uint(floor)
            if anchor is None: anchor = (*bounds, floor)
            elif anchor != (*bounds, floor): raise AdapterError('BULK_HISTORY_SNAPSHOT_CHANGED')
            if p.get('coverage') not in ('complete', 'partial', 'unknown'):
                raise AdapterError('BULK_HISTORY_COVERAGE_INVALID')
            # Precision matters here: a venue that DECLINES to certify
            # (coverage "unknown", no published floor) is different from one
            # that DECLARES a real gap (partial coverage, or a floor above the
            # requested start). Only the former is eligible for a provisional
            # acceptance by the ledger layer; the latter must keep refusing.
            page_reason = ''
            if reply.status != 200:
                page_reason = 'BULK_HISTORY_HTTP_FAILED'
            elif p['coverage'] == 'partial':
                page_reason = 'BULK_HISTORY_PARTIAL_COVERAGE'
            elif p['coverage'] == 'unknown':
                page_reason = 'BULK_HISTORY_COVERAGE_UNCERTIFIED'
            elif floor is None:
                page_reason = 'BULK_HISTORY_FLOOR_UNKNOWN'
            elif floor > start_slot:
                page_reason = 'BULK_HISTORY_FLOOR_GAP'
            safe = safe and not page_reason
            if page_reason:
                uncertified_reason = _stricter_reason(uncertified_reason, page_reason)
            more = flag(p['hasMore']); following = p['nextCursor']
            if (more and (not isinstance(following, str) or not 1 <= len(following) <= 8192)
                    or not more and following is not None):
                raise AdapterError('BULK_HISTORY_CURSOR_INVALID')
            for raw in data['data']:
                position = self._row_position(raw)
                if not bounds[0] <= position[0] <= bounds[1] or last is not None and position > last:
                    raise AdapterError('BULK_HISTORY_ORDER_OR_WINDOW_INVALID')
                last = position
                # The endpoint scans the entire owner's scope; all rows still
                # determine paging and coverage, but only this native market
                # may enter the market's ledger.
                if raw['symbol'] != self.symbol: continue
                record = parser(raw)
                if kind == 'fills':
                    identity, comparable = record.execution_key, record.to_dict() | {'received_ts': None}
                elif kind == 'fundingHistory': identity, comparable = record.payment_key, record
                else: identity, comparable = record[0].exchange_order_id, record
                if identity in rows and rows[identity][1] != comparable:
                    raise AdapterError('BULK_HISTORY_CONFLICT')
                rows[identity] = (record, comparable)
            if not more:
                return BulkHistoryWindow(tuple(v[0] for v in rows.values()), *anchor[:3], anchor[3], safe,
                    page_num, '' if safe else uncertified_reason)
            if following in seen_cursors: raise AdapterError('BULK_HISTORY_CURSOR_STALLED')
            seen_cursors.add(following); cursor = following
        return BulkHistoryWindow(tuple(v[0] for v in rows.values()), *anchor[:3], anchor[3], False,
                                 max_pages, 'BULK_HISTORY_PAGE_BUDGET_EXHAUSTED')

    async def history_checkpointed(self, kind, *, start_slot, checkpoint, end_slot=None, batch_pages=10, limit=500):
        """Resume one bounded scan and replay all staged pages through validation.

        This experimental path does not persist ledger effects; complete means
        only that the native history reader has certified the returned window.
        """
        import copy
        from .bulk_history_checkpoint import MAX_PAGES
        uint(batch_pages,20)
        if not batch_pages:raise AdapterError('BULK_HISTORY_LIMIT_INVALID')
        scope=dict(account=self.account,owner=self.owner,market=self.market_ref,kind=kind,
                   start_slot=start_slot,end_slot=end_slot)
        if checkpoint.scope!=scope:raise AdapterError('BULK_HISTORY_CHECKPOINT_SCOPE_CONFLICT')
        state=await checkpoint.begin()
        original=self.transport
        class Replay:
            def __init__(self):self.index=0;self.state=state
            async def account(self, requested_kind, owner, **params):
                if requested_kind!=kind or owner!=scope['owner']:
                    raise AdapterError('BULK_HISTORY_CHECKPOINT_SCOPE_CONFLICT')
                if self.index<len(self.state['pages']):
                    page=self.state['pages'][self.index]
                    expected=None if self.index==0 else self.state['pages'][self.index-1]['page']['nextCursor']
                    if params.get('cursor')!=expected:raise AdapterError('BULK_HISTORY_CHECKPOINT_CURSOR_INVALID')
                    self.index+=1
                    return BulkReply(200,copy.deepcopy(page))
                reply=await original.account(requested_kind,owner,**params)
                if isinstance(reply,BulkReply) and reply.status==200 and isinstance(reply.data,dict) and 'error' not in reply.data:
                    self.state=await checkpoint.append(generation=self.state['generation'],version=self.state['version'],
                        requested_cursor=params.get('cursor'),response=reply.data)
                self.index+=1
                return reply
        replay=Replay();reader=copy.copy(self);reader.transport=replay
        try:
            result=await reader.history(kind,start_slot=start_slot,end_slot=end_slot,limit=limit,
                max_pages=min(MAX_PAGES,len(state['pages'])+batch_pages))
        except AdapterError as error:
            retryable={'BULK_READ_UNCERTAIN','BULK_READ_HTTP_FAILED','BULK_HISTORY_BUSY',
                'BULK_HISTORY_STORAGE_ERROR','BULK_HISTORY_STATE_NOT_READY',
                'BULK_HISTORY_CHECKPOINT_STALE','BULK_HISTORY_CHECKPOINT_MISSING',
                'BULK_HISTORY_CHECKPOINT_LIMIT'}
            if str(error) not in retryable:
                await checkpoint.discard(generation=replay.state['generation'],version=replay.state['version'])
            raise
        except (KeyError,TypeError,ValueError):
            # A malformed native page cannot poison every later retry.
            await checkpoint.discard(generation=replay.state['generation'],version=replay.state['version'])
            raise
        if replay.state.get('finished'):
            await checkpoint.discard(generation=replay.state['generation'],version=replay.state['version'])
        return result

    async def fills(self, cursor=None, through=None):
        if cursor is None: raise AdapterError('BULK_HISTORY_START_REQUIRED')
        window = await self.history('fills', start_slot=self.funding_cursor_rank(cursor),
                                    end_slot=self.funding_cursor_rank(through) if through else None)
        return HistoryPage(window.records, None, self.cursor(window.start_slot),
            self.cursor(window.end_slot) if window.complete else None, complete=window.complete,
            truncated=not window.complete, source_revision=SOURCE+':'+self.scope)

    async def funding_history(self, cursor=None, through=None):
        if cursor is None: raise AdapterError('BULK_HISTORY_START_REQUIRED')
        window = await self.history('fundingHistory', start_slot=self.funding_cursor_rank(cursor),
                                    end_slot=self.funding_cursor_rank(through) if through else None)
        return FundingPage(window.records, self.cursor(window.start_slot),
            self.cursor(window.end_slot) if window.complete else None, complete=window.complete,
            truncated=not window.complete, source_revision=SOURCE+':'+self.scope)

    async def lookup(self, client_id, exchange_order_id=None, *, start_slot):
        matches = [oid for oid, cid in self.bindings.items() if cid == client_id]
        if len(matches) != 1 or exchange_order_id is not None and exchange_order_id != matches[0]:
            raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        oid = matches[0]
        first = await self.open_orders()
        window = await self.history('orderHistory', start_slot=start_slot)
        last = await self.open_orders()
        terminal = [r for r in window.records if r[0].exchange_order_id == oid]
        current = [r for r in last if r[0].exchange_order_id == oid]
        if terminal and current: raise AdapterError('BULK_ORDER_STATE_CONFLICT')
        if terminal:
            order, status = terminal[0]
            cumulative=native_decimal(order.cumulative_qty)
            for old, _ in first:
                if old.exchange_order_id==oid:
                    previous=native_decimal(old.cumulative_qty)
                    if status in REJECTED and previous>0:raise AdapterError('BULK_REJECTION_HAS_EXECUTIONS')
                    if order.cumulative_known and previous>cumulative:raise AdapterError('BULK_ORDER_EXECUTION_REGRESSED')
                    cumulative=max(previous,cumulative)
            # The order API and published SDK disagree on whether this
            # status can leave a remainder live. Do not release ownership or
            # claim a terminal order until its semantics can be established.
            if status == 'partiallyFilled':
                return LookupResult('NOT_FOUND_TRANSIENT', oid, text(cumulative),
                    detail='partial_fill_terminality_unverified', cumulative_known=False,
                    native_status=status)
            final = 'REJECTED' if status in REJECTED else 'FILLED' if status == 'filled' else 'CANCELED'
            return LookupResult('FOUND_TERMINAL', oid, text(cumulative), final,
                cumulative_known=order.cumulative_known,native_status=status)
        if current:
            order, status = current[0]
            cumulative=native_decimal(order.cumulative_qty)
            for old, _ in first:
                if old.exchange_order_id==oid:
                    previous=native_decimal(old.cumulative_qty)
                    if order.cumulative_known and previous>cumulative:raise AdapterError('BULK_ORDER_EXECUTION_REGRESSED')
                    cumulative=max(previous,cumulative)
            return LookupResult('NOT_FOUND_TRANSIENT' if status == 'pending' else 'FOUND_OPEN', oid,
                                text(cumulative),cumulative_known=order.cumulative_known,native_status=status,
                                conditional_parked=order.conditional_parked)
        # Even complete retained history cannot prove an unknown submission
        # never landed before the retained interval or after its asOfSlot.
        prior = next((r[0].cumulative_qty for r in first if r[0].exchange_order_id == oid), '0')
        return LookupResult('NOT_FOUND_TRANSIENT', oid, prior, detail='original_identity_not_yet_proven',cumulative_known=False)


class BulkObservation:
    """Connection UI provider. Public account evidence is not authorization."""
    def __init__(self, transport_factory): self.transport_factory = transport_factory

    def preview_venue(self, record, *, store):
        """Build an unsigned account bridge from public saved metadata only."""
        from .bulk import BulkAccountVenue
        from .bulk_budget import BulkAccountBudget
        if record.get('venue')!='bulk' or 'credentials' in record:
            raise AdapterError('BULK_PREVIEW_RECORD_SCOPE_CONFLICT')
        clock=lambda:time.time_ns()//1000000
        budget=BulkAccountBudget(account=record['account_id'],network=record['network'],
            credential_revision=record['revision'],credential_ref=record['credential_ref'],clock_ms=clock)
        venue=BulkAccountVenue(self.transport_factory(record['network']),store=store,owner=record['account_address'],
            network=record['network'],credential_revision=record['revision'],credential_ref=record['credential_ref'],
            history_start_slot=0,risk=budget,clock_ms=clock)
        if venue._account.account_id!=record['account_id']:
            raise AdapterError('BULK_PREVIEW_ACCOUNT_SCOPE_CONFLICT')
        venue.preview_only=True
        venue.preview_capabilities=dict(directions=['long'],margin_modes=['cross'],
            spacings=['arithmetic','geometric'],execution_policies=['gtc','post_only'],protection_modes=['native'])
        return venue

    async def markets(self, network):
        rows = await self.transport_factory(network).get('/api/v1/exchangeInfo')
        if not isinstance(rows, list): raise AdapterError('BULK_CATALOG_INVALID')
        return [market_row('bulk', network, r['symbol'], r['baseAsset']) for r in rows if r['status'] == 'TRADING']

    async def check(self, record):
        from ..key_formats import solana_public_key
        owner = solana_public_key(record['credentials']['secret_key'])
        if owner != record['account_address']: raise AdapterError('BULK_ACCOUNT_IDENTITY_CONFLICT')
        markets = await self.markets(record['network'])
        if not markets: raise AdapterError('BULK_CATALOG_EMPTY')
        reader = BulkEvidenceReader(self.transport_factory(record['network']), account=record['account_id'],
            owner=owner, market_ref=markets[0]['market_ref'])
        try:
            evidence = await reader.full_account()
        except AdapterError as error:
            reason = {
                'BULK_ACCOUNT_NOT_FOUND': '交易所尚未找到此主钱包账户；请确认使用已开通的独立资金钱包，代理钱包暂不支持',
                'BULK_ACCOUNT_KIND_UNVERIFIED': '当前仅支持主钱包账户；子账户或其他账户类型仍待接入',
                'BULK_ISOLATED_SCOPE_UNSUPPORTED': '此账户包含逐仓资金或订单；逐仓资金核算和保护仍待接入',
            }.get(str(error))
            if reason is None: raise
            return {'checks': [dict(key='public_markets', state='pass', message=f'已读取 {len(markets)} 个交易市场'),
                dict(key='public_account', state='pending', message=reason),
                dict(key='authorization', state='pending', message='真实交易权限、资金风险与止损保护尚未验收')],
                'evidence': dict(market_count=len(markets), available_margin=None, asset='USDC',
                                 identity_verified=False, authorization_verified=False, source='bulk:public-account')}
        return {'checks': [dict(key='public_account', state='pass', message='已读取独立资金地址的公开账户及共享保证金快照'),
            dict(key='public_markets', state='pass', message=f'已读取 {len(markets)} 个交易市场'),
            dict(key='authorization', state='pending', message='公开账户查询无需签名；真实交易权限、风险和保护仍待验收')],
            'evidence': {**evidence, 'market_count': len(markets), 'asset': 'USDC',
                         'available_margin': None, 'source': 'bulk:public-account'}}

"""Bounded native order/history reads for the pinned Lighter protocol.

This is an evidence reader, not a GridVenue or an execution grant. Tokens
are supplied by an injected read authenticator; no SDK nonce retry, send or
credential discovery is reachable here. Pagination exhaustion is explicitly
distinct from certified historical coverage. Fee units, funding sign and
retention have not yet been verified with an independent account.
"""
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from functools import wraps
import asyncio
import json
import re

from .. import models
from .base import AdapterError, ExchangeOrder, HistoryPage, FundingPage, LookupResult
from .lighter_wire import SDK_COMMIT, MAX_CLIENT_INDEX, integer
from .observation import ReadTransport


ACTIVE = '/api/v1/accountActiveOrders'
INACTIVE = '/api/v1/accountInactiveOrders'
TRADES = '/api/v1/trades'
FUNDING = '/api/v1/positionFunding'
CANCELED = frozenset(('canceled', 'canceled-post-only', 'canceled-reduce-only',
    'canceled-position-not-allowed', 'canceled-margin-not-allowed',
    'canceled-too-much-slippage', 'canceled-not-enough-liquidity',
    'canceled-self-trade', 'canceled-expired', 'canceled-oco', 'canceled-child',
    'canceled-liquidation', 'canceled-invalid-balance'))


def number(value, *, maximum=2**64-1):
    if type(value) is int:
        return integer(value, maximum)
    if isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,19}', value):
        return integer(int(value), maximum)
    raise AdapterError('LIGHTER_READ_INTEGER_INVALID')


def paired_id(row, numeric, text, *, maximum=2**64-1):
    value = number(row[numeric], maximum=maximum)
    if text in row and number(row[text], maximum=maximum) != value:
        raise AdapterError('LIGHTER_READ_ID_CONFLICT')
    return str(value)


def decimal(value, *, signed=False, positive=False):
    if not isinstance(value, str) or len(value) > 128:
        raise AdapterError('LIGHTER_READ_DECIMAL_INVALID')
    try:
        result = models.parse_decimal_text(value, 'native', allow_negative=signed)
    except models.GridModelError:
        raise AdapterError('LIGHTER_READ_DECIMAL_INVALID') from None
    if positive and result <= 0:
        raise AdapterError('LIGHTER_READ_DECIMAL_INVALID')
    return result


def boolean(value):
    if type(value) is not bool:
        raise AdapterError('LIGHTER_READ_BOOLEAN_INVALID')
    return value


def timestamp(value):
    # Timestamp fields in REST trade/order/funding schemas are milliseconds;
    # transaction_time is microseconds and is intentionally not substituted.
    value = integer(value, 253402300799999)
    return value


def iso_ms(value):
    from datetime import datetime, timedelta, timezone
    return models.format_iso_ts(datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value))


def native_schema(method):
    """Malformed native rows cannot reflect raw upstream values in errors."""
    @wraps(method)
    def parse(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except AdapterError:
            raise
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise AdapterError('LIGHTER_READ_SCHEMA_INVALID') from None
    return parse


class LighterHistoryTransport(ReadTransport):
    """Separate authenticated GET allowlist; observation probes stay public."""
    def __init__(self, session, venue, network):
        if venue not in ('lighter', 'lighter-rh'):
            raise AdapterError('LIGHTER_READ_DEPLOYMENT_INVALID')
        super().__init__(session, venue, network)
        self.paths = frozenset((ACTIVE, INACTIVE, TRADES, FUNDING))


@dataclass(frozen=True)
class NativeOrder:
    order: ExchangeOrder
    status: str
    updated_ms: int
    initial_qty: str
    remaining_qty: str

    def lookup(self):
        state, terminal = 'NOT_FOUND_TRANSIENT', None
        if self.status == 'open':
            state = 'FOUND_OPEN'
        elif self.status == 'filled' or self.status in CANCELED:
            state = 'FOUND_TERMINAL'
            terminal = 'FILLED' if self.status == 'filled' else 'CANCELED'
        return LookupResult(state, self.order.exchange_order_id, self.order.cumulative_qty,
            terminal_state=terminal, detail='native_order_row', native_status=self.status,
            native_status_ms=self.updated_ms)


@dataclass(frozen=True)
class NativeTrade:
    event: models.ExecutionEvent
    native_fee: object
    native_integrator_fee: object
    fee_units_verified: bool = False


@dataclass(frozen=True)
class NativeFunding:
    evidence_key: str
    funding_id: str
    timestamp_ms: int
    change: str
    discount: str
    rate: str
    position_size: str
    position_side: str
    accounting_verified: bool = False


@dataclass(frozen=True)
class ReadBatch:
    records: tuple
    next_cursor: object
    exhausted: bool
    pages: int
    # Neither an empty list nor a missing next_cursor proves retention,
    # server ingestion finality or a historically complete bounded window.
    coverage_verified: bool = field(default=False, init=False)
    reason: str = field(default='LIGHTER_HISTORY_COVERAGE_UNVERIFIED', init=False)
    source_revision: str = field(default='lighter-sdk:' + SDK_COMMIT, init=False)


class LighterEvidenceReader:
    def __init__(self, transport, rules, *, account, account_index, read_token, client_bindings=None):
        if not isinstance(account, str) or not account or len(account) > 128:
            raise AdapterError('LIGHTER_READ_ACCOUNT_INVALID')
        parts = rules.market_ref.split('|')
        deployment = parts[0]
        if (deployment not in ('lighter:mainnet', 'lighter:testnet', 'lighter-rh:mainnet')
                or parts[1:] != ['', str(rules.market_index), 'linear_perp', 'USDC']):
            raise AdapterError('LIGHTER_READ_DEPLOYMENT_INVALID')
        # A real transport must match the native market deployment. Injected
        # offline transports have no origin and cannot discover credentials.
        if isinstance(transport, LighterHistoryTransport):
            from .observation import ORIGINS
            venue, network = deployment.split(':')
            if transport.venue != venue or transport.origin != ORIGINS[venue, network]:
                raise AdapterError('LIGHTER_READ_DEPLOYMENT_MISMATCH')
        self.transport, self.rules, self.account = transport, rules, account
        self.account_index = integer(account_index, 2**63-1)
        self.read_token = read_token
        self.bindings = dict(client_bindings or {})
        for native, original in self.bindings.items():
            if (native == '0' or str(number(native, maximum=MAX_CLIENT_INDEX)) != native or not isinstance(original, str)
                    or not re.fullmatch(r'[0-9]{1,15}', original) or int(original) != int(native)):
                raise AdapterError('LIGHTER_READ_BINDING_INVALID')

    def client_id(self, native):
        return self.bindings.get(native, native) if native != '0' else None

    def _scope(self, row, market_key):
        if integer(row[market_key], 2**15-1) != self.rules.market_index:
            raise AdapterError('LIGHTER_READ_MARKET_MISMATCH')
        if row.get('market_kind', 'perps') != 'perps':
            raise AdapterError('LIGHTER_READ_PRODUCT_MISMATCH')

    @native_schema
    def order(self, row):
        self._scope(row, 'market_index')
        if integer(row['owner_account_index'], 2**63-1) != self.account_index:
            raise AdapterError('LIGHTER_READ_ACCOUNT_MISMATCH')
        oid = paired_id(row, 'order_index', 'order_id')
        cid = paired_id(row, 'client_order_index', 'client_order_id', maximum=MAX_CLIENT_INDEX)
        initial = decimal(row['initial_base_amount'], positive=True)
        remaining = decimal(row['remaining_base_amount'])
        filled = decimal(row['filled_base_amount'])
        price = decimal(row['price'], positive=True)
        with localcontext() as ctx:
            ctx.prec = 256
            if filled + remaining != initial:
                raise AdapterError('LIGHTER_READ_QUANTITY_CONFLICT')
        status = row['status']
        if status not in CANCELED | {'open', 'pending', 'in-progress', 'filled'}:
            raise AdapterError('LIGHTER_READ_STATUS_UNSUPPORTED')
        if status == 'filled' and remaining != 0 or status == 'open' and remaining == 0:
            raise AdapterError('LIGHTER_READ_QUANTITY_CONFLICT')
        if (row['type'] not in ('limit', 'market') or decimal(row['trigger_price']) != 0
                or type(row.get('order_flags', 0)) is not int or row.get('order_flags', 0) != 0):
            raise AdapterError('LIGHTER_READ_PROTECTION_UNVERIFIED')
        ask, reduce = boolean(row['is_ask']), boolean(row['reduce_only'])
        updated = timestamp(row['updated_at'])
        result = ExchangeOrder(self.client_id(cid), oid, 'sell' if ask else 'buy',
            models.canonical_decimal_text(initial), models.canonical_decimal_text(price),
            models.canonical_decimal_text(filled), reduce, 'rest_query')
        return NativeOrder(result, status, updated, models.canonical_decimal_text(initial),
                           models.canonical_decimal_text(remaining))

    @native_schema
    def trade(self, row):
        self._scope(row, 'market_id')
        ask = integer(row['ask_account_id'], 2**63-1) == self.account_index
        bid = integer(row['bid_account_id'], 2**63-1) == self.account_index
        if ask == bid:  # Neither owner, or a self-match requiring two legs.
            raise AdapterError('LIGHTER_READ_TRADE_ACCOUNT_AMBIGUOUS')
        if row['type'] != 'trade':
            raise AdapterError('LIGHTER_READ_NONTRADE_EXECUTION_UNVERIFIED')
        leg = 'ask' if ask else 'bid'
        tid = paired_id(row, 'trade_id', 'trade_id_str')
        oid = paired_id(row, leg + '_id', leg + '_id_str')
        cid = paired_id(row, leg + '_client_id', leg + '_client_id_str', maximum=MAX_CLIENT_INDEX)
        quantity, price = decimal(row['size'], positive=True), decimal(row['price'], positive=True)
        maker = boolean(row['is_maker_ask']) == ask
        fee_field = 'maker_fee' if maker else 'taker_fee'
        native_fee, integrator_fee = row.get(fee_field), row.get('integrator_' + fee_field)
        # Preserve omitted/zero distinction and raw integer units. No guessed
        # USDC divisor or deduction of rebates from the other account's leg.
        for fee in (native_fee, integrator_fee):
            if fee is not None and (type(fee) is not int or abs(fee) > 2**63-1):
                raise AdapterError('LIGHTER_READ_FEE_INVALID')
        ts = iso_ms(timestamp(row['timestamp']))
        event = models.ExecutionEvent(
            execution_key=models.sha256_hex(['lighter-trade', self.account, self.rules.market_ref, tid, leg]),
            account=self.account, market=self.rules.market_ref, order_client_id=self.client_id(cid),
            exchange_order_id=oid, trade_id=tid, side='sell' if ask else 'buy',
            qty_base=models.canonical_decimal_text(quantity), price=models.canonical_decimal_text(price),
            fee_amount=None, fee_asset=None, is_maker=maker, exchange_ts=ts,
            received_ts=models.utc_now_iso(), source='rest_backfill', source_seq=tid, finality='unknown')
        return NativeTrade(event, native_fee, integrator_fee)

    @native_schema
    def funding(self, row):
        self._scope(row, 'market_id')
        fid = str(integer(row['funding_id'], 2**63-1))
        ts = timestamp(row['timestamp'])
        side = row['position_side']
        if side not in ('long', 'short'):
            raise AdapterError('LIGHTER_READ_FUNDING_SIDE_INVALID')
        values = [models.canonical_decimal_text(decimal(row[key], signed=key in ('change', 'rate')))
                  for key in ('change', 'discount', 'rate', 'position_size')]
        # This is an evidence dedup key, not a certified funding payment key.
        # Amount is excluded so a corrected/conflicting duplicate is detected.
        key = models.sha256_hex(['lighter-funding', self.account, self.rules.market_ref, fid, side])
        return NativeFunding(key, fid, ts, *values, side)

    async def _get(self, path, params):
        try:
            token = await asyncio.wait_for(self.read_token(), timeout=5)
            if not isinstance(token, str) or not token or len(token) > 4096 or '\r' in token or '\n' in token:
                raise AdapterError('LIGHTER_READ_AUTH_INVALID')
            data = await asyncio.wait_for(self.transport.get(path, params, headers={'authorization': token}), timeout=20)
        except asyncio.CancelledError:
            raise
        except Exception:
            # HTTP libraries may reflect auth headers or bodies in exceptions.
            raise AdapterError('LIGHTER_AUTHENTICATED_READ_FAILED') from None
        if not isinstance(data, dict) or type(data.get('code')) is not int or data['code'] != 200:
            raise AdapterError('LIGHTER_READ_RESPONSE_INVALID')
        return data

    async def active_orders(self):
        data = await self._get(ACTIVE, dict(account_index=self.account_index,
            market_id=self.rules.market_index, market_type='perp'))
        rows = self._rows(data, 'orders', 10000)
        if data.get('next_cursor') not in (None, ''):
            raise AdapterError('LIGHTER_ACTIVE_ORDERS_TRUNCATED')
        result = tuple(self.order(row) for row in rows)
        if any(r.status not in ('open', 'pending', 'in-progress') for r in result):
            raise AdapterError('LIGHTER_ACTIVE_ORDERS_STATUS_INVALID')
        if len({r.order.exchange_order_id for r in result}) != len(result):
            raise AdapterError('LIGHTER_READ_DUPLICATE_ORDER')
        cids = [r.order.client_id for r in result if r.order.client_id is not None]
        if len(set(cids)) != len(cids):
            raise AdapterError('LIGHTER_READ_DUPLICATE_CLIENT_ID')
        return result

    @staticmethod
    def _rows(data, key, limit):
        rows = data.get(key)
        if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(r, dict) for r in rows):
            raise AdapterError('LIGHTER_READ_ROWS_INVALID')
        return rows

    async def history(self, kind, *, cursor=None, max_pages=5, start_ms=None, end_ms=None):
        integer(max_pages, 20, minimum=1)
        kinds = {'orders': (INACTIVE, 'orders', self.order), 'trades': (TRADES, 'trades', self.trade),
                 'funding': (FUNDING, 'position_fundings', self.funding)}
        if kind not in kinds:
            raise AdapterError('LIGHTER_READ_HISTORY_KIND_INVALID')
        if (start_ms is None) != (end_ms is None):
            raise AdapterError('LIGHTER_READ_WINDOW_INVALID')
        if start_ms is not None:
            timestamp(start_ms); timestamp(end_ms)
            if start_ms > end_ms or kind != 'funding':
                raise AdapterError('LIGHTER_READ_WINDOW_INVALID')
        scope = models.sha256_hex([self.account, self.account_index, self.rules.market_ref,
                                  kind, start_ms, end_ms, SDK_COMMIT])
        seen_cursors = set()
        if cursor is not None:
            # A persisted continuation is bound to account, deployment, market,
            # history kind and fixed window. Never accept a naked native cursor.
            try:
                if not isinstance(cursor, str) or len(cursor) > 40000:
                    raise ValueError()
                saved = json.loads(cursor)
                if set(saved) != {'scope', 'native', 'seen'} or saved['scope'] != scope:
                    raise ValueError()
                native, seen = saved['native'], saved['seen']
                if (not isinstance(native, str) or not 1 <= len(native) <= 2048
                        or not isinstance(seen, list) or len(seen) > 400
                        or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x) for x in seen)):
                    raise ValueError()
                seen_cursors = set(seen)
                if models.sha256_hex(native) in seen_cursors:
                    raise ValueError()
                cursor = native
            except (ValueError, TypeError):
                raise AdapterError('LIGHTER_READ_CURSOR_INVALID') from None
        path, key, parser = kinds[kind]
        params = dict(account_index=self.account_index, limit=100)
        if kind == 'funding':
            params.update(market_ids=str(self.rules.market_index), side='all')
            if start_ms is not None:
                params.update(start_timestamp=start_ms, end_timestamp=end_ms)
        else:
            params.update(market_id=self.rules.market_index, market_type='perp')
        if kind == 'trades':
            params.update(sort_by='trade_id', sort_dir='desc', aggregate='false', role='all', type='all')
        records = {}
        for page in range(1, max_pages + 1):
            if cursor is not None:
                params['cursor'] = cursor; seen_cursors.add(models.sha256_hex(cursor))
                if len(seen_cursors) > 400:
                    raise AdapterError('LIGHTER_HISTORY_SCAN_LIMIT')
            data = await self._get(path, params)
            for raw in self._rows(data, key, 100):
                record = parser(raw)
                if kind == 'orders':
                    if record.status not in CANCELED | {'filled'}:
                        raise AdapterError('LIGHTER_INACTIVE_ORDERS_STATUS_INVALID')
                    identity, comparable = record.order.exchange_order_id, record
                elif kind == 'trades':
                    identity = record.event.execution_key
                    comparable = record.event.to_dict() | {'received_ts': None,
                        'native_fee': record.native_fee, 'native_integrator_fee': record.native_integrator_fee}
                else:
                    identity, comparable = record.evidence_key, record
                    if start_ms is not None and not start_ms <= record.timestamp_ms <= end_ms:
                        raise AdapterError('LIGHTER_READ_WINDOW_MISMATCH')
                if identity in records and records[identity][1] != comparable:
                    raise AdapterError('LIGHTER_HISTORY_CONFLICT')
                records[identity] = (record, comparable)
            # The schema explicitly declares next_cursor optional. Missing or
            # empty ends the API pagination only, never a recovery watermark.
            next_cursor = data.get('next_cursor')
            if next_cursor in (None, ''):
                return ReadBatch(tuple(r[0] for r in records.values()), None, True, page)
            if (not isinstance(next_cursor, str) or len(next_cursor) > 2048
                    or models.sha256_hex(next_cursor) in seen_cursors):
                raise AdapterError('LIGHTER_HISTORY_CURSOR_STALLED')
            cursor = next_cursor
        bookmark = json.dumps(dict(scope=scope, native=cursor, seen=sorted(seen_cursors)), separators=(',', ':'))
        return ReadBatch(tuple(r[0] for r in records.values()), bookmark, False, max_pages)

    async def lookup(self, client_id, exchange_order_id=None, *, max_pages=5):
        if not isinstance(client_id, str) or not re.fullmatch(r'[0-9]{1,15}', client_id):
            raise AdapterError('LIGHTER_READ_CLIENT_ID_INVALID')
        cid = str(integer(int(client_id), MAX_CLIENT_INDEX, minimum=1))
        oid = str(number(exchange_order_id)) if exchange_order_id is not None else None
        # Bracket the paged terminal query with active reads. An order can
        # leave the active book while the inactive replica is catching up.
        first = await self.active_orders()
        history = await self.history('orders', max_pages=max_pages)
        last = await self.active_orders()
        matches = []
        for row in (*first, *history.records, *last):
            current = row.order
            same_cid = current.client_id is not None and int(current.client_id) == int(cid)
            same_oid = oid is not None and current.exchange_order_id == oid
            if same_cid or same_oid:
                if not same_cid or oid is not None and current.exchange_order_id != oid:
                    raise AdapterError('LIGHTER_LOOKUP_IDENTITY_CONFLICT')
                matches.append(row)
        if not matches:
            return LookupResult('NOT_FOUND_TRANSIENT', detail='native_absence_not_certified')
        if len({r.order.exchange_order_id for r in matches}) != 1:
            raise AdapterError('LIGHTER_LOOKUP_IDENTITY_CONFLICT')
        matches.sort(key=lambda r: r.updated_ms)
        for old, new in zip(matches, matches[1:]):
            if (old.updated_ms == new.updated_ms and old != new
                    or (old.order.side, old.order.price, old.initial_qty, old.order.reduce_only)
                        != (new.order.side, new.order.price, new.initial_qty, new.order.reduce_only)
                    or Decimal(new.order.cumulative_qty) < Decimal(old.order.cumulative_qty)
                    or old.status in CANCELED | {'filled'} and new.status != old.status):
                raise AdapterError('LIGHTER_LOOKUP_STATE_CONFLICT')
        latest = matches[-1]
        current_ids = {r.order.exchange_order_id for r in last}
        if latest.status not in CANCELED | {'filled'} and latest.order.exchange_order_id not in current_ids:
            # The first snapshot is now stale. Disappearance can be a fill
            # with a lagging history replica; never reuse that old OPEN proof.
            return LookupResult('NOT_FOUND_TRANSIENT', latest.order.exchange_order_id,
                latest.order.cumulative_qty, detail='active_disappeared_history_pending',
                native_status=latest.status, native_status_ms=latest.updated_ms)
        return matches[-1].lookup()

    async def fills(self, **kwargs):
        batch = await self.history('trades', **kwargs)
        return HistoryPage(tuple(r.event for r in batch.records), batch.next_cursor,
                           None, None, complete=False, truncated=True, source_revision=batch.source_revision)

    async def funding_history(self, **kwargs):
        batch = await self.history('funding', **kwargs)
        # Raw observations remain available via history('funding'). A guessed
        # sign, unit or coverage must never be committed to the money ledger.
        return FundingPage((), None, None, complete=False, truncated=True, source_revision=batch.source_revision)

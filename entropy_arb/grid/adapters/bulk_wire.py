"""Bulk order preparation and durable original-request identity; no HTTP.

Official keychain only. The engine's client ID is never changed. A native
deterministic order ID and a unique account-wide nonce are bound to it before
any future sender may use the prepared envelope. No trading grant is issued.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext, ROUND_HALF_UP
import copy
import json
import re
import time

from .. import models
from .base import AdapterError

SDK_VERSION = '0.1.30'
U64 = 2**64-1
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def native_tif_matches(native,wire_tif):
    """Only equivalent native spellings; slide/join are different policies.

    Original signed requests keep GTC/IOC/ALO. Current account responses use
    postOnly for ALO, while older native responses used alo.
    """
    if type(native) is not str or type(wire_tif) is not str:return False
    return native in {'GTC':('gtc',),'IOC':('ioc',),'ALO':('alo','postOnly')}.get(wire_tif,())


def uint(value, maximum=U64):
    if type(value) is not int or not 0 <= value <= maximum:
        raise AdapterError('BULK_INTEGER_INVALID')
    return value


def base58(value, length=32):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise AdapterError('BULK_BASE58_INVALID')
    n = 0
    try:
        for ch in value:
            n = n*58 + ALPHABET.index(ch)
    except ValueError:
        raise AdapterError('BULK_BASE58_INVALID') from None
    raw = b'\0' * (len(value)-len(value.lstrip('1'))) + n.to_bytes((n.bit_length()+7)//8, 'big')
    if len(raw) != length:
        raise AdapterError('BULK_BASE58_INVALID')
    return raw


def native_decimal(value, *, negative=True):
    # JSON numbers are parsed as text before reaching this function. Integer
    # monetary values are exact too; binary floats and bools are never accepted.
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or len(value) > 128
            or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?', value)):
        raise AdapterError('BULK_DECIMAL_INVALID')
    d = Decimal(value)
    if not d.is_finite() or abs(d.adjusted()) > 128 or not negative and d < 0:
        raise AdapterError('BULK_DECIMAL_INVALID')
    return d


def text(value):
    return models.canonical_decimal_text(value)


def sdk_float(value):
    """Validate exact protocol units across the SDK's mandatory f64 bridge.

    Ledger/request values remain DecimalText. Only the official SDK call uses
    float; its documented round(f64*1e8) must produce exactly the same integer.
    Values above a conservative exact-integer bound are refused, not rounded.
    """
    with localcontext() as ctx:
        ctx.prec = 256
        d = models.parse_decimal_text(value, 'bulk_wire', allow_negative=False)
        units = d * Decimal(10)**8
    if units != units.to_integral_value() or not 1 <= units <= 2**52-1:
        raise AdapterError('BULK_WIRE_PRECISION_INVALID')
    value_float = float(d)
    # Rust f64::round rounds halfway away from zero. Inputs here are positive.
    native = Decimal.from_float(value_float * 1e8).to_integral_value(rounding=ROUND_HALF_UP)
    if native != units:
        raise AdapterError('BULK_SDK_PRECISION_LOSS')
    return value_float


@dataclass(frozen=True)
class BulkWireRules:
    market_ref: str
    symbol: str
    tick: str
    step: str
    min_notional: str
    max_leverage: str
    policies: tuple

    @classmethod
    def from_native(cls, row, *, network='mainnet'):
        if network not in ('mainnet', 'testnet'):
            raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        symbol = row['symbol']
        if (not isinstance(symbol, str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', symbol)
                or row['status'] != 'TRADING' or row['quoteAsset'] != 'USD'):
            raise AdapterError('BULK_MARKET_NOT_SUPPORTED')
        sd, pd = uint(row['sizeDecimals'], 8), uint(row['pricePrecision'], 8)
        tick, step, minimum, leverage = [native_decimal(row[k], negative=False)
            for k in ('tickSize', 'sizeIncrement', 'minNotional', 'maxLeverage')]
        if tick <= 0 or minimum <= 0 or leverage < 1 or step != Decimal(10)**-sd or tick != Decimal(10)**-pd:
            raise AdapterError('BULK_MARKET_RULES_UNVERIFIED')
        policies = row['timeInForces']
        if not isinstance(policies, list) or not policies or any(not isinstance(v, str) for v in policies):
            raise AdapterError('BULK_MARKET_POLICIES_INVALID')
        market = models.MarketIdentity('bulk:'+network, symbol, '', 'linear_perp', 'USDC')
        return cls(market.market_key, symbol, text(tick), text(step), text(minimum), text(leverage), tuple(policies))

    def compile(self, request, *, account):
        if request.account != account or request.market != self.market_ref:
            raise AdapterError('BULK_ORDER_SCOPE_MISMATCH')
        if request.whole_position:
            raise AdapterError('BULK_PROTECTION_UNVERIFIED')
        if request.trigger_price is not None:
            return self.compile_fixed_stop(request, account=account)
        if request.request_deadline_sec is not None:
            raise AdapterError('BULK_REQUEST_DEADLINE_UNVERIFIED')
        if request.side not in ('buy', 'sell') or type(request.reduce_only) is not bool:
            raise AdapterError('BULK_ORDER_FLAGS_INVALID')
        tif = {'gtc': 'GTC', 'post_only': 'ALO', 'ioc': 'IOC', 'reduce_only_ioc': 'IOC'}.get(request.policy)
        if tif not in self.policies or request.policy == 'reduce_only_ioc' and not request.reduce_only:
            raise AdapterError('BULK_ORDER_POLICY_UNSUPPORTED')
        qty, price = (models.parse_decimal_text(v, 'bulk_wire', allow_negative=False)
                      for v in (request.qty_base, request.price))
        with localcontext() as ctx:
            ctx.prec = 256
            if qty <= 0 or price <= 0 or qty % Decimal(self.step) != 0:
                raise AdapterError('BULK_ORDER_SIZE_INVALID')
            if request.policy in ('gtc', 'post_only') and price % Decimal(self.tick) != 0:
                raise AdapterError('BULK_ORDER_PRICE_INVALID')
            if qty*price < Decimal(self.min_notional):
                raise AdapterError('BULK_ORDER_BELOW_MINIMUM')
        sdk_float(request.qty_base); sdk_float(request.price)
        # Decimal strings are persisted. Native float conversion is confined
        # to BulkKeychain.prepare, after the original identity is reserved.
        return dict(type='order', symbol=self.symbol, is_buy=request.side == 'buy',
            price=request.price, size=request.qty_base, reduce_only=request.reduce_only,
            iso=False, order_type=dict(type='limit', tif=tif))

    def compile_fixed_stop(self, request, *, account):
        """Native downward market stop, with an exact fixed reducing quantity."""
        if (request.account != account or request.market != self.market_ref
                or request.side != 'sell' or request.reduce_only is not True
                or request.whole_position is not False or request.policy != 'gtc'
                or request.request_deadline_sec is not None or request.trigger_price != request.price
                or 'GTC' not in self.policies):
            raise AdapterError('BULK_FIXED_STOP_SCOPE_INVALID')
        qty = models.parse_decimal_text(request.qty_base, 'bulk_stop', allow_negative=False)
        trigger = models.parse_decimal_text(request.trigger_price, 'bulk_stop', allow_negative=False)
        with localcontext() as ctx:
            ctx.prec = 256
            if (qty <= 0 or trigger <= 0 or qty % Decimal(self.step)
                    or trigger % Decimal(self.tick) or qty * trigger < Decimal(self.min_notional)):
                raise AdapterError('BULK_FIXED_STOP_RULES_INVALID')
        sdk_float(request.qty_base); sdk_float(request.trigger_price)
        return dict(type='stop', symbol=self.symbol, size=request.qty_base,
            trigger_price=request.trigger_price, iso=False)


class BulkKeychain:
    """Lazy, pinned SDK with no implicit nonce, network, faucet or settings."""
    def __init__(self, *, network, owner, secret=None, native_identity=None):
        if network not in ('mainnet', 'testnet'):
            raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        base58(owner)
        from importlib.metadata import version
        if version('bulk-keychain') != SDK_VERSION:
            raise AdapterError('BULK_SDK_VERSION_MISMATCH')
        import bulk_keychain
        self.sdk, self.network, self.owner = bulk_keychain, network, owner
        self.native_identity = native_identity
        self.wallet = None
        if secret is not None:
            from ..key_formats import solana_public_key
            if solana_public_key(secret) != owner:
                raise AdapterError('BULK_SIGNER_OWNER_MISMATCH')
            self.wallet = bulk_keychain.Signer.from_base58(secret, network)
            if self.wallet.pubkey != owner:
                raise AdapterError('BULK_SIGNER_OWNER_MISMATCH')

    def prepare(self, action, nonce):
        uint(nonce)
        if isinstance(action,dict) and action.get('type')=='stop':
            if set(action)!={'type','symbol','size','trigger_price','iso'} or action['iso'] is not False:
                raise AdapterError('BULK_PREPARE_ACTION_FORBIDDEN')
            return self.prepare_stop(action['symbol'],action['size'],action['trigger_price'],nonce)
        if (set(action) != {'type', 'symbol', 'is_buy', 'price', 'size', 'reduce_only', 'iso', 'order_type'}
                or action['type'] != 'order' or action['iso'] is not False
                or type(action['is_buy']) is not bool or type(action['reduce_only']) is not bool
                or action['order_type'] not in ({'type': 'limit', 'tif': tif} for tif in ('GTC', 'IOC', 'ALO'))):
            raise AdapterError('BULK_PREPARE_ACTION_FORBIDDEN')
        native = copy.deepcopy(action)
        native['price'], native['size'] = sdk_float(action['price']), sdk_float(action['size'])
        prepared = self.sdk.prepare_order(native, self.network, account=self.owner,
                                         signer=self.owner, nonce=nonce)
        oid = prepared['order_id']; base58(oid)
        message = bytes(prepared['message_bytes'])
        expected = nonce.to_bytes(8, 'little') + base58(self.owner) + bytes([1 if self.network == 'mainnet' else 2])
        if (not message.endswith(expected) or prepared['account'] != self.owner
                or prepared['signer'] != self.owner or prepared['nonce'] != nonce):
            raise AdapterError('BULK_PREPARED_SCOPE_INVALID')
        return prepared

    def sign(self, prepared):
        if self.wallet is None:
            raise AdapterError('BULK_SIGNER_NOT_CONFIGURED')
        actions = prepared['actions']
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
            raise AdapterError('BULK_SIGN_ACTION_FORBIDDEN')
        if set(actions[0]) == {'l'} and set(actions[0]['l']) == {'b', 'c', 'i', 'px', 'r', 'sz', 'tif'}:
            native = actions[0]['l']
            action = dict(type='order', symbol=native['c'], is_buy=native['b'],
                price=text(native_decimal(str(native['px']))), size=text(native_decimal(str(native['sz']))),
                reduce_only=native['r'], iso=native['i'], order_type=dict(type='limit', tif=native['tif']))
            expected = self.prepare(action, prepared['nonce'])
        elif set(actions[0]) == {'cx'} and set(actions[0]['cx']) == {'c', 'oid'}:
            native = actions[0]['cx']
            expected = self.prepare_cancel(native['c'], native['oid'], prepared['nonce'])
        elif set(actions[0]) == {'st'} and set(actions[0]['st']) == {'c', 'd', 'sz', 'tr', 'lim', 'i'}:
            native = actions[0]['st']
            if native['d'] is not False or native['lim'] is not None or native['i'] is not False:
                raise AdapterError('BULK_SIGN_ACTION_FORBIDDEN')
            expected = self.prepare_stop(native['c'], text(native_decimal(str(native['sz']))),
                text(native_decimal(str(native['tr']))), prepared['nonce'])
        else:
            raise AdapterError('BULK_SIGN_ACTION_FORBIDDEN')
        # Finalize alone does not bind changed JSON to prepared bytes. Rebuild
        # the original canonical prepare with the pinned SDK, then compare.
        if any(expected.get(k) != prepared.get(k) for k in ('actions', 'nonce', 'account', 'signer', 'order_id', 'message_bytes')):
            raise AdapterError('BULK_PREPARED_REQUEST_CHANGED')
        # Native finalize verifies the Ed25519 signature. No HTTP is reachable.
        signed = self.wallet.sign_prepared(prepared)
        if any(signed.get(k) != prepared.get(k) for k in ('actions', 'nonce', 'account', 'signer', 'order_id')):
            raise AdapterError('BULK_SIGNED_REQUEST_CHANGED')
        return signed

    def prepare_cancel(self, symbol, order_id, nonce):
        if not isinstance(symbol, str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', symbol):
            raise AdapterError('BULK_CANCEL_MARKET_INVALID')
        base58(order_id); uint(nonce)
        prepared = self.sdk.prepare_order(dict(type='cancel', symbol=symbol, order_id=order_id),
            self.network, account=self.owner, signer=self.owner, nonce=nonce)
        suffix = nonce.to_bytes(8, 'little')+base58(self.owner)+bytes([1 if self.network=='mainnet' else 2])
        if (not bytes(prepared['message_bytes']).endswith(suffix) or prepared['account']!=self.owner
                or prepared['signer']!=self.owner or prepared['nonce']!=nonce
                or prepared.get('order_id') is not None):
            raise AdapterError('BULK_PREPARED_SCOPE_INVALID')
        return prepared

    def prepare_stop(self, symbol, size, trigger, nonce):
        """Prepare a fixed-size downward stop; does not grant live support.

        Native IDs come from official Action.hash(), not a hash of the
        signable message (different index, metadata and domain framing).
        The fixed-size protection coverage contract still gates integration.
        """
        if self.native_identity is None:
            raise AdapterError('BULK_STOP_IDENTITY_UNVERIFIED')
        if not isinstance(symbol, str) or not re.fullmatch('[A-Z0-9][A-Z0-9._-]{0,48}-USD', symbol):
            raise AdapterError('BULK_STOP_MARKET_INVALID')
        uint(nonce)
        action = dict(type='stop', symbol=symbol, is_buy=False, size=sdk_float(size),
                      trigger_price=sdk_float(trigger), iso=False)
        prepared = self.sdk.prepare_order(action, self.network, account=self.owner,
                                          signer=self.owner, nonce=nonce)
        identity = self.native_identity.calculate(action=prepared['actions'][0], owner=self.owner, nonce=nonce)
        canonical = bytes.fromhex(identity['canonical_action_hex'])
        # Official conditional signing uses the V4 schema. Verify the native
        # hash helper and signer serialize this exact action identically.
        expected = (b'\xff'*8 + b'bulk-actions\x04' + (1).to_bytes(8, 'little') + canonical
                    + nonce.to_bytes(8, 'little') + base58(self.owner)
                    + bytes([1 if self.network == 'mainnet' else 2]))
        if (bytes(prepared['message_bytes']) != expected or prepared['account'] != self.owner
                or prepared['signer'] != self.owner or prepared['nonce'] != nonce
                or prepared.get('order_id') is not None):
            raise AdapterError('BULK_STOP_CANONICAL_CONFLICT')
        prepared['order_id'] = identity['order_id']
        return prepared


class BulkIntentJournal:
    """A crash-safe binding, deliberately separate from a future HTTP sender."""
    def __init__(self, store, *, account, owner, network, clock_ns=time.time_ns, require_core=False):
        base58(owner)
        if network not in ('mainnet', 'testnet'):
            raise AdapterError('BULK_NETWORK_UNSUPPORTED')
        self.store, self.account, self.owner, self.network = store, account, owner, network
        self.clock_ns = clock_ns
        self.require_core = require_core
        self.scope = models.sha256_hex([network, owner])
        # Bumped by every durable journal write (all of them route through
        # _put). The account-level bindings cache keys on this, so a submit
        # or cancel invalidates the cached oid->client-id maps immediately —
        # a lookup right after attach must never serve a stale map.
        self.revision = 0

    def _key(self, cid):
        if not isinstance(cid, str) or not re.fullmatch('[a-zA-Z0-9:_-]{1,128}', cid):
            raise AdapterError('BULK_CLIENT_ID_INVALID')
        return 'bulk-intent:'+self.scope+':'+cid

    @staticmethod
    def _load(c, key):
        row = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, c, key, value):
        self.revision += 1
        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (key, json.dumps(value, sort_keys=True, separators=(',', ':'))))

    def get(self, client_id):
        return self.store.read(lambda c: self._load(c, self._key(client_id)))

    async def record_lookup(self, client_id, proof):
        """Persist native evidence separately from the original send receipt."""
        from .bulk_order_evidence import record_lookup
        return await record_lookup(self, client_id, proof)

    async def resolve_stop_identity(self,client_id,*,protocol,terminal_order,terminal_status):
        """Append a proved legacy stop-root resolution without rewriting history.

        Native cancellation history can omit size/side. The caller must first
        parse its positive terminal row with the exact durable root binding.
        Neither a matching price nor absence from the book is a terminal proof.
        This writes no nonce and has no signing/submission capability.
        """
        from .bulk_identity import STOP_HASH_SCHEMA,STOP_HASH_SOURCE_REVISION
        from .bulk_read import CANCELED
        if terminal_status not in CANCELED:raise AdapterError('BULK_STOP_RESOLUTION_TERMINAL_REQUIRED')
        if protocol.owner!=self.owner or protocol.network!=self.network:
            raise AdapterError('BULK_STOP_RESOLUTION_SCOPE_CONFLICT')
        key=self._key(client_id)
        def write(c):
            original=self._load(c,key)
            if (original is None or original['account']!=self.account or original['action'].get('type')!='stop'
                    or original['state'] not in ('MAY_HAVE_SENT','ACK_ACCEPTED') or not original.get('message_hex')):
                raise AdapterError('BULK_STOP_RESOLUTION_ORIGINAL_REQUIRED')
            prepared=protocol.prepare(original['action'],original['nonce'])
            if (bytes(prepared['message_bytes']).hex()!=original['message_hex']
                    or prepared['actions']!=original['native_actions']):
                raise AdapterError('BULK_STOP_RESOLUTION_SIGNING_CONFLICT')
            identity=protocol.native_identity.calculate(action=prepared['actions'][0],owner=self.owner,nonce=original['nonce'])
            if identity.get('order_hash_schema')!=STOP_HASH_SCHEMA or identity.get('order_hash_source_revision')!=STOP_HASH_SOURCE_REVISION:
                raise AdapterError('BULK_STOP_RESOLUTION_HASH_UNVERIFIED')
            order=terminal_order;action=original['action'];oid=prepared['order_id']
            market=models.MarketIdentity('bulk:'+self.network,action['symbol'],'','linear_perp','USDC').market_key
            if (oid!=identity['order_id'] or order.exchange_order_id!=oid or order.client_id!=client_id
                    or order.side!='sell' or order.reduce_only is not True or order.whole_position
                    or order.qty_base!=action['size'] or order.price!=action['trigger_price']
                    or order.source!='rest_query' or order.cumulative_qty!='0'):
                raise AdapterError('BULK_STOP_RESOLUTION_TERMINAL_CONFLICT')
            resolution=dict(native_order_id=oid,status=terminal_status,market=market,
                request_hash=original['request_hash'],order_hash_schema=STOP_HASH_SCHEMA,
                order_hash_source_revision=STOP_HASH_SOURCE_REVISION,
                message_sha256=__import__('hashlib').sha256(bytes.fromhex(original['message_hex'])).hexdigest())
            if original.get('terminal_resolution') is not None:
                if original['terminal_resolution']!=resolution:raise AdapterError('BULK_STOP_RESOLUTION_CONFLICT')
                return resolution
            bound=self._load(c,'bulk-order:'+self.scope+':'+oid)
            if bound not in (None,client_id):raise AdapterError('BULK_NATIVE_ID_COLLISION')
            original['terminal_resolution']=resolution
            self._put(c,key,original)
            self._put(c,'bulk-order:'+self.scope+':'+oid,client_id)
            return resolution
        return await self.store.write(write)

    def bindings(self, symbol):
        prefix = 'bulk-intent:'+self.scope+':'
        def read(c):
            rows = [json.loads(row[0]) for row in c.execute('SELECT value FROM runtime_state WHERE key LIKE ?', (prefix+'%',))]
            return {r.get('terminal_resolution',{}).get('native_order_id',r['order_id']):r['client_id'] for r in rows if r['account']==self.account
                    and r['action']['symbol']==symbol and r.get('order_id')}
        return self.store.read(read)

    def action_bindings(self,symbol):
        """Snapshot original actions for reads; never synthesize a new identity."""
        prefix='bulk-intent:'+self.scope+':'
        def read(c):
            result={}
            for row in c.execute('SELECT value FROM runtime_state WHERE key LIKE ?',(prefix+'%',)):
                value=json.loads(row[0])
                if value['account']!=self.account or value['action']['symbol']!=symbol or not value.get('order_id'):continue
                oid=value.get('terminal_resolution',{}).get('native_order_id',value['order_id'])
                if oid in result:raise AdapterError('BULK_NATIVE_ID_COLLISION')
                result[oid]=dict(account=self.account,client_id=value['client_id'],action=value['action'])
            return result
        return self.store.read(read)

    async def reserve(self, request, rules, *, history_start_slot=None):
        action = rules.compile(request, account=self.account)
        if history_start_slot is not None: uint(history_start_slot)
        key = self._key(request.client_id)
        digest = models.sha256_hex(request.to_dict())
        def commit(c):
            old = self._load(c, key)
            if old:
                if old['request_hash'] != digest or old['account'] != self.account:
                    raise AdapterError('BULK_ORIGINAL_INTENT_CONFLICT')
                if old['state'] == 'ABORTED_NOT_SENT':
                    raise AdapterError('BULK_ORIGINAL_REQUEST_ABORTED')
                return old
            counter = 'bulk-nonce:'+self.scope
            last = self._load(c, counter)
            nonce = uint(max(uint(self.clock_ns()), (uint(last) if last is not None else 0)+1))
            value = dict(account=self.account, owner=self.owner, network=self.network,
                client_id=request.client_id, intent_id=request.intent_id,
                request_hash=digest, nonce=nonce, action=action, state='ALLOCATED')
            if history_start_slot is not None: value['history_start_slot'] = history_start_slot
            self._put(c, counter, nonce); self._put(c, key, value)
            return value
        return await self.store.write(commit)

    async def reject_fixed_not_sent(self, request, rules, result):
        """Atomically retire a fixed stop proved not to have reached I/O.

        A tombstone consumes the original CID and prevents a later sender in
        another process from reserving or claiming it. Any ambiguity preserves
        the ordinary unresolved-terminal fence.
        """
        from ..journal import OrderJournal
        if (result.state != 'REJECTED' or result.stage not in
                ('preparation_not_sent','final_gate_not_sent')
                or result.exchange_order_id is not None or result.executed_qty != '0'
                or request.account != self.account or not request.reduce_only
                or request.whole_position or request.trigger_price is None):
            return False
        try:
            action = rules.compile(request, account=self.account)
            key = self._key(request.client_id)
        except (AdapterError, ValueError, TypeError):
            return False
        if action.get('type') != 'stop':return False
        digest = models.sha256_hex(request.to_dict())
        def commit(c):
            row=c.execute('SELECT i.*,g.account_id,g.market_key FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?',
                          (request.intent_id,)).fetchone()
            if row is None:return False
            intent=OrderJournal.decorate(c,row)
            fields=('client_id','market','side','qty_base','price','policy','epoch','trigger_price','whole_position')
            actual=dict(intent,market=row['market_key'])
            if (row['account_id'] != request.account or row['state'] != 'DISPATCHING'
                    or intent.get('protection_kind') != 'fixed_quantity'
                    or any(actual.get(k) != getattr(request,k) for k in fields)
                    or bool(row['reduce_only']) != request.reduce_only):return False
            exchange=c.execute('SELECT * FROM exchange_orders WHERE account=? AND client_id=? AND intent_id=?',
                               (self.account,request.client_id,request.intent_id)).fetchone()
            if (exchange is None or exchange['exchange_order_id'] is not None
                    or exchange['terminal_state'] is not None or exchange['raw_receipt_json'] is not None
                    or exchange['cumulative_qty'] != '0'):
                return False
            if c.execute('SELECT 1 FROM runtime_state WHERE key=?',
                         ('fixed-stop-terminal-unverified:'+request.intent_id,)).fetchone():return False
            old=self._load(c,key)
            if old is None:
                if result.stage != 'preparation_not_sent':return False
                old=dict(account=self.account,owner=self.owner,network=self.network,
                    client_id=request.client_id,intent_id=request.intent_id,request_hash=digest,action=action)
            else:
                if (old.get('account')!=self.account or old.get('owner')!=self.owner
                        or old.get('network')!=self.network or old.get('client_id')!=request.client_id
                        or old.get('intent_id')!=request.intent_id or old.get('request_hash')!=digest
                        or old.get('action')!=action or old.get('state') not in ('ALLOCATED','PREPARED')
                        or any(k in old for k in ('receipt','native_evidence','terminal_resolution'))):return False
                if result.stage == 'final_gate_not_sent' and not (
                        old['state']=='PREPARED' and old.get('last_send_known_not_sent') is True):return False
            native_id=old.get('order_id')
            if c.execute('SELECT 1 FROM executions WHERE account=? AND (order_client_id=? OR (? IS NOT NULL AND exchange_order_id=?)) LIMIT 1',
                         (self.account,request.client_id,native_id,native_id)).fetchone():return False
            old['state']='ABORTED_NOT_SENT'
            self._put(c,key,old)
            proof=dict(schema='bulk-fixed-not-sent-v1',native_key=key,account=self.account,
                       intent_id=request.intent_id,client_id=request.client_id,grid_id=row['grid_id'],
                       request=request.to_dict(),request_hash=digest,action_hash=models.sha256_hex(action),
                       native_order_id=native_id,stage=result.stage)
            self._put(c,'fixed-stop-not-sent:'+request.intent_id,proof)
            c.execute("UPDATE order_intents SET state='REJECTED',updated_ts=? WHERE intent_id=? AND state='DISPATCHING'",
                      (models.utc_now_iso(),request.intent_id))
            return True
        return await self.store.write(commit)

    async def defer_not_sent(self, request, result):
        """Recheck the original native record and defer in one transaction.

        claim_send also checks the core intent, so an older sender cannot
        publish after this transaction has returned the intent to PLANNED.
        None means the proof was insufficient; it does not mean unsent.
        """
        if (result.state!='REJECTED' or result.stage not in
                ('preparation_not_sent','final_gate_not_sent') or request.account!=self.account
                or result.exchange_order_id is not None or result.executed_qty!='0'):
            return None
        key=self._key(request.client_id);digest=models.sha256_hex(request.to_dict())
        def commit(c):
            from ..journal import OrderJournal
            row=c.execute('SELECT i.*,g.account_id,g.market_key,g.epoch current_epoch,g.status,g.desired_mode '
                          'FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?',
                          (request.intent_id,)).fetchone()
            if row is None or row['state']!='DISPATCHING' or row['account_id']!=self.account:return None
            intent=OrderJournal.decorate(c,row);actual=dict(intent,market=row['market_key'])
            fields=('client_id','market','side','qty_base','price','policy','epoch','trigger_price','whole_position')
            if (any(actual.get(k,False if k=='whole_position' else None)!=getattr(request,k) for k in fields)
                    or bool(row['reduce_only'])!=request.reduce_only):return None
            exchange=c.execute('SELECT * FROM exchange_orders WHERE account=? AND client_id=? AND intent_id=?',
                               (self.account,request.client_id,request.intent_id)).fetchone()
            if (exchange is None or exchange['exchange_order_id'] is not None or exchange['terminal_state'] is not None
                    or exchange['raw_receipt_json'] is not None or exchange['cumulative_qty']!='0'):return None
            old=self._load(c,key)
            if old is None:
                if result.stage!='preparation_not_sent':return None
                native_id=None
            else:
                if (old.get('account')!=self.account or old.get('owner')!=self.owner or old.get('network')!=self.network
                        or old.get('intent_id')!=request.intent_id or old.get('client_id')!=request.client_id
                        or old.get('request_hash')!=digest or old.get('state') not in ('ALLOCATED','PREPARED')
                        or any(k in old for k in ('receipt','native_evidence','terminal_resolution'))):return None
                if result.stage=='final_gate_not_sent' and not (
                        old['state']=='PREPARED' and old.get('last_send_known_not_sent') is True):return None
                native_id=old.get('order_id')
            if c.execute('SELECT 1 FROM executions WHERE account=? AND (order_client_id=? OR (? IS NOT NULL AND exchange_order_id=?)) LIMIT 1',
                         (self.account,request.client_id,native_id,native_id)).fetchone():return None
            current=(row['epoch']==row['current_epoch'] and row['desired_mode']=='ACTIVE'
                     and row['status'] in ('STARTING','RUNNING'))
            c.execute('UPDATE order_intents SET state=?,updated_ts=? WHERE intent_id=?',
                      ('PLANNED' if current else 'EXPIRED_EPOCH',models.utc_now_iso(),request.intent_id))
            return current
        return await self.store.write(commit)

    async def save_submission(self, client_id, result):
        from dataclasses import asdict
        key = self._key(client_id)
        def write(c):
            row = self._load(c, key)
            if row is None or row['account']!=self.account or row['state']!='MAY_HAVE_SENT':
                raise AdapterError('BULK_SUBMISSION_RECEIPT_CONFLICT')
            if result.exchange_order_id is not None and result.exchange_order_id!=row['order_id']:
                raise AdapterError('BULK_SUBMISSION_RECEIPT_CONFLICT')
            row['receipt'] = asdict(result)
            if result.state in ('ACCEPTED','REJECTED'): row['state'] = 'ACK_'+result.state
            self._put(c,key,row)
        await self.store.write(write)

    async def mark_not_sent(self, client_id):
        # Only the transport's final fence may prove this, before opening I/O.
        key = self._key(client_id)
        def write(c):
            row = self._load(c,key)
            if row is None or row['account']!=self.account or row['state']!='MAY_HAVE_SENT':
                raise AdapterError('BULK_NOT_SENT_STATE_CONFLICT')
            row['state']='PREPARED';row['last_send_known_not_sent']=True
            self._put(c,key,row)
        await self.store.write(write)

    async def reserve_cancel(self, *, request_id, client_id, order_id, symbol):
        self._key(request_id); original = self.get(client_id)
        if (original is None or original['account']!=self.account or original.get('order_id')!=order_id
                or original['action']['symbol']!=symbol or 'history_start_slot' not in original):
            raise AdapterError('BULK_ORIGINAL_ORDER_BINDING_REQUIRED')
        key='bulk-cancel:'+self.scope+':'+request_id
        fields=dict(account=self.account, client_id=client_id, order_id=order_id, symbol=symbol)
        def write(c):
            row=self._load(c,key)
            if row:
                if any(row[k]!=v for k,v in fields.items()):raise AdapterError('BULK_CANCEL_REQUEST_CONFLICT')
                return row
            counter='bulk-nonce:'+self.scope
            last=self._load(c,counter)
            nonce=uint(max(uint(self.clock_ns()),(uint(last) if last is not None else 0)+1))
            row={**fields,'nonce':nonce,'state':'ALLOCATED','history_start_slot':original['history_start_slot']}
            self._put(c,counter,nonce);self._put(c,key,row)
            return row
        return await self.store.write(write)

    async def supersede_cancel(self, request_id):
        """Re-allocate the nonce of a cancel whose send outcome was lost.

        Only legal when the caller has JUST proven the original order still
        open: an applied cancel makes the order terminal, so the earlier
        unknown send cannot have taken effect. A fresh nonce keeps request
        identity unique; a duplicate cancel of an already-terminal order is
        rejected harmlessly and the following lookup proves the outcome.
        Without this, one lost cancel send poisoned the deterministic
        request id forever: every retry returned BULK_CANCEL_ORIGINAL_PENDING
        without sending, and the residual entry order could never be
        cancelled (2026-11-04, g000000000186 stuck MAY_HAVE_SENT).
        """
        key='bulk-cancel:'+self.scope+':'+request_id
        def write(c):
            row=self._load(c,key)
            if row is None or row['account']!=self.account:
                raise AdapterError('BULK_CANCEL_MUST_BE_QUERIED')
            if row['state']!='MAY_HAVE_SENT' or row.get('acknowledged'):
                raise AdapterError('BULK_CANCEL_MUST_BE_QUERIED')
            counter='bulk-nonce:'+self.scope
            last=self._load(c,counter)
            nonce=uint(max(uint(self.clock_ns()),(uint(last) if last is not None else 0)+1))
            row['nonce']=nonce;row['state']='ALLOCATED'
            row['superseded']=int(row.get('superseded') or 0)+1
            self._put(c,counter,nonce);self._put(c,key,row)
            return row
        return await self.store.write(write)

    async def claim_cancel(self, request_id):
        key='bulk-cancel:'+self.scope+':'+request_id
        def write(c):
            row=self._load(c,key)
            if row is None or row['account']!=self.account or row['state']!='ALLOCATED':
                raise AdapterError('BULK_CANCEL_MUST_BE_QUERIED')
            row['state']='MAY_HAVE_SENT';self._put(c,key,row)
        await self.store.write(write)

    async def cancel_not_sent(self, request_id):
        key='bulk-cancel:'+self.scope+':'+request_id
        def write(c):
            row=self._load(c,key)
            if row is None or row['account']!=self.account or row['state']!='MAY_HAVE_SENT':
                raise AdapterError('BULK_NOT_SENT_STATE_CONFLICT')
            row['state']='ALLOCATED';self._put(c,key,row)
        await self.store.write(write)

    async def record_cancel(self, request_id, *, client_id, acknowledged=False, terminal=None):
        if terminal is not None and terminal not in ('FILLED','CANCELED','REJECTED'):
            raise AdapterError('BULK_CANCEL_TERMINAL_INVALID')
        key='bulk-cancel:'+self.scope+':'+request_id
        def write(c):
            row=self._load(c,key)
            if row is None:return  # already terminal before a cancel was issued
            if row['account']!=self.account or row['client_id']!=client_id:
                raise AdapterError('BULK_CANCEL_REQUEST_CONFLICT')
            row['acknowledged']=bool(row.get('acknowledged')) or bool(acknowledged)
            if terminal is not None:
                row.update(state='ORIGINAL_TERMINAL',original_terminal=terminal)
            self._put(c,key,row)
        await self.store.write(write)

    async def attach(self, request, prepared, protocol):
        """Persist public prepare output; this cannot publish a transaction."""
        key = self._key(request.client_id)
        oid = prepared['order_id']; base58(oid)
        if protocol.owner != self.owner or protocol.network != self.network:
            raise AdapterError('BULK_PREPARED_SCOPE_INVALID')
        def commit(c):
            old = self._load(c, key)
            if (old is None or old['account'] != self.account or request.account != self.account
                    or old['request_hash'] != models.sha256_hex(request.to_dict())
                    or old['nonce'] != prepared['nonce'] or prepared['account'] != self.owner
                    or prepared['signer'] != self.owner):
                raise AdapterError('BULK_PREPARED_BINDING_CONFLICT')
            expected = protocol.prepare(old['action'], old['nonce'])
            if any(expected[k] != prepared[k] for k in ('actions', 'nonce', 'account', 'signer', 'order_id', 'message_bytes')):
                raise AdapterError('BULK_PREPARED_BINDING_CONFLICT')
            if old['state'] != 'ALLOCATED':
                if old.get('order_id') != oid:
                    raise AdapterError('BULK_PREPARED_BINDING_CONFLICT')
                return old
            native_key = 'bulk-order:'+self.scope+':'+oid
            bound = self._load(c, native_key)
            if bound is not None and bound != request.client_id:
                raise AdapterError('BULK_NATIVE_ID_COLLISION')
            # No secret or signature persisted. Original prepare bytes are
            # retained for exact later signing; signed action JSON must match.
            message = bytes(prepared['message_bytes'])
            if not message.endswith(old['nonce'].to_bytes(8, 'little')+base58(self.owner)+bytes([1 if self.network == 'mainnet' else 2])):
                raise AdapterError('BULK_PREPARED_SCOPE_INVALID')
            old.update(state='PREPARED', order_id=oid, message_hex=message.hex(),
                       native_actions=prepared['actions'])
            self._put(c, native_key, request.client_id); self._put(c, key, old)
            return old
        return await self.store.write(commit)

    async def claim_send(self, client_id):
        """Reserve the single send attempt *before* I/O. No HTTP performed.

        After a crash or timeout, subsequent callers must query this same
        original native identity; a fresh nonce is never allocated implicitly.
        """
        key = self._key(client_id)
        def commit(c):
            old = self._load(c, key)
            if old is None or old['account'] != self.account or old['state'] != 'PREPARED':
                raise AdapterError('BULK_ORIGINAL_REQUEST_MUST_BE_QUERIED')
            core=c.execute('SELECT i.*,g.account_id,g.market_key,g.epoch current_epoch, '
                           'g.status grid_status,g.desired_mode FROM order_intents i '
                           'JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?',
                           (old['intent_id'],)).fetchone()
            if core is None and self.require_core:
                raise AdapterError('BULK_CORE_INTENT_REQUIRED')
            if core is not None and (core['state']!='DISPATCHING' or core['client_id']!=client_id
                                     or core['account_id']!=self.account):
                raise AdapterError('BULK_CORE_INTENT_NOT_DISPATCHING')
            if core is not None:
                closing=core['cause_key'].startswith('exit-flat:') and bool(core['reduce_only'])
                if (core['epoch']!=core['current_epoch'] or
                        (closing and core['grid_status']!='STOPPING') or
                        (not closing and (core['desired_mode']!='ACTIVE' or core['grid_status'] not in ('STARTING','RUNNING')))):
                    raise AdapterError('BULK_CORE_CONTROL_CHANGED')
                from ..journal import OrderJournal
                from .base import OrderRequest
                intent=OrderJournal.decorate(c,core)
                req=OrderRequest(core['intent_id'],core['client_id'],core['account_id'],core['market_key'],
                    core['side'],core['qty_base'],core['price'],core['policy'],bool(core['reduce_only']),core['epoch'],
                    trigger_price=intent.get('trigger_price'),whole_position=bool(intent.get('whole_position')))
                if old['request_hash']!=models.sha256_hex(req.to_dict()):
                    raise AdapterError('BULK_CORE_INTENT_CONFLICT')
            if core is not None and not core['reduce_only']:
                if OrderJournal.pending_account_fixed_terminals(c,self.account):
                    raise AdapterError('PROTECTION_CUMULATIVE_UNVERIFIED')
            old['state'] = 'MAY_HAVE_SENT';old['last_send_known_not_sent']=False
            self._put(c, key, old)
            return old
        return await self.store.write(commit)

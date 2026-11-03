"""Hyperliquid main perpetuals GridVenue; live qualification stays false.

Read paths need the ECONOMIC account address, never the agent address.
Writes use exact string wire values and the official SDK for signing.
Transport and signer are injected so raw-response tests never use keys or
networks. History coverage needs an independently certified retention floor;
fetching a short page alone cannot prove that older retained fills exist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import aiohttp
import certifi

from .. import models
from ..store import Store
from .base import (AccountSnapshot, AdapterError, CancelResult, ExchangeOrder,
                   FundingPage, FundingRecord, GridVenue, HistoryPage, LookupResult,
                   OrderRequest, SubmitResult, VenueEvent)

SDK_VERSION = "0.24.0"
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
CLOID = re.compile(r"0x[0-9a-f]{32}\Z")
ENDPOINTS = {
    "mainnet": ("https://api.hyperliquid.xyz", "wss://api.hyperliquid.xyz/ws"),
    "testnet": ("https://api.hyperliquid-testnet.xyz", "wss://api.hyperliquid-testnet.xyz/ws"),
}
FILL_LIMIT = 2000
FILL_RETENTION = 10000
FUNDING_LIMIT = 500  # conservative split threshold, not a coverage assertion
SUBMIT_PREPARATION_TIMEOUT_SEC = 8


def _stream_close_reason(reason):
    """Closed vocabulary only; never persist peer text or subscription data."""
    if not isinstance(reason, str) or not reason:
        return 'unspecified'
    value = reason.lower()
    for label, words in (('rate_limit', ('rate limit', 'too many')),
                         ('heartbeat', ('ping', 'pong', 'heartbeat')),
                         ('idle', ('idle',)), ('shutdown', ('shutdown', 'restart')),
                         ('lifetime', ('lifetime', 'max age'))):
        if any(word in value for word in words):
            return label
    return 'other'


CANCELED_STATUSES = frozenset(("canceled", "marginCanceled", "vaultWithdrawalCanceled",
    "openInterestCapCanceled", "selfTradeCanceled", "reduceOnlyCanceled", "siblingFilledCanceled",
    "delistedCanceled", "liquidatedCanceled", "scheduledCancel", "rejected", "tickRejected",
    "minTradeNtlRejected", "perpMarginRejected", "reduceOnlyRejected", "badAloPxRejected",
    "iocCancelRejected", "badTriggerPxRejected", "marketOrderNoLiquidityRejected",
    "positionIncreaseAtOpenInterestCapRejected", "positionFlipAtOpenInterestCapRejected",
    "tooAggressiveAtOpenInterestCapRejected", "openInterestIncreaseRejected",
    "insufficientSpotBalanceRejected", "oracleRejected", "perpMaxPositionRejected"))


def _address(value: str) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise AdapterError("HL_ACCOUNT_ADDRESS_INVALID")
    return value.lower()


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise AdapterError(f"HL_DECIMAL_STRING_REQUIRED:{field}")
    try:
        return models.parse_decimal_text(value.strip(), field)
    except models.GridModelError as error:
        raise AdapterError(f"HL_DECIMAL_INVALID:{field}") from error


def _id(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AdapterError(f"HL_ID_INVALID:{field}")
    text = str(value)
    if not text.isdigit():
        raise AdapterError(f"HL_ID_INVALID:{field}")
    return text


def _ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError("HL_TIMESTAMP_INVALID")
    return value


def _iso(ms: int) -> str:
    return models.format_iso_ts(datetime.fromtimestamp(_ms(ms) / 1000, timezone.utc))


def _cursor(value: Optional[str], default: Optional[int]) -> int:
    if value is None:
        if default is None:
            raise AdapterError("HL_HISTORY_START_REQUIRED")
        return _ms(default)
    if value.startswith("hl-ms:") and value[6:].isdigit():
        return int(value[6:])
    try:
        return int(models.parse_iso_ts(value, "history_cursor").timestamp() * 1000)
    except (ValueError, models.GridModelError) as error:
        raise AdapterError("HL_HISTORY_CURSOR_INVALID") from error


class HyperliquidTransport:
    """Only public official URLs; the caller owns the aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, network: str = "mainnet"):
        if network not in ENDPOINTS:
            raise AdapterError("HL_NETWORK_UNSUPPORTED")
        self.session = session
        self.base_url, self.ws_url = ENDPOINTS[network]
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def post(self, path: str, payload: dict) -> Any:
        if path not in ("/info", "/exchange"):
            raise AdapterError("HL_PATH_UNSUPPORTED")
        from ..hl_rate import info_weight, shared_read_budget
        budget = shared_read_budget(self.base_url)
        if path == "/info":
            await budget.acquire(info_weight(payload.get("type")))
        # No queueing or automatic resend of signed actions. In particular,
        # never let an expired signed envelope reach the native endpoint.
        if path == "/exchange" and payload.get("expiresAfter") is not None:
            if int(payload["expiresAfter"]) <= int(time.time() * 1000):
                raise AdapterError("HL_REQUEST_EXPIRED")
        try:
            async with self.session.post(self.base_url + path, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10), ssl=self.ssl_context) as response:
                if response.status != 200:
                    if response.status == 429:
                        budget.rate_limited(source='http:' + str(payload.get('type', path)))
                    raise AdapterError(f"HL_HTTP_{response.status}")
                # Never convert source decimal strings or large integer ids to float.
                body = json.loads(await response.text(), parse_float=Decimal)
                if path == "/info":
                    budget.charge_response(payload.get("type"), body)
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise AdapterError("HL_TRANSPORT_UNCERTAIN") from error

    async def prepare_read_capacity(self, weight=350):
        from ..hl_rate import shared_read_budget
        await shared_read_budget(self.base_url).wait_capacity(weight)

    def read_sequence(self, weight=350):
        from ..hl_rate import shared_read_budget
        return shared_read_budget(self.base_url).read_sequence(weight)

    async def market_info(self, kind, **fields):
        from ..hl_ws_market import shared_market_socket
        try:
            return await shared_market_socket(self.session,self.ws_url,self.ssl_context).info(kind,**fields)
        except AdapterError as error:
            if str(error)=='HL_WS_MARKET_RATE_LIMITED':
                from ..hl_rate import shared_read_budget
                budget = shared_read_budget(self.base_url)
                if budget.last_rate_limit is None or budget.blocked_until <= budget.clock():
                    budget.rate_limited(source='ws:market')
            raise

    async def authorization_info(self,kind,**fields):
        from ..hl_ws_market import shared_authorization_socket
        try:
            return await shared_authorization_socket(self.session,self.ws_url,self.ssl_context).info(kind,**fields)
        except AdapterError as error:
            if str(error)=='HL_WS_MARKET_RATE_LIMITED':
                from ..hl_rate import shared_read_budget
                budget = shared_read_budget(self.base_url)
                if budget.last_rate_limit is None or budget.blocked_until <= budget.clock():
                    budget.rate_limited(source='ws:authorization')
                raise
            if str(error) not in ('HL_WS_MARKET_UNAVAILABLE','HL_WS_MARKET_RESPONSE_ERROR'):
                raise
        return await self.post('/info',{'type':kind,**fields})

    async def authorization_roles(self,owner,signer):
        from ..hl_ws_market import shared_authorization_socket
        try:
            return await shared_authorization_socket(self.session,self.ws_url,self.ssl_context).roles(owner,signer)
        except AdapterError as error:
            if str(error)=='HL_WS_MARKET_RATE_LIMITED':
                from ..hl_rate import shared_read_budget
                budget = shared_read_budget(self.base_url)
                if budget.last_rate_limit is None or budget.blocked_until <= budget.clock():
                    budget.rate_limited(source='ws:authorization_roles')
                raise
            if str(error) not in ('HL_WS_MARKET_UNAVAILABLE','HL_WS_MARKET_RESPONSE_ERROR'):
                raise
        return await asyncio.gather(self.post('/info',{'type':'userRole','user':owner}),
                                    self.post('/info',{'type':'userRole','user':signer}))

    def account_read_active(self):
        from ..hl_rate import shared_read_budget
        reservation = shared_read_budget(self.base_url).current_reservation()
        return bool(reservation and reservation.critical)

    async def account_infos(self, requests):
        from ..hl_ws_market import shared_account_socket
        from ..hl_rate import shared_read_budget
        if not 1 <= len(requests) <= 128:
            raise AdapterError('HL_WS_ACCOUNT_READ_ONLY')
        socket = shared_account_socket(self.session, self.ws_url, self.ssl_context)
        # Validate every request before any network access, including later batches.
        for kind, fields in requests:
            socket.validate(kind, fields)
        results = []
        for offset in range(0, len(requests), 4):
            batch = requests[offset:offset + 4]
            budget = shared_read_budget(self.base_url)
            if budget.clock() < budget.blocked_until:
                raise AdapterError('HL_WS_MARKET_RATE_LIMITED')
            try:
                results.extend(await socket.exchange(batch))
                continue
            except AdapterError as error:
                if str(error) == 'HL_WS_MARKET_RATE_LIMITED':
                    budget = shared_read_budget(self.base_url)
                    if budget.last_rate_limit is None or budget.blocked_until <= budget.clock():
                        budget.rate_limited(source='ws:account')
                    raise
                if str(error) not in ('HL_WS_MARKET_UNAVAILABLE', 'HL_WS_MARKET_RESPONSE_ERROR'):
                    raise
            # Only this failed read batch falls back; all original HTTP pacing applies.
            results.extend(await asyncio.gather(*(self.post('/info', {'type': kind, **fields})
                                                   for kind, fields in batch)))
        return results

    async def account_info(self, kind, **fields):
        return (await self.account_infos([(kind, fields)]))[0]

    def public_read_sequence(self, weight=160):
        from ..hl_rate import shared_read_budget
        return shared_read_budget(self.base_url).read_sequence(weight, critical=False)

    def execution_read_sequence(self, weight=350):
        from ..hl_rate import shared_read_budget
        return shared_read_budget(self.base_url).execution_read_sequence(weight)

    def read_capacity_available(self, weight=350):
        from ..hl_rate import shared_read_budget
        return shared_read_budget(self.base_url).capacity_available(weight)

    def public_read_capacity_available(self, weight=350):
        from ..hl_rate import shared_read_budget
        return shared_read_budget(self.base_url).capacity_available(weight,critical=False)


class HyperliquidSigner:
    """Official signing with a durable high-water nonce and process lease.

    The shared lock directory must also be used by every writer of this
    signer. Legacy worker integration is a separate pending qualification.
    Keys are passed as a wallet; this class never loads .env or credentials.
    """

    def __init__(self, wallet: Any, store: Store, *, network: str,
                 lock_dir: str, vault_address: Optional[str] = None,
                 clock_ms=None):
        import fcntl
        import hashlib
        import os
        from pathlib import Path
        if network not in ENDPOINTS:
            raise AdapterError("HL_NETWORK_UNSUPPORTED")
        self.address = _address(wallet.address)
        self.vault_address = _address(vault_address) if vault_address else None
        self.network, self.wallet, self.store = network, wallet, store
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.scope = f"hl:{network}:{self.address}"
        self._lock = asyncio.Lock()
        Path(lock_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lease = open(Path(lock_dir) / (hashlib.sha256(self.scope.encode()).hexdigest() + ".lock"), "a+")
        os.chmod(self._lease.name, 0o600)
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._lease.close()
            raise AdapterError("HL_SIGNER_IN_USE") from error

    async def sign(self, action: dict, expires_after: Optional[int], *, include_action_nonce=False) -> dict:
        from importlib.metadata import version
        from hyperliquid.utils.signing import sign_l1_action
        if version("hyperliquid-python-sdk") != SDK_VERSION:
            raise AdapterError("HL_SDK_VERSION_UNVERIFIED")
        if self._lease.closed:
            raise AdapterError("HL_SIGNER_CLOSED")
        if include_action_nonce and (action.get("type") != "agentSendAsset" or "nonce" in action):
            raise AdapterError("HL_ACTION_NONCE_CONTEXT_INVALID")
        async with self._lock:
            now = _ms(self.clock_ms())
            key = f"nonce:{self.scope}"
            # Also retain the watermark in the shared lease, across store changes.
            self._lease.seek(0)
            shared = self._lease.read().strip()
            shared_nonce = int(shared) if shared else 0
            def reserve(c):
                row = c.execute("SELECT value FROM counters WHERE name=?", (key,)).fetchone()
                nonce = max(now, shared_nonce + 1, int(row[0]) + 1 if row else 0)
                c.execute("INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (key, nonce))
                return nonce
            nonce = await self.store.write(reserve)
            if include_action_nonce:
                action = {**action, "nonce": nonce}
            import os
            self._lease.seek(0)
            self._lease.truncate()
            self._lease.write(str(nonce))
            self._lease.flush()
            os.fsync(self._lease.fileno())
            signature = sign_l1_action(self.wallet, action, self.vault_address,
                                      nonce, expires_after, self.network == "mainnet")
            return {"action": action, "nonce": nonce, "signature": signature,
                    "vaultAddress": self.vault_address, "expiresAfter": expires_after}

    def close(self):
        self._lease.close()


@dataclass
class HyperliquidStreamState:
    connected: bool = False
    generation: int = 0
    private_revision: int = 0
    gap: bool = True


class HyperliquidStreamMixin:
    @property
    def stream_connected(self):
        return self._stream_state.connected

    @stream_connected.setter
    def stream_connected(self, value):
        self._stream_state.connected = value

    @property
    def stream_generation(self):
        return self._stream_state.generation

    @stream_generation.setter
    def stream_generation(self, value):
        self._stream_state.generation = value

    @property
    def private_revision(self):
        return self._stream_state.private_revision

    @private_revision.setter
    def private_revision(self, value):
        self._stream_state.private_revision = value

    @property
    def stream_gap(self):
        return self._stream_state.gap

    @stream_gap.setter
    def stream_gap(self, value):
        self._stream_state.gap = value


class HyperliquidGridVenue(HyperliquidStreamMixin, GridVenue):
    venue = "hl"
    client_id_kind = "hex128"
    live_qualified = False
    risk_model = None

    def __init__(self, transport: Any, *, account_address: str, symbol: str = "BTC",
                 network: str = "mainnet", signer=None, write_enabled: bool = False,
                 history_start_ms: Optional[int] = None,
                 retention_floor_ms: Optional[int] = None,
                 funding_retention_floor_ms: Optional[int] = None,
                 account_mode_confirmed: bool = False, clock_ms=None, stream_state=None):
        if network not in ENDPOINTS or not re.fullmatch(r"[A-Za-z0-9]{1,24}", symbol):
            raise AdapterError("HL_MAIN_PERP_ONLY")
        self.transport, self.network, self.symbol = transport, network, symbol
        self.owner = _address(account_address)
        self.deployment = "" if network == "mainnet" else "testnet"
        self.market = models.MarketIdentity(self.deployment, symbol, "", "linear_perp", "USDC")
        account_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"grid:hl:{network}:{self.owner}"))
        self._account = models.AccountIdentity(account_id, "hl", self.deployment, self.owner,
                                               fixture=False, credential_revision="observer")
        self.signer, self.write_enabled = signer, write_enabled
        if signer and getattr(signer, "network", network) != network:
            raise AdapterError("HL_SIGNER_NETWORK_MISMATCH")
        if signer and getattr(signer, "vault_address", None) and signer.vault_address != self.owner:
            raise AdapterError("HL_VAULT_ACCOUNT_MISMATCH")
        self.history_start_ms = history_start_ms
        self.retention_floor_ms = retention_floor_ms
        self.funding_retention_floor_ms = funding_retention_floor_ms
        self.account_mode_confirmed = account_mode_confirmed
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.rules = None
        self._position_stops = {}
        self.asset_id = None
        self.size_decimals = None
        self.max_leverage = None
        self.price_history = []
        self._events = []
        self._oid_to_client = {}
        self._stream_state = stream_state or HyperliquidStreamState()
        self.stream_clock = time.monotonic
        self.account_parent = None
        self.credential_ref = "hl-observer"
        self.control_store = None
        self.authorization_check = None
        self.dispatch_gate = None
        self._controls = None

    async def configure_leverage(self, *, leverage, margin_mode, request_id, gate):
        if self.control_store is None:
            return {"state": "BLOCKED", "code": "CONTROL_STORE_REQUIRED", "request_id": request_id}
        if self._controls is None:
            from ..hl_controls import HyperliquidControls
            self._controls = HyperliquidControls(self, self.control_store)
        return await self._controls.leverage(leverage=leverage, margin_mode=margin_mode, request_id=request_id, gate=gate)

    async def _info(self, kind: str, **fields):
        from ..hl_ws_market import ACCOUNT_INFO_KINDS
        if kind in ACCOUNT_INFO_KINDS and self._account_read_active():
            return await self.transport.account_info(kind, **fields)
        return await self.transport.post("/info", {"type": kind, **fields})

    def _account_read_active(self):
        return bool(hasattr(self.transport, 'account_infos')
                    and hasattr(self.transport, 'account_read_active')
                    and self.transport.account_read_active())

    async def _account_infos(self, requests):
        if self._account_read_active():
            return await self.transport.account_infos(requests)
        return await asyncio.gather(*(self._info(kind, **fields) for kind, fields in requests))

    async def resolve_account(self):
        return self._account

    def price_tick(self, price: Decimal) -> Decimal:
        """A safe tick at a price: 5 significant figures / 6-szDecimals.

        Integer prices remain valid regardless of significant figures. For a
        range, use its largest price; all lower lines share this safe tick.
        """
        if self.size_decimals is None or not price.is_finite() or price <= 0:
            raise AdapterError("HL_PRICE_PRECISION_UNAVAILABLE")
        fractional = Decimal(10) ** -(6 - self.size_decimals)
        significant = min(Decimal(1), Decimal(10) ** (price.adjusted() - 4))
        return max(fractional, significant)

    def grid_rules(self, lower_price: str, upper_price: str):
        if self.rules is None:
            raise AdapterError("HL_RULES_UNAVAILABLE")
        tick = models.canonical_decimal_text(self.price_tick(_decimal(upper_price, "upper_price")))
        revision = models.sha256_hex({"instrument_revision": self.rules.revision,
                                      "price_policy": "hl-grid-safe-tick-v1", "tick": tick})
        return replace(self.rules, tick=tick, revision=revision,
                       unknown_reasons=tuple(r for r in self.rules.unknown_reasons if r != "DYNAMIC_PRICE_PRECISION"))

    def protected_ioc_price(self, price: Decimal, side: str) -> Decimal:
        # Buy limit rounds down, sell limit rounds up, preserving the caller's
        # slippage boundary. Re-evaluate if rounding crosses a decade.
        tick = self.price_tick(price)
        rounded = (price / tick).to_integral_value(rounding="ROUND_FLOOR" if side == "buy" else "ROUND_CEILING") * tick
        return rounded

    async def load_instrument(self, market_ref: str):
        if hasattr(self.transport, 'public_read_sequence'):
            async with self.transport.public_read_sequence(40):
                return await self._load_instrument(market_ref)
        return await self._load_instrument(market_ref)

    async def _load_instrument(self, market_ref: str):
        if market_ref != self.market.market_key:
            raise AdapterError("HL_MARKET_SCOPE_MISMATCH")
        if self._account_read_active():
            meta, fees = await self._account_infos([('meta', {}), ('userFees', {'user': self.owner})])
        else:
            meta, fees = await asyncio.gather(self._info("meta"), self._info("userFees", user=self.owner))
        universe = meta.get("universe") if isinstance(meta, dict) else None
        if not isinstance(universe, list):
            raise AdapterError("HL_META_MALFORMED")
        for index, row in enumerate(universe):
            if not isinstance(row, dict):
                raise AdapterError("HL_META_MALFORMED")
            if row.get("name") == self.symbol:
                if row.get("isDelisted"):
                    raise AdapterError("HL_MARKET_DELISTED")
                decimals = models.parse_strict_int(row["szDecimals"], "szDecimals", minimum=0, maximum=6)
                self.max_leverage = models.parse_strict_int(row["maxLeverage"], "maxLeverage", minimum=1)
                self.asset_id, self.size_decimals = index, decimals
                if not isinstance(fees, dict) or not all(key in fees for key in ("userAddRate", "userCrossRate")):
                    raise AdapterError("HL_ACCOUNT_FEES_UNAVAILABLE")
                tiers = next((t[1].get("marginTiers", []) for t in meta.get("marginTables", [])
                              if t[0] == row.get("marginTableId")), [])
                table_id = row.get("marginTableId")
                # Official margin-table IDs below 50 encode a single tier
                # whose maximum leverage equals the ID; not every such table
                # is expanded in the meta response.
                if not tiers and isinstance(table_id, int) and not isinstance(table_id, bool) and 0 < table_id < 50:
                    tiers = [{"lowerBound": "0", "maxLeverage": table_id}]
                native = {"instrument": row, "tiers": tiers,
                          "maker": fees.get("userAddRate"), "taker": fees.get("userCrossRate")}
                self.rules = models.InstrumentRules(
                    market=market_ref, native_id=self.symbol, product_type="linear_perp",
                    base_asset=self.symbol, settlement_asset="USDC",
                    tick=models.canonical_decimal_text(Decimal(10) ** -(6 - decimals)),
                    qty_step=models.canonical_decimal_text(Decimal(10) ** -decimals),
                    min_qty=models.canonical_decimal_text(Decimal(10) ** -decimals),
                    min_notional="10", max_orders=None, multiplier="1",
                    maker_fee_rate=models.canonical_decimal_text(_decimal(fees["userAddRate"], "maker_fee")),
                    taker_fee_rate=models.canonical_decimal_text(_decimal(fees["userCrossRate"], "taker_fee")),
                    maintenance_tiers=tuple({k: str(v) for k, v in t.items()} for t in tiers),
                    revision=models.sha256_hex(native), source="hl:meta+userFees",
                    unknown_reasons=("DYNAMIC_PRICE_PRECISION", "RISK_MODEL_UNVERIFIED", "OPEN_ORDER_LIMIT_UNVERIFIED"))
                return self.rules
        raise AdapterError("HL_MARKET_NOT_FOUND")

    async def position_snapshot(self):
        """Fresh main-DEX quantity for reduce-only protection, never margin authority.

        Full account/risk observations still collect abstraction, orders and
        all collateral scopes. This intentionally exposes no available assets
        or risk evidence, so it cannot qualify an opening order.
        """
        requested_ms = _ms(self.clock_ms())
        data = await self._info("clearinghouseState", user=self.owner)
        if not isinstance(data, dict) or not isinstance(data.get("assetPositions"), list):
            raise AdapterError("HL_ACCOUNT_SNAPSHOT_MALFORMED")
        timestamp = _ms(data.get("time"))
        now = _ms(self.clock_ms())
        if timestamp > now or now - timestamp > 5000:
            logging.getLogger('grid.hyperliquid').warning(
                'HL_POSITION_SNAPSHOT_STALE age_ms=%s roundtrip_ms=%s',
                now - timestamp, now - requested_ms)
            raise AdapterError("HL_POSITION_SNAPSHOT_STALE")
        qty, mode, seen = Decimal(0), None, set()
        for item in data["assetPositions"]:
            pos = item.get("position") if isinstance(item, dict) else None
            coin = pos.get("coin") if isinstance(pos, dict) else None
            if not isinstance(coin, str) or not coin or coin in seen:
                raise AdapterError("HL_ACCOUNT_SNAPSHOT_MALFORMED")
            seen.add(coin)
            value = _decimal(pos.get("szi"), "position")
            if coin == self.symbol:
                leverage = pos.get("leverage")
                mode = leverage.get("type") if isinstance(leverage, dict) else None
                if mode not in ("cross", "isolated"):
                    raise AdapterError("HL_ACCOUNT_SNAPSHOT_MALFORMED")
                qty = value
        return AccountSnapshot(self._account.account_id, _iso(timestamp), [], mode,
            models.canonical_decimal_text(qty), source="hl:protection-position-only")

    async def account_snapshot(self):
        batch = None
        if self.account_mode_confirmed and self._account_read_active():
            batch = await self._account_infos([
                ('clearinghouseState', {'user': self.owner}),
                ('userAbstraction', {'user': self.owner}),
                ('activeAssetData', {'user': self.owner, 'coin': self.symbol}),
                ('frontendOpenOrders', {'user': self.owner})])
        data = batch[0] if batch is not None else await self._info("clearinghouseState", user=self.owner)
        if not isinstance(data, dict) or not isinstance(data.get("assetPositions"), list):
            raise AdapterError("HL_ACCOUNT_SNAPSHOT_MALFORMED")
        qty, mode = Decimal(0), None
        for item in data["assetPositions"]:
            pos = item["position"]
            if pos["coin"] == self.symbol:
                qty += _decimal(pos["szi"], "position")
                mode = pos["leverage"]["type"]
        # Confirmation is only permission to query, never a cached assertion
        # of the account's current abstraction or available trading margin.
        assets, evidence = [], None
        if self.account_mode_confirmed:
            abstraction, active, orders = batch[1:] if batch is not None else await asyncio.gather(
                self._info("userAbstraction", user=self.owner),
                self._info("activeAssetData", user=self.owner, coin=self.symbol),
                self._info("frontendOpenOrders", user=self.owner))
            if not isinstance(active, dict) or active.get("coin") != self.symbol or _address(active.get("user")) != self.owner:
                raise AdapterError("HL_ACCOUNT_SCOPE_MISMATCH")
            standard = abstraction in ("default", "disabled")
            amounts = active.get("availableToTrade")
            if not isinstance(amounts, list) or len(amounts) != 2:
                raise AdapterError("HL_AVAILABLE_MARGIN_UNVERIFIED")
            available = min(_decimal(v, "availableToTrade") for v in amounts)
            available = max(Decimal(0), min(available, _decimal(data["withdrawable"], "withdrawable")))
            if standard:
                assets = [("USDC", models.canonical_decimal_text(available))]
            mode = active["leverage"]["type"]
            evidence = {"abstraction": abstraction, "leverage": active["leverage"],
                        "max_trade_sizes": active.get("maxTradeSzs"), "open_orders": len(orders),
                        "has_positions": any(_decimal(p["position"]["szi"], "position") != 0 for p in data["assetPositions"]),
                        "positions": [dict(p["position"]) for p in data["assetPositions"]],
                        "order_rows": orders, "cross_summary": data.get("crossMarginSummary")}
            if abstraction == "unifiedAccount":
                from ..hl_unified import unified_main_balance
                free, summary, scope = await unified_main_balance(self, data, orders)
                available = min(free, max(Decimal(0), min(_decimal(v, "availableToTrade") for v in amounts)))
                assets = [("USDC", models.canonical_decimal_text(available))]
                evidence.update(cross_summary=summary, unified_scope=scope)
        return AccountSnapshot(self._account.account_id, _iso(data["time"]), assets,
                               mode, models.canonical_decimal_text(qty), source="hl:clearinghouseState+activeAssetData" if evidence else "hl:clearinghouseState", evidence=evidence)

    def risk_model_for(self, config, lines, snapshot, account_snapshot, *, owned_context=None, portfolio_context=None):
        from ..hl_risk import HyperliquidRangeRisk, HyperliquidOwnedRisk, owned_account_capacity, portfolio_account_capacity
        evidence = account_snapshot.evidence
        if not evidence or not account_snapshot.assets:
            return None, None
        if config.margin_mode != "cross":
            raise models.GridModelError("HL_STANDARD_CROSS_ONLY", "真实保证金预览当前仅支持标准全仓账户")
        if config.leverage > self.max_leverage:
            raise models.GridModelError("HL_LEVERAGE_LIMIT", "填写杠杆超过该币种交易所上限")
        if owned_context is None and portfolio_context is None and (evidence["has_positions"] or evidence["open_orders"]):
            raise models.GridModelError("HL_ACCOUNT_NOT_CLEAN", "真实规模预览需要无现有订单与仓位的网格专用账户")
        if not self.rules.maintenance_tiers:
            raise models.GridModelError("HL_MARGIN_TIERS_UNAVAILABLE", "无法取得该币种保证金档位，暂不能计算数量")
        prices = tuple(lines["levels"])
        protection_prices = []
        if config.protection is not None:
            if config.protection.lower_stop is not None:
                protection_prices.append(models.canonical_decimal_text(self.protected_ioc_price(Decimal(config.protection.lower_stop) * Decimal('.98'), 'sell')))
            if config.protection.upper_stop is not None:
                protection_prices.append(models.canonical_decimal_text(self.protected_ioc_price(Decimal(config.protection.upper_stop) * Decimal('1.02'), 'buy')))
        high = max(Decimal(snapshot.mark), *map(Decimal, prices), *map(Decimal, protection_prices))
        model = HyperliquidRangeRisk(self.rules.qty_step, prices, snapshot.mark, config.grid_count,
                                    config.leverage, self.rules.taker_fee_rate, self.rules.maintenance_tiers,
                                    stress_prices=tuple(protection_prices))
        total = config.capital.total
        portfolio_capacity = None
        if portfolio_context is not None:
            portfolio_capacity = portfolio_account_capacity(config, account_snapshot, portfolio_context)
            remaining = max(Decimal(0), Decimal(portfolio_capacity) - Decimal(portfolio_context["other_reserved"]))
            model = replace(model, account_remaining=models.canonical_decimal_text(remaining))
        if owned_context is not None:
            capacity = (model.account_remaining if portfolio_capacity is not None else
                        owned_account_capacity(config, account_snapshot, owned_context, self.symbol))
            model = HyperliquidOwnedRisk(self.rules.qty_step, prices, snapshot.mark, config.grid_count,
                config.leverage, self.rules.taker_fee_rate, self.rules.maintenance_tiers,
                context=owned_context, direction=config.direction, initial_position=config.initial_position,
                account_remaining=capacity, stress_prices=tuple(protection_prices))
            total = model.total_budget
        cap = Decimal(total) * config.leverage / (config.grid_count * high)
        sizes = evidence["max_trade_sizes"]
        if not isinstance(sizes, list) or len(sizes) != 2:
            raise models.GridModelError("HL_SIZE_LIMIT_UNVERIFIED", "交易所可下单数量未知，暂不能计算规模")
        cap = min(cap, min(_decimal(v, "maxTradeSzs") for v in sizes) / config.grid_count)
        if cap <= 0:
            raise models.GridModelError("HL_NO_TRADE_CAPACITY", "账户当前没有可用下单额度")
        return model, models.canonical_decimal_text(cap)

    def owned_margin_capacity(self, config, snapshot, context):
        from ..hl_risk import owned_account_capacity
        return owned_account_capacity(config, snapshot, context, self.symbol)

    async def candles(self, interval='15m'):
        from ..candles import request_window, normalize
        end = int(self.clock_ms())
        start, end = request_window(interval, end)
        async def read():
            rows = await self._info('candleSnapshot', req=dict(
                coin=self.symbol, interval=interval, startTime=start, endTime=end))
            return normalize(rows, market=self.market.market_key, symbol=self.symbol,
                             interval=interval, start_ms=start, end_ms=end)
        if hasattr(self.transport, 'public_read_sequence'):
            async with self.transport.public_read_sequence(23):
                return await read()
        return await read()

    async def market_snapshot(self):
        if hasattr(self.transport, 'public_read_sequence'):
            async with self.transport.public_read_sequence(22):
                return await self._market_snapshot()
        return await self._market_snapshot()

    async def execution_market_snapshot(self):
        if hasattr(self.transport,'market_info'):
            try:
                quote = await self._market_snapshot(info=self.transport.market_info)
                age = self.clock_ms() - int(models.parse_iso_ts(quote.ts, 'execution_quote').timestamp() * 1000)
                if -1000 <= age <= 5000:
                    return quote
                # A scoped WS response can still contain an old native book.
                # Obtain one wholly new REST observation; never freshen the
                # old timestamp or reuse its mark to authorize a send.
                logging.getLogger('grid.hyperliquid').warning(
                    'HL_EXECUTION_WS_QUOTE_STALE age_ms=%s fallback=rest', age)
            except AdapterError as error:
                if str(error) not in ('HL_WS_MARKET_UNAVAILABLE','HL_WS_MARKET_RESPONSE_ERROR'):
                    raise
        if hasattr(self.transport,'read_sequence'):
            async with self.transport.read_sequence(22):
                return await self.market_snapshot()
        return await self.market_snapshot()

    async def _market_snapshot(self, *, info=None):
        info = info or self._info
        meta_ctx, book = await asyncio.gather(info("metaAndAssetCtxs"), info("l2Book", coin=self.symbol))
        try:
            index = next(i for i, item in enumerate(meta_ctx[0]["universe"]) if item["name"] == self.symbol)
            mark = models.canonical_decimal_text(_decimal(meta_ctx[1][index]["markPx"], "mark"))
            if book["coin"] != self.symbol:
                raise AdapterError("HL_BOOK_SCOPE_MISMATCH")
            bid = models.canonical_decimal_text(_decimal(book["levels"][0][0]["px"], "bid")) if book["levels"][0] else None
            ask = models.canonical_decimal_text(_decimal(book["levels"][1][0]["px"], "ask")) if book["levels"][1] else None
            stamp = _iso(book["time"])
        except (KeyError, IndexError, TypeError, StopIteration) as error:
            raise AdapterError("HL_MARKET_SNAPSHOT_MALFORMED") from error
        self.price_history.append({"ts": stamp, "price": mark})
        self.price_history = self.price_history[-500:]
        return models.MarketSnapshot(market=self.market.market_key, ts=stamp, source="hl:info", mark=mark, bid=bid, ask=ask)

    def _wire(self, request: OrderRequest, *, allow_small_full_close=False):
        if request.account != self._account.account_id or request.market != self.market.market_key:
            raise AdapterError("HL_ORDER_SCOPE_MISMATCH")
        if not CLOID.fullmatch(request.client_id):
            raise AdapterError("HL_CLOID_INVALID")
        if request.side not in ("buy", "sell") or request.policy not in ("post_only", "gtc", "ioc", "reduce_only_ioc"):
            raise AdapterError("HL_ORDER_POLICY_INVALID")
        if self.rules is None or self.asset_id is None:
            raise AdapterError("HL_RULES_NOT_LOADED")
        qty, px = _decimal(request.qty_base, "qty"), _decimal(request.price, "price")
        if qty <= 0 or px <= 0 or qty % Decimal(self.rules.qty_step):
            raise AdapterError("HL_SIZE_INVALID")
        normalized = px.normalize()
        if px != px.to_integral_value() and (len(normalized.as_tuple().digits) > 5 or -normalized.as_tuple().exponent > 6 - self.size_decimals):
            raise AdapterError("HL_PRICE_PRECISION_INVALID")
        if qty * px < Decimal(self.rules.min_notional) and not (
                allow_small_full_close and request.reduce_only and request.policy == 'reduce_only_ioc'):
            raise AdapterError("HL_MIN_NOTIONAL")
        if request.policy == "reduce_only_ioc" and not request.reduce_only:
            raise AdapterError("HL_REDUCE_ONLY_REQUIRED")
        tif = {"post_only": "Alo", "gtc": "Gtc", "ioc": "Ioc", "reduce_only_ioc": "Ioc"}[request.policy]
        return {"a": self.asset_id, "b": request.side == "buy", "p": models.canonical_decimal_text(px),
                "s": models.canonical_decimal_text(qty), "r": request.reduce_only,
                "t": {"limit": {"tif": tif}}, "c": request.client_id}

    def validate_resting_candidates(self, candidates, *, epoch, policy):
        """Pure wire validation, including surviving old-version exit orders."""
        for candidate in candidates:
            self._wire(OrderRequest(intent_id="preview", client_id="0x" + "00" * 16,
                account=self._account.account_id, market=self.market.market_key, side=candidate["side"],
                qty_base=candidate["qty_base"], price=candidate["price"], policy=policy,
                reduce_only=bool(candidate.get("reduce_only", candidate.get("purpose") == "initial_exit")), epoch=epoch))

    def stop_loss_wire(self, order: OrderRequest, trigger_price: str, *, allow_small_full_close=False):
        """Pure native fixed-size reduce-only market stop validation.

        This builder alone grants no submission permission or qualification.
        Caller must prove inventory/parent scope and observe native coverage.
        """
        if not order.reduce_only or order.policy != "reduce_only_ioc":
            raise AdapterError("HL_STOP_REDUCE_ONLY_REQUIRED")
        wire = self._wire(order, allow_small_full_close=allow_small_full_close)
        trigger = _decimal(trigger_price, "trigger_price")
        if trigger <= 0:
            raise AdapterError("HL_TRIGGER_PRICE_INVALID")
        normalized = trigger.normalize()
        if trigger != trigger.to_integral_value() and (len(normalized.as_tuple().digits) > 5
                or -normalized.as_tuple().exponent > 6 - self.size_decimals):
            raise AdapterError("HL_TRIGGER_PRICE_PRECISION_INVALID")
        wire["t"] = {"trigger": {"isMarket": True,
            "triggerPx": models.canonical_decimal_text(trigger), "tpsl": "sl"}}
        return wire

    def position_stop_wire(self, order: OrderRequest, trigger_price: str, *, allow_small_full_close=False):
        """Encode a full-position stop for bounded protocol commissioning.

        The request quantity is a positive commissioning bound, validated by
        the fixed-size builder. Native s=0 semantics still require independent
        exchange evidence; this method grants no dispatch/live qualification.
        """
        wire = self.stop_loss_wire(order, trigger_price, allow_small_full_close=allow_small_full_close)
        wire["s"] = "0"
        return wire

    def register_position_stop(self, client_id, *, side, qty_bound, price, trigger_price):
        if not CLOID.fullmatch(client_id) or side not in ('buy', 'sell'):
            raise AdapterError('HL_POSITION_STOP_BINDING_INVALID')
        values = {'side': side, 'qty_bound': models.canonical_decimal_text(_decimal(qty_bound, 'qty_bound')),
                  'price': models.canonical_decimal_text(_decimal(price, 'price')),
                  'trigger_price': models.canonical_decimal_text(_decimal(trigger_price, 'trigger_price'))}
        if any(Decimal(values[key]) <= 0 for key in ('qty_bound', 'price', 'trigger_price')):
            raise AdapterError('HL_POSITION_STOP_BINDING_INVALID')
        previous = self._position_stops.get(client_id)
        if previous is not None and previous != values:
            raise AdapterError('HL_POSITION_STOP_BINDING_CONFLICT')
        self._position_stops[client_id] = values

    def _validate_position_stop(self, row, *, require_binding=False, terminal=None):
        cid = row.get('cloid')
        bound = self._position_stops.get(cid)
        # After activation the venue replaces the trigger with a market
        # child under the SAME oid/cloid. Its price and size are rewritten.
        # This is terminal evidence only, never evidence of live protection.
        triggered = (terminal in ('filled', *CANCELED_STATUSES)
                     and row.get('isTrigger') is False
                     and row.get('triggerCondition') == 'Triggered'
                     and _decimal(row.get('triggerPx'), 'trigger_price') == 0
                     and _decimal(row.get('origSz'), 'origSz') > 0)
        if (row.get('isPositionTpsl') is not True or row.get('reduceOnly') is not True
                or (not triggered and row.get('isTrigger') is not True) or row.get('orderType') != 'Stop Market'
                or row.get('side') not in ('A', 'B') or not cid or not CLOID.fullmatch(cid)
                or (not triggered and _decimal(row.get('triggerPx'), 'trigger_price') <= 0)
                or _decimal(row.get('limitPx'), 'price') <= 0
                or _decimal(row.get('origSz'), 'origSz') < 0 or _decimal(row.get('sz'), 'remaining') < 0
                or triggered and (_decimal(row['sz'], 'remaining') > _decimal(row['origSz'], 'origSz')
                                  or terminal == 'filled' and _decimal(row['sz'], 'remaining') != 0)):
            raise AdapterError('HL_POSITION_STOP_MALFORMED')
        if require_binding and not bound:
            raise AdapterError('HL_POSITION_STOP_UNBOUND')
        if bound and (('buy' if row['side'] == 'B' else 'sell') != bound['side']
                or not triggered and _decimal(row['limitPx'], 'price') != Decimal(bound['price'])
                or not triggered and _decimal(row['triggerPx'], 'trigger_price') != Decimal(bound['trigger_price'])
                or _decimal(row['origSz'], 'origSz') > Decimal(bound['qty_bound'])
                or _decimal(row['sz'], 'remaining') > Decimal(bound['qty_bound'])):
            raise AdapterError('HL_POSITION_STOP_SCOPE_MISMATCH')

    async def _prepare_submission(self, order: OrderRequest):
        trial = bool(getattr(self, 'trial_submit_gate', None) and await self.trial_submit_gate(order))
        if (not self.write_enabled and not trial) or self.signer is None:
            return SubmitResult("REJECTED", reject_code="REAL_DISPATCH_DISABLED", stage="local_gate")
        small_full_close = False
        try:
            if order.whole_position:
                if order.trigger_price is None:
                    raise AdapterError('HL_TRIGGER_PRICE_REQUIRED')
                wire = self.position_stop_wire(order, order.trigger_price)
                self.register_position_stop(order.client_id, side=order.side, qty_bound=order.qty_base,
                    price=order.price, trigger_price=order.trigger_price)
            else:
                if order.trigger_price is not None:
                    raise AdapterError('HL_TRIGGER_TYPE_UNSUPPORTED')
                wire = self._wire(order)
        except AdapterError as error:
            small_gate = getattr(self, 'small_close_gate', None)
            if (str(error) != 'HL_MIN_NOTIONAL' or not small_gate
                    or not await small_gate(order)):
                return SubmitResult("REJECTED", reject_code=str(error), stage="local_validation")
            if order.whole_position:
                wire = self.position_stop_wire(order, order.trigger_price, allow_small_full_close=True)
                self.register_position_stop(order.client_id, side=order.side, qty_bound=order.qty_base,
                    price=order.price, trigger_price=order.trigger_price)
            else:
                wire = self._wire(order, allow_small_full_close=True)
            small_full_close = True
        if order.request_deadline_sec is not None:
            deadline = _ms(order.request_deadline_sec) * 1000
        else:
            deadline = self.clock_ms() + 10000
        if deadline <= self.clock_ms():
            return SubmitResult("REJECTED", reject_code="REQUEST_EXPIRED", stage="local_gate")
        if self.authorization_check:
            try:
                await self.authorization_check()
            except Exception as error:
                if isinstance(error, AdapterError) and str(error) == 'HL_INFO_BUDGET_WAIT':
                    return SubmitResult('REJECTED', reject_code='RISK_API_BUDGET_WAIT', stage='final_gate_not_sent')
                return SubmitResult("REJECTED", reject_code="HL_AUTHORIZATION_UNVERIFIED", stage="authorization")
        # Signing failures are known not-sent; transport failures are uncertain.
        try:
            payload = await self.signer.sign({"type": "order", "orders": [wire],
                "grouping": "positionTpsl" if order.whole_position else "na"}, deadline)
        except (AdapterError, ImportError, ValueError, OSError):
            return SubmitResult("REJECTED", reject_code="SIGNING_NOT_SENT", stage="signing")
        if self.dispatch_gate:
            refuse = await self.dispatch_gate(order)
            if refuse:
                return SubmitResult("REJECTED", reject_code=refuse, stage="final_gate_not_sent")
        if small_full_close and not await self.small_close_gate(order):
            return SubmitResult('REJECTED', reject_code='HL_SMALL_CLOSE_UNVERIFIED', stage='final_gate_not_sent')
        if order.policy == 'post_only':
            try:
                book = await self._info('l2Book', coin=self.symbol)
                if book.get('coin') != self.symbol or not -1000 <= self.clock_ms() - book['time'] < 5000:
                    raise AdapterError('HL_BOOK_UNAVAILABLE')
                rows = book['levels'][1 if order.side == 'buy' else 0]
                best = _decimal(rows[0]['px'], 'best_price')
                if best <= 0:
                    raise AdapterError('HL_BOOK_UNAVAILABLE')
            except Exception:
                return SubmitResult('REJECTED', reject_code='POST_ONLY_BOOK_WAIT', stage='final_gate_not_sent')
            if Decimal(order.price) >= best if order.side == 'buy' else Decimal(order.price) <= best:
                return SubmitResult('REJECTED', reject_code='POST_ONLY_WAIT', stage='final_gate_not_sent')
            if self.dispatch_gate:
                refuse = await self.dispatch_gate(order)
                if refuse:
                    return SubmitResult("REJECTED", reject_code=refuse, stage="final_gate_not_sent")
        if self.clock_ms() >= deadline:
            return SubmitResult("REJECTED", reject_code="REQUEST_EXPIRED", stage="final_gate_not_sent")
        return payload

    async def submit(self, order: OrderRequest):
        # Keep all awaited authorization/signing/final gates inside a
        # separate deadline. Only the subsequent transport can be uncertain.
        # Cancellation of the caller still propagates normally.
        try:
            payload = await asyncio.wait_for(self._prepare_submission(order), SUBMIT_PREPARATION_TIMEOUT_SEC)
        except TimeoutError:
            return SubmitResult('REJECTED', reject_code='PREPARATION_TIMEOUT_NOT_SENT', stage='preparation_not_sent')
        if isinstance(payload, SubmitResult):
            return payload
        try:
            body = await self.transport.post("/exchange", payload)
            if not isinstance(body, dict):
                return SubmitResult("UNKNOWN", stage="malformed_ack")
            if body.get("status") == "err":
                return SubmitResult("REJECTED", reject_code="VENUE_REJECTED", stage="exchange", detail=str(body.get("response")))
            if body.get("status") != "ok" or body["response"]["type"] != "order":
                return SubmitResult("UNKNOWN", stage="malformed_ack")
            statuses = body["response"]["data"]["statuses"]
            if len(statuses) != 1:
                return SubmitResult("UNKNOWN", stage="malformed_ack")
            status = statuses[0]
            if order.whole_position and status == 'waitingForTrigger':
                proof = await self.lookup(order.client_id)
                if proof.state == 'FOUND_OPEN':
                    return SubmitResult('ACCEPTED', exchange_order_id=proof.exchange_order_id, stage='trigger_query')
                return SubmitResult('UNKNOWN', stage='trigger_ack_unresolved')
            if "error" in status:
                message = status['error'].lower()
                code = ("POST_ONLY_CROSS" if "post only" in message or "post-only" in message
                        else "HL_MIN_NOTIONAL" if "order must have minimum value" in message
                        else "VENUE_REJECTED")
                return SubmitResult("REJECTED", reject_code=code, stage="exchange", detail=status["error"])
            if "resting" in status:
                oid = _id(status["resting"]["oid"], "oid")
                self._oid_to_client[oid] = order.client_id
                return SubmitResult("ACCEPTED", exchange_order_id=oid, stage="resting")
            if "filled" in status:
                filled = status["filled"]
                oid = _id(filled["oid"], "oid")
                self._oid_to_client[oid] = order.client_id
                return SubmitResult("ACCEPTED", exchange_order_id=oid,
                                    executed_qty=models.canonical_decimal_text(_decimal(filled["totalSz"], "totalSz")), stage="filled_ack")
        except (AdapterError, OSError, TimeoutError, KeyError, TypeError, ValueError, AttributeError):
            return SubmitResult("UNKNOWN", stage="transport_or_ack")
        return SubmitResult("UNKNOWN", stage="unrecognized_ack")

    async def lookup(self, client_id: str, exchange_order_id: Optional[str] = None):
        if not CLOID.fullmatch(client_id):
            raise AdapterError("HL_CLOID_INVALID")
        body = await self._info("orderStatus", user=self.owner, oid=client_id)
        if not isinstance(body, dict):
            return LookupResult("UNKNOWN")
        if body.get("status") == "unknownOid":
            return LookupResult("NOT_FOUND_TRANSIENT")  # never proof of absence
        if body.get("status") != "order":
            return LookupResult("UNKNOWN")
        outer = body.get("order")
        row = outer.get("order") if isinstance(outer, dict) else None
        if not isinstance(row, dict) or not all(key in row for key in ("oid", "origSz", "sz")):
            return LookupResult("UNKNOWN")
        if row.get("coin") != self.symbol or row.get("cloid") != client_id:
            raise AdapterError("HL_ORDER_QUERY_SCOPE_MISMATCH")
        oid = _id(row["oid"], "oid")
        self._oid_to_client[oid] = client_id
        if exchange_order_id is not None and oid != exchange_order_id:
            raise AdapterError("HL_ORDER_ID_MISMATCH")
        original = _decimal(row["origSz"], "origSz")
        remaining = _decimal(row["sz"], "remaining")
        full_position = client_id in self._position_stops or original == 0
        economic_terminal = False
        if full_position:
            self._validate_position_stop(row, require_binding=True, terminal=outer.get('status'))
            economic_terminal = row.get('isTrigger') is False
            # A triggered terminal child has an actual economic origSz;
            # compare it to history, never use it to manufacture executions.
            cumulative = original - remaining if economic_terminal else Decimal(0)
        else:
            cumulative = original - remaining
        if not full_position and (original <= 0 or remaining < 0 or cumulative < 0):
            raise AdapterError("HL_CUMULATIVE_INVALID")
        common = {"exchange_order_id": oid, "cumulative_qty": models.canonical_decimal_text(cumulative),
                  'cumulative_known': not full_position or economic_terminal}
        state = outer.get("status")
        if isinstance(state, str):
            common.update(native_status=state,
                native_status_ms=_ms(outer['statusTimestamp']) if 'statusTimestamp' in outer else None)
        if state == "open":
            return LookupResult("FOUND_OPEN", **common)
        if state == "filled":
            return LookupResult("FOUND_TERMINAL", terminal_state="FILLED", **common)
        if state in CANCELED_STATUSES:
            return LookupResult("FOUND_TERMINAL", terminal_state="CANCELED", **common)
        return LookupResult("UNKNOWN", **common, detail=f"unclassified status:{state}")

    async def cancel(self, client_id: str, exchange_order_id: Optional[str] = None):
        trial = bool(getattr(self, 'trial_cancel_gate', None) and await self.trial_cancel_gate(client_id))
        if (not self.write_enabled and not trial) or self.signer is None:
            return CancelResult("UNKNOWN", detail="REAL_DISPATCH_DISABLED")
        if not CLOID.fullmatch(client_id) or self.asset_id is None:
            raise AdapterError("HL_CANCEL_SCOPE_INVALID")
        try:
            if self.authorization_check:
                await self.authorization_check()
            payload = await self.signer.sign({"type": "cancelByCloid", "cancels": [{"asset": self.asset_id, "cloid": client_id}]}, self.clock_ms() + 10000)
            body = await self.transport.post("/exchange", payload)
            if body.get("status") != "ok":
                return CancelResult("UNKNOWN", detail="cancel acknowledgement unconfirmed")
            statuses = body.get("response", {}).get("data", {}).get("statuses", [])
            acknowledged = body.get("response", {}).get("type") == "cancel" and statuses == ["success"]
            result = await self.lookup(client_id, exchange_order_id)
            if result.state == "FOUND_TERMINAL":
                return CancelResult("TERMINAL", final_state=result.terminal_state)
            return CancelResult("SUBMITTED" if acknowledged else "UNKNOWN")
        except (AdapterError, OSError, TimeoutError, KeyError, TypeError, ValueError, ImportError, AttributeError):
            return CancelResult("UNKNOWN")

    async def cancel_batch(self, orders):
        """One authorized action, then independent native proof for every ID.

        Only the executor supplies these already journaled owned IDs. A lost
        acknowledgement never resends the batch; callers reconcile each ID.
        Whole-position protection deliberately stays on the single path.
        """
        if (not 2 <= len(orders) <= 20 or len({cid for cid, _ in orders}) != len(orders)
                or any(not isinstance(cid, str) or not CLOID.fullmatch(cid) for cid, _ in orders)
                or any(cid in self._position_stops for cid, _ in orders)
                or self.asset_id is None):
            raise AdapterError('HL_CANCEL_SCOPE_INVALID')
        results = {cid: CancelResult('UNKNOWN') for cid, _ in orders}
        if self.signer is None or not self.write_enabled:
            return results
        # Narrow trial-only writers keep the existing single-order path;
        # this method requires the account's ordinary write permission.
        try:
            if self.authorization_check:
                await self.authorization_check()
            payload = await self.signer.sign({'type': 'cancelByCloid', 'cancels': [
                {'asset': self.asset_id, 'cloid': cid} for cid, _ in orders]}, self.clock_ms() + 10000)
            await self.transport.post('/exchange', payload)
        except (AdapterError, OSError, TimeoutError, KeyError, TypeError, ValueError, ImportError, AttributeError):
            # Signed result unknown: query the ORIGINAL identities below.
            pass
        for cid, oid in orders:
            try:
                proof = await self.lookup(cid, oid)
                if proof.state == 'FOUND_TERMINAL' and proof.terminal_state in ('FILLED', 'CANCELED'):
                    results[cid] = CancelResult('TERMINAL', final_state=proof.terminal_state)
            except (AdapterError, OSError, TimeoutError, KeyError, TypeError, ValueError, AttributeError):
                pass
        return results

    def _open_order(self, row: dict):
        original = _decimal(row["origSz"], "origSz")
        remaining = _decimal(row["sz"], "remaining")
        full_position = row.get('cloid') in self._position_stops or original == 0
        if full_position:
            self._validate_position_stop(row)
        cumulative = Decimal(0) if full_position else original - remaining
        if not full_position and (original <= 0 or remaining < 0 or cumulative < 0 or row.get("side") not in ("A", "B") or not isinstance(row.get("reduceOnly"), bool)):
            raise AdapterError("HL_OPEN_ORDER_MALFORMED")
        cid = row.get("cloid")
        if cid is not None and not CLOID.fullmatch(cid):
            raise AdapterError("HL_CLOID_INVALID")
        if cid:
            self._oid_to_client[_id(row["oid"], "oid")] = cid
        return ExchangeOrder(cid, _id(row["oid"], "oid"), "buy" if row["side"] == "B" else "sell",
            models.canonical_decimal_text(original), models.canonical_decimal_text(_decimal(row["limitPx"], "price")),
            models.canonical_decimal_text(cumulative), row["reduceOnly"], "rest_query",
            whole_position=full_position, trigger_price=row.get('triggerPx') if full_position else None,
            cumulative_known=not full_position)

    async def open_orders(self):
        rows = await self._info("frontendOpenOrders", user=self.owner)
        if not isinstance(rows, list):
            raise AdapterError("HL_OPEN_ORDERS_MALFORMED")
        return [self._open_order(row) for row in rows if row.get("coin") == self.symbol]

    def normalize_fill(self, row: dict, *, source: str):
        if row.get("coin") != self.symbol or row.get("side") not in ("A", "B"):
            raise AdapterError("HL_FILL_SCOPE_INVALID")
        oid, tid = _id(row["oid"], "oid"), _id(row["tid"], "tid")
        fee_asset = row.get("feeToken")
        if fee_asset != "USDC" or not isinstance(row["crossed"], bool):
            raise AdapterError("HL_FILL_FEE_UNSUPPORTED")
        qty, px = _decimal(row["sz"], "qty"), _decimal(row["px"], "price")
        if qty <= 0 or px <= 0:
            raise AdapterError("HL_FILL_AMOUNT_INVALID")
        cid = row.get("cloid") or self._oid_to_client.get(oid)
        if cid is not None and not CLOID.fullmatch(cid):
            raise AdapterError("HL_CLOID_INVALID")
        return models.ExecutionEvent(
            execution_key=f"hl:{self.network}:{self.owner}:{self.symbol}:{oid}:{tid}",
            account=self._account.account_id, market=self.market.market_key,
            order_client_id=cid, exchange_order_id=oid, trade_id=tid,
            side="buy" if row["side"] == "B" else "sell", qty_base=models.canonical_decimal_text(qty),
            price=models.canonical_decimal_text(px), fee_amount=models.canonical_decimal_text(_decimal(row["fee"], "fee")),
            fee_asset=fee_asset, is_maker=not row["crossed"], exchange_ts=_iso(row["time"]),
            received_ts=_iso(self.clock_ms()), source=source, source_seq=None, finality="final")

    async def _history(self, kind: str, start: int, end: int, limit: int):
        """Bisect saturated inclusive time windows; never skip same-ms fills."""
        if start > end:
            raise AdapterError("HL_HISTORY_WINDOW_INVALID")
        stack, result, requests = [(start, end)], [], 0
        while stack:
            lo, hi = stack.pop()
            requests += 1
            if requests > 128:
                raise AdapterError("HL_HISTORY_REQUEST_BUDGET")
            fields = {"user": self.owner, "startTime": lo, "endTime": hi}
            if kind == "userFillsByTime":
                fields["aggregateByTime"] = False
            rows = await self._info(kind, **fields)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or "time" not in row or not lo <= _ms(row["time"]) <= hi for row in rows):
                raise AdapterError("HL_HISTORY_RESPONSE_INVALID")
            if len(rows) >= limit:
                if lo == hi:
                    raise AdapterError("HL_HISTORY_SAME_MS_TRUNCATED")
                middle = (lo + hi) // 2
                stack.extend([(middle + 1, hi), (lo, middle)])
            else:
                result.extend(rows)
                if kind == "userFillsByTime" and len(result) >= FILL_RETENTION:
                    raise AdapterError("HL_HISTORY_RETENTION_LIMIT")
        return sorted(result, key=lambda row: row["time"])

    async def fills(self, cursor: Optional[str] = None, through: Optional[str] = None):
        start = _cursor(cursor, self.history_start_ms)
        end = _cursor(through, self.clock_ms())
        rows = await self._history("userFillsByTime", start, end, FILL_LIMIT)
        records = [self.normalize_fill(row, source="rest_backfill") for row in rows if row["coin"] == self.symbol]
        # Mixed-side fills at one ms lack a certified economic ordering.
        sides = {}
        for fill in records:
            sides.setdefault(fill.exchange_ts, set()).add(fill.side)
        ordered = not any(len(side) > 1 for side in sides.values())
        covered = self.retention_floor_ms is not None and start >= self.retention_floor_ms and ordered
        return HistoryPage(records=records, next_cursor=f"hl-ms:{end}" if covered else None,
            coverage_start=f"hl-ms:{start}", coverage_through=f"hl-ms:{end}" if covered else None,
            complete=covered, truncated=not covered, source_revision="hl-main-perp-v1")

    async def funding_payments(self, cursor: Optional[str] = None, through: Optional[str] = None):
        page = await self.funding_history(cursor, through)
        if not page.complete or page.truncated:
            raise AdapterError("HL_FUNDING_COVERAGE_UNVERIFIED")
        return list(page.records)

    def history_cursor_from_snapshot(self, snapshot):
        if snapshot.account != self._account.account_id:
            raise AdapterError("HL_ACCOUNT_SCOPE_MISMATCH")
        return f"hl-ms:{_cursor(snapshot.ts, None)}"

    def funding_replay_cursor(self, cursor):
        saved = _cursor(cursor, None)
        floor = self.funding_retention_floor_ms
        if floor is None or saved < floor:
            raise AdapterError("HL_FUNDING_COVERAGE_UNVERIFIED")
        # This catches delayed publication; it is not a substitute for
        # certifying the venue's retention and publication boundaries.
        return f"hl-ms:{max(floor, saved - 120000)}"

    def funding_cursor_rank(self, cursor):
        return _cursor(cursor, None)

    async def funding_history(self, cursor=None, through=None):
        if hasattr(self.transport, 'read_sequence'):
            async with self.transport.read_sequence(100):
                return await self._funding_history(cursor, through)
        return await self._funding_history(cursor, through)

    async def _funding_history(self, cursor=None, through=None):
        start, end = _cursor(cursor, self.history_start_ms), _cursor(through, self.clock_ms())
        if self.funding_retention_floor_ms is None or start < self.funding_retention_floor_ms:
            return FundingPage((), f"hl-ms:{start}", None, complete=False, truncated=True)
        rows = await self._history("userFunding", start, end, FUNDING_LIMIT)
        records = []
        for row in rows:
            delta = row["delta"]
            if delta.get("coin") != self.symbol:
                continue
            if delta.get("type") != "funding" or not isinstance(row.get("hash"), str) or not row["hash"]:
                raise AdapterError("HL_FUNDING_RECORD_INVALID")
            records.append(FundingRecord(
                payment_key=f"hl:{self.network}:{self.owner}:{self.symbol}:{row['time']}:{row['hash']}",
                amount=models.canonical_decimal_text(-_decimal(delta["usdc"], "funding")),
                asset="USDC", ts=_iso(row["time"]), source_seq=f"hl-ms:{row['time']}"))
        return FundingPage(records, f"hl-ms:{start}", f"hl-ms:{end}", complete=True,
                           source_revision="hl-main-perp-funding-v1")

    def ingest_message(self, message: dict):
        if self.account_parent is None and message.get("channel") in ("orderUpdates", "userFills"):
            self.private_revision += 1
        if message.get("channel") == "orderUpdates":
            for update in message["data"]:
                row = update["order"]
                if row.get("coin") == self.symbol and row.get("cloid"):
                    if not CLOID.fullmatch(row["cloid"]):
                        raise AdapterError("HL_CLOID_INVALID")
                    self._oid_to_client[_id(row["oid"], "oid")] = row["cloid"]
            return
        if message.get("channel") != "userFills":
            return
        data = message["data"]
        if _address(data["user"]) != self.owner or not isinstance(data.get("fills"), list):
            raise AdapterError("HL_STREAM_ACCOUNT_MISMATCH")
        for row in data["fills"]:
            if row["coin"] == self.symbol:
                event = self.normalize_fill(row, source="stream")
                if len(self._events) >= 2000:
                    self.stream_gap = True
                    raise AdapterError("HL_STREAM_QUEUE_OVERFLOW")
                self._events.append(VenueEvent("fill", execution=event))

    async def run_stream(self, stop: asyncio.Event):
        """Subscription acknowledgements establish readiness, never coverage.
        JSON heartbeats keep quiet account streams alive (the venue's 60s
        timeout is an application-message timeout, not a WebSocket ping).
        """
        backoff = 1
        while not stop.is_set():
            try:
                async with self.transport.session.ws_connect(self.transport.ws_url,
                        heartbeat=15, ssl=self.transport.ssl_context, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    observer = getattr(self, 'observations', None)
                    if observer is not None:
                        observer.begin(self.stream_generation + 1)
                    await ws.send_json({"method": "subscribe", "subscription": {"type": "userFills", "user": self.owner, "aggregateByTime": False}})
                    await ws.send_json({"method": "subscribe", "subscription": {"type": "orderUpdates", "user": self.owner}})
                    self.stream_connected = False
                    self.stream_gap = True
                    acknowledged = set()
                    opened = last_received = last_ping = self.stream_clock()
                    last_pong = None
                    ping_count = pong_count = 0
                    backoff = 1
                    while not stop.is_set():
                        if observer is not None:
                            for sub in observer.subscriptions():
                                await ws.send_json({'method': 'subscribe', 'subscription': sub})
                        now = self.stream_clock()
                        if not self.stream_connected and now - opened >= 15:
                            raise AdapterError("HL_STREAM_SUBSCRIPTION_TIMEOUT")
                        if now - last_received >= 45:
                            raise AdapterError("HL_STREAM_HEARTBEAT_TIMEOUT")
                        if now - last_ping >= getattr(self, 'stream_ping_interval', 20):
                            await ws.send_json({"method": "ping"})
                            last_ping = now
                            ping_count += 1
                        try:
                            frame = await asyncio.wait_for(ws.receive(), 1)
                        except TimeoutError:
                            continue
                        if frame.type == aiohttp.WSMsgType.TEXT:
                            message = json.loads(frame.data)
                            last_received = self.stream_clock()
                            if message.get('channel') == 'pong':
                                last_pong = last_received
                                pong_count += 1
                            if observer is not None:
                                observer.heartbeat = last_received
                            if message.get("channel") == "error":
                                raise AdapterError("HL_STREAM_SUBSCRIPTION_ERROR")
                            if message.get("channel") == "subscriptionResponse":
                                if observer is not None:
                                    observer.acknowledge(message)
                                data = message["data"]
                                sub = data["subscription"]
                                if (data.get("method") == "subscribe" and sub.get("type") in ("userFills", "orderUpdates")
                                        and _address(sub.get("user")) == self.owner):
                                    acknowledged.add(sub["type"])
                                if len(acknowledged) == 2 and not self.stream_connected:
                                    self.stream_connected = True
                                    self.stream_generation += 1
                            else:
                                self.ingest_message(message)
                        elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            # Record only the protocol code, never the peer's
                            # message/reason body or account subscription data.
                            close_code = frame.data if type(frame.data) is int else getattr(ws, 'close_code', None)
                            if type(close_code) is not int or not 0 <= close_code <= 65535:
                                close_code = None
                            logging.getLogger('grid.hyperliquid').warning(
                                'HL_ACCOUNT_STREAM_INTERRUPTED frame=%s close_code=%s generation=%s '
                                'age_ms=%s receive_age_ms=%s pong_age_ms=%s pings=%s pongs=%s reason_class=%s',
                                frame.type.name, close_code, self.stream_generation,
                                int(max(0, self.stream_clock()-opened)*1000),
                                int(max(0, self.stream_clock()-last_received)*1000),
                                None if last_pong is None else int(max(0, self.stream_clock()-last_pong)*1000),
                                ping_count, pong_count, _stream_close_reason(getattr(frame, 'extra', None)))
                            break
            except (aiohttp.ClientError, AdapterError, TimeoutError, ValueError, KeyError, TypeError) as error:
                # Adapter codes are safe enums. Transport/parser exceptions
                # can contain response bodies, URLs or credentials: type only.
                code = str(error) if isinstance(error, AdapterError) else ''
                reason = code if re.fullmatch(r'HL_[A-Z0-9_]{1,64}', code) else type(error).__name__
                logging.getLogger('grid.hyperliquid').warning(
                    'HL_ACCOUNT_STREAM_INTERRUPTED reason=%s generation=%s', reason, self.stream_generation)
            finally:
                self.stream_connected = False
                self.stream_gap = True
                if getattr(self, 'observations', None) is not None:
                    self.observations.invalidate()
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), backoff)
                except TimeoutError:
                    backoff = min(backoff * 2, 20)

    def poll(self):
        events, self._events = self._events, []
        return events

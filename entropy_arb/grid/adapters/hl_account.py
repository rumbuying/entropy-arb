"""One economic account and signer; independent perpetual market views.

Market selection never changes another view's symbol, rules or event queue.
The account stream, signer, nonce store and execution lock remain shared.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from decimal import Decimal

from .. import models
from .base import AccountSnapshot, AdapterError, GridVenue
from .hyperliquid import (ENDPOINTS, HyperliquidGridVenue,
    HyperliquidStreamMixin, HyperliquidStreamState, _address, _decimal, _iso)


class HyperliquidAccountVenue(HyperliquidStreamMixin, GridVenue):
    venue = "hl"
    client_id_kind = "hex128"
    live_qualified = False
    rules = None
    market = None

    def __init__(self, transport, *, account_address, network="mainnet", clock_ms=None):
        if network not in ENDPOINTS:
            raise AdapterError("HL_NETWORK_INVALID")
        self.transport, self.network = transport, network
        self.owner = _address(account_address)
        self.deployment = "" if network == "mainnet" else "testnet"
        aid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"grid:hl:{network}:{self.owner}"))
        self._account = models.AccountIdentity(aid, "hl", self.deployment, self.owner,
            fixture=False, credential_revision="observer")
        self._stream_state = HyperliquidStreamState()
        self.stream_clock = time.monotonic
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.views = {}
        self.signer = None
        self.write_enabled = False
        self.account_mode_confirmed = False
        self.credential_ref = "hl-observer"
        self.control_store = None
        self.authorization_check = None
        from ..hl_observation import UnifiedObservation
        self.observations = UnifiedObservation(self)
        self.stream_ping_interval = 2

    async def _info(self, kind, **fields):
        return await self.transport.post("/info", {"type": kind, **fields})

    async def resolve_account(self):
        return self._account

    def portfolio_capacity(self, config, snapshot, context):
        from ..hl_risk import portfolio_account_capacity
        return portfolio_account_capacity(config, snapshot, context)

    def market_venue(self, market_ref):
        parts = market_ref.split("|") if isinstance(market_ref, str) else []
        if (len(parts) != 5 or parts[0] != self.deployment or parts[1] != ""
                or parts[3:] != ["linear_perp", "USDC"] or not re.fullmatch(r"[A-Za-z0-9]{1,24}", parts[2])):
            raise AdapterError("HL_MARKET_SCOPE_MISMATCH")
        view = self.views.get(market_ref)
        if view is None:
            view = HyperliquidGridVenue(self.transport, account_address=self.owner,
                symbol=parts[2], network=self.network, clock_ms=self.clock_ms, stream_state=self._stream_state)
            view.account_parent = self
            self.views[market_ref] = view
        # Credentials and service policy belong to the account. Qualification,
        # history boundaries, rules and leverage belong to this market view.
        view._account = self._account
        view.signer, view.write_enabled = self.signer, self.write_enabled
        view.account_mode_confirmed = self.account_mode_confirmed
        view.credential_ref = self.credential_ref
        view.control_store = self.control_store
        view.authorization_check = self.authorization_check
        release = getattr(self, 'live_release', None)
        if release is not None:
            view.live_qualified = bool(release.grant(self._account.account_id, market_ref))
            view.write_enabled = view.live_qualified and os.getenv('GRID_REAL_DISPATCH','0')=='1'
        return view

    async def catalog_markets(self):
        meta = await self._info("meta")
        if not isinstance(meta, dict) or not isinstance(meta.get("universe"), list):
            raise AdapterError("HL_META_MALFORMED")
        markets = []
        for item in meta["universe"]:
            symbol = item.get("name")
            if item.get("isDelisted") or not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9]{1,24}", symbol):
                continue
            market = models.MarketIdentity(self.deployment, symbol, "", "linear_perp", "USDC")
            markets.append({"market_ref": market.market_key, "symbol": symbol,
                "base_asset": symbol, "asset": "USDC", "max_leverage": item.get("maxLeverage")})
        release = getattr(self, 'live_release', None)
        refresh = getattr(release, 'refresh_market_catalog', None)
        if callable(refresh):
            refresh(self._account.account_id, markets)
        return markets

    async def account_snapshot(self):
        state, abstraction = await asyncio.gather(
            self._info("clearinghouseState", user=self.owner), self._info("userAbstraction", user=self.owner))
        return await self.snapshot_for_state(state, abstraction)

    async def snapshot_for_state(self, state, abstraction, orders=None):
        if abstraction != "unifiedAccount":
            return self.snapshot_from_state(state, abstraction)
        from ..hl_unified import unified_main_balance
        if orders is None:
            orders = await self._info("frontendOpenOrders", user=self.owner)
        free, summary, scope = await unified_main_balance(self, state, orders)
        return AccountSnapshot(self._account.account_id, _iso(state["time"]),
            [("USDC", models.canonical_decimal_text(free))], None, None,
            source="hl:unified-spot-cash+main-cross-positions", evidence={"abstraction": abstraction,
                "cross_summary": summary, "unified_scope": scope, "order_rows": orders,
                "positions": [dict(row["position"]) for row in state["assetPositions"]]})

    def snapshot_from_state(self, state, abstraction):
        if not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
            raise AdapterError("HL_ACCOUNT_SNAPSHOT_MALFORMED")
        summary = state.get("crossMarginSummary")
        if not isinstance(summary, dict):
            raise AdapterError("HL_AVAILABLE_MARGIN_UNVERIFIED")
        equity = _decimal(summary["accountValue"], "accountValue")
        used = _decimal(summary["totalMarginUsed"], "totalMarginUsed")
        if used < 0:
            raise AdapterError("HL_AVAILABLE_MARGIN_UNVERIFIED")
        free = max(Decimal(0), min(equity - used, _decimal(state["withdrawable"], "withdrawable")))
        assets = [("USDC", models.canonical_decimal_text(free))] if abstraction in ("disabled", "default") else []
        return AccountSnapshot(self._account.account_id, _iso(state["time"]), assets, None, None,
            source="hl:account-cross-summary", evidence={"abstraction": abstraction, "cross_summary": summary,
                "positions": [dict(row["position"]) for row in state["assetPositions"]]})

    @property
    def pending_account_events(self):
        return any(view._events for view in self.views.values())

    def ingest_message(self, message):
        if self.observations.ingest(message):
            return
        if message.get("channel") in ("orderUpdates", "userFills"):
            self.private_revision += 1
        for view in list(self.views.values()):
            view.ingest_message(message)

    async def run_stream(self, stop):
        await HyperliquidGridVenue.run_stream(self, stop)

    def poll(self):
        return [event for view in self.views.values() for event in view.poll()]

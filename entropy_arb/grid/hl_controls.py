"""Durable desired-state leverage commands; ACK is not effect confirmation.

No automatic retry of a DISPATCHING/UNKNOWN command. A later read can
confirm the desired state; otherwise the command remains unresolved.
Credentials and raw exception strings never enter the control journal.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from . import models, store
from .adapters.base import AdapterError
from .adapters.hyperliquid import _address


class HyperliquidControls:
    def __init__(self, venue, st):
        self.venue, self.store = venue, st
        self._lock = asyncio.Lock()

    async def _active(self):
        v=self.venue
        if hasattr(v.transport,'read_sequence'):
            async with v.transport.read_sequence(20):
                data = await v._info("activeAssetData", user=v.owner, coin=v.symbol)
        else:
            data = await v._info("activeAssetData", user=v.owner, coin=v.symbol)
        if not isinstance(data, dict) or data.get("coin") != self.venue.symbol or _address(data.get("user")) != self.venue.owner:
            raise AdapterError("HL_CONTROL_SCOPE_MISMATCH")
        leverage = data["leverage"]
        value = models.parse_strict_int(leverage["value"], "leverage", minimum=1)
        if leverage["type"] not in ("cross", "isolated"):
            raise AdapterError("HL_MARGIN_MODE_UNVERIFIED")
        return {"value": value, "type": leverage["type"]}

    async def _write(self, key, record):
        def commit(c):
            c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(record, sort_keys=True)))
            store.register_event(c, event_key=f"{key}:{record['state']}", scope=self.venue._account.account_id,
                                 event_type="account_control", payload=record)
        await self.store.write(commit)

    async def leverage(self, *, leverage, margin_mode, request_id, gate):
        async with self._lock:
            return await self._leverage(leverage=leverage, margin_mode=margin_mode, request_id=request_id, gate=gate)

    async def _leverage(self, *, leverage, margin_mode, request_id, gate):
        v = self.venue
        def result(state, code=None):
            return {"state": state, "code": code, "request_id": request_id}
        trial = bool(getattr(v, 'trial_control_gate', None) and await v.trial_control_gate(request_id))
        if ((not v.write_enabled or not v.live_qualified) and not trial) or v.signer is None:
            return result("BLOCKED", "REAL_DISPATCH_DISABLED")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or gate is None:
            return result("BLOCKED", "CONTROL_CONTEXT_REQUIRED")
        leverage = models.parse_strict_int(leverage, "leverage", minimum=1)
        if margin_mode not in ("cross", "isolated"):
            return result("REJECTED", "HL_MARGIN_MODE_UNSUPPORTED")
        if v.rules is None:
            await v.load_instrument(v.market.market_key)
        if not v.rules.maintenance_tiers or leverage > min(v.max_leverage, int(v.rules.maintenance_tiers[0]["maxLeverage"])):
            return result("REJECTED", "HL_LEVERAGE_LIMIT")
        target = {"value": leverage, "type": margin_mode}
        payload = {"account": v._account.account_id, "market": v.market.market_key,
                   "credential_revision": v._account.credential_revision, "target": target}
        request_hash = models.sha256_hex(payload)
        key = f"control:{v._account.account_id}:{models.sha256_hex(request_id)}"
        saved = self.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone())
        record = json.loads(saved[0]) if saved else {"request_hash": request_hash, **payload,
                "request_id": request_id, "state": "PREPARED", "created_ts": models.utc_now_iso()}
        if record["request_hash"] != request_hash:
            return result("BLOCKED", "IDEMPOTENCY_CONFLICT")
        pending = self.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE ? AND key!=?", (f"control:{v._account.account_id}:%", key))])
        if any(r["state"] in ("DISPATCHING", "UNKNOWN") and
               (not isinstance(r.get("expires_after"), int) or r["expires_after"] > v.clock_ms()) for r in pending):
            return result("BLOCKED", "HL_PREVIOUS_CONTROL_IN_FLIGHT")
        try:
            active = await self._active()
        except Exception:
            return result("UNKNOWN" if saved else "BLOCKED", "HL_CONTROL_OBSERVATION_UNAVAILABLE")
        if active == target:
            record["state"] = "CONFIRMED"
            await self._write(key, record)
            return {**result("CONFIRMED"), "unchanged": True}
        if saved:
            # Includes a previous confirmed setting changed outside this
            # operation. Never restore it behind the user's newer intention.
            return result("UNKNOWN" if record["state"] in ("DISPATCHING", "UNKNOWN") else "BLOCKED", "HL_CONTROL_REQUIRES_RECONCILIATION")
        try:
            state, orders = await asyncio.gather(v._info("clearinghouseState", user=v.owner),
                                                v._info("frontendOpenOrders", user=v.owner))
            if not isinstance(orders, list) or not isinstance(state.get("assetPositions"), list):
                raise AdapterError("HL_CONTROL_BASELINE_UNVERIFIED")
            if orders or any(Decimal(p["position"]["szi"]) != 0 for p in state["assetPositions"]):
                return result("BLOCKED", "HL_ACCOUNT_NOT_CLEAN")
            if getattr(v, "authorization_check", None):
                await v.authorization_check()
        except Exception:
            return result("BLOCKED", "HL_CONTROL_AUTHORIZATION_OR_BASELINE_UNVERIFIED")
        if not await gate():
            return result("BLOCKED", "CONTROL_INTERRUPTED")
        record["state"] = "PREPARED"
        await self._write(key, record)
        action = {"type": "updateLeverage", "asset": v.asset_id, "isCross": margin_mode == "cross", "leverage": leverage}
        expiry = v.clock_ms() + 10000
        try:
            signed = await v.signer.sign(action, expiry)
        except Exception:
            record.update(state="NOT_SENT", code="HL_SIGNING_FAILED")
            await self._write(key, record)
            return result("REJECTED", "HL_SIGNING_FAILED")
        record.update(state="DISPATCHING", nonce=signed["nonce"], expires_after=expiry)
        await self._write(key, record)
        if not await gate() or v.clock_ms() >= expiry:
            record.update(state="NOT_SENT", code="CONTROL_INTERRUPTED")
            await self._write(key, record)
            return result("BLOCKED", "CONTROL_INTERRUPTED")
        # All preparation, nonce reservation and the send marker are durable.
        # Cancellation during the network await leaves DISPATCHING for lookup.
        try:
            await v.transport.post("/exchange", signed)
        except Exception:
            pass
        try:
            confirmed = await self._active() == target
        except Exception:
            confirmed = False
        record["state"] = "CONFIRMED" if confirmed else "UNKNOWN"
        await self._write(key, record)
        return result(record["state"], None if confirmed else "HL_CONTROL_EFFECT_UNCONFIRMED")

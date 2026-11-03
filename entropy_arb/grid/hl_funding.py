"""Bounded, journaled self-transfer from spot USDC to main perpetuals.

This does not arm grid orders. The operator must configure a cumulative
funding allowance. Recipient is the proven economic owner, never RPC input.
Unknown sends consume allowance and cannot be resent with a different id.
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

from . import models
from .credentials import CredentialError
from .adapters.base import AdapterError


class PerpetualFunding:
    def __init__(self, runtime, *, allowance="0"):
        self.runtime = runtime
        self.allowance = models.parse_decimal_text(allowance, "funding_allowance")
        if self.allowance < 0:
            raise ValueError("funding allowance must be nonnegative")

    async def _snapshot(self, venue):
        spot, perp, orders, mode = await asyncio.gather(
            venue._info("spotClearinghouseState", user=venue.owner),
            venue._info("clearinghouseState", user=venue.owner),
            venue._info("frontendOpenOrders", user=venue.owner),
            venue._info("userAbstraction", user=venue.owner))
        if mode not in ("default", "disabled") or not isinstance(orders, list) or orders:
            raise AdapterError("HL_FUNDING_ACCOUNT_NOT_CLEAN")
        if any(Decimal(p["position"]["szi"]) != 0 for p in perp["assetPositions"]):
            raise AdapterError("HL_FUNDING_ACCOUNT_NOT_CLEAN")
        balances = [b for b in spot["balances"] if b.get("coin") == "USDC" and b.get("token") == 0]
        if len(balances) != 1:
            raise AdapterError("HL_FUNDING_SPOT_USDC_UNVERIFIED")
        total = models.parse_decimal_text(balances[0]["total"], "spot_total")
        held = models.parse_decimal_text(balances[0]["hold"], "spot_hold")
        equity = models.parse_decimal_text(perp["crossMarginSummary"]["accountValue"], "perp_equity")
        used = models.parse_decimal_text(perp["crossMarginSummary"]["totalMarginUsed"], "perp_margin")
        if total < 0 or held < 0 or held > total or equity < 0 or used != 0:
            raise AdapterError("HL_FUNDING_BALANCE_UNVERIFIED")
        return {"spot": models.canonical_decimal_text(total), "spot_free": models.canonical_decimal_text(total-held),
                "perp": models.canonical_decimal_text(equity), "ts": models.utc_now_iso()}

    async def _write(self, key, record):
        await self.runtime.store.write(lambda c: c.execute(
            "INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(record, sort_keys=True))))

    async def fund(self, row, venue, params):
        if self.runtime.execution_mode != "production" or self.allowance == 0:
            raise CredentialError("FUNDING_DISABLED", "服务未启用同账户保证金划转", 403)
        rid = params.get("request_id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
        amount = models.parse_decimal_text(params.get("amount"), "amount")
        if amount <= 0 or amount % Decimal("0.000001"):
            raise CredentialError("FUNDING_AMOUNT_INVALID", "划转金额必须为正数且不超过六位小数", 400)
        context = {"account": row["account_id"], "credential_revision": row["revision"],
                   "amount": models.canonical_decimal_text(amount), "request_id": rid}
        request_hash = models.sha256_hex(context)
        key = "self-funding:" + models.sha256_hex([row["account_id"], rid])
        saved = self.runtime.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone())
        if saved:
            record = json.loads(saved[0])
            if record["request_hash"] != request_hash:
                raise CredentialError("IDEMPOTENCY_CONFLICT", "划转请求编号已用于其他金额或凭证")
            # A lost read after an ACK may be resolved by later balance reads;
            # never sign or resend this request, even after its deadline.
            if record["state"] == "UNKNOWN" and record.get("ack"):
                try:
                    after = await self._snapshot(venue)
                    before = record["before"]
                    if (Decimal(after["spot"]) == Decimal(before["spot"]) - amount
                            and Decimal(after["perp"]) == Decimal(before["perp"]) + amount):
                        record.update(state="CONFIRMED", after=after, completed_ts=models.utc_now_iso())
                        await self._write(key, record)
                except Exception:
                    pass
            return {**record, "replayed": True}
        if (venue.owner != row["account_address"] or venue.signer is None or venue.signer.vault_address
                or venue.signer.address != row["signer_address"]):
            raise CredentialError("FUNDING_SIGNER_UNVERIFIED", "需要已核对的主账户代理签名", 422)
        authorization = await venue.authorization_check()
        if authorization.owner != venue.owner or authorization.master != venue.owner or authorization.vault_address:
            raise CredentialError("FUNDING_SCOPE_UNVERIFIED", "只支持同一主账户的现货到主永续划转", 422)
        prior = self.runtime.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%'")])
        probes = self.runtime.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
            "SELECT value FROM runtime_state WHERE key LIKE 'passive-commission:%'")])
        if any(r["state"] in ("PREPARED", "DISPATCHING", "UNKNOWN", "FILLED_REQUIRES_EXIT") for r in probes):
            raise CredentialError("FUNDING_UNRESOLVED", "先核对实盘验收订单，不能划转", 422)
        if any(r["state"] in ("PREPARED", "DISPATCHING", "UNKNOWN") for r in prior):
            raise CredentialError("FUNDING_UNRESOLVED", "先核对上一笔划转，不能再次发送", 422)
        consumed = sum((Decimal(r["amount"]) for r in prior if r["state"] == "CONFIRMED"), Decimal(0))
        if consumed + amount > self.allowance:
            raise CredentialError("FUNDING_ALLOWANCE_EXCEEDED", "划转总额超过本次服务授权上限", 422)
        before = await self._snapshot(venue)
        if amount > Decimal(before["spot_free"]):
            raise CredentialError("FUNDING_BALANCE_INSUFFICIENT", "现货可用 USDC 不足", 422)
        meta = await venue._info("spotMeta")
        tokens = [token for token in meta["tokens"] if token.get("name") == "USDC" and token.get("index") == 0]
        if len(tokens) != 1 or not re.fullmatch(r"0x[0-9a-f]{32}", tokens[0].get("tokenId", "")):
            raise CredentialError("FUNDING_TOKEN_UNVERIFIED", "USDC 原生币种编号未核对", 422)
        action = {"type": "agentSendAsset", "destination": venue.owner, "sourceDex": "spot", "destinationDex": "",
                  "token": "USDC:" + tokens[0]["tokenId"], "amount": context["amount"], "fromSubAccount": ""}
        record = {**context, "request_hash": request_hash, "state": "PREPARED", "before": before}
        await self._write(key, record)
        expiry = venue.clock_ms() + 10000
        try:
            signed = await venue.signer.sign(action, expiry, include_action_nonce=True)
        except Exception:
            record.update(state="NOT_SENT", code="FUNDING_SIGNING_FAILED")
            await self._write(key, record)
            return record
        # Recheck authorization and balances after signing, before transmission.
        try:
            authorization = await venue.authorization_check()
            current = await self._snapshot(venue)
            valid = (authorization.owner == venue.owner and authorization.master == venue.owner
                     and venue.owner == row["account_address"] == signed["action"]["destination"]
                     and authorization.vault_address is None and venue.clock_ms() < expiry
                     and current["spot"] == before["spot"] and current["spot_free"] == before["spot_free"]
                     and current["perp"] == before["perp"] and signed["action"]["nonce"] == signed["nonce"])
        except Exception:
            valid = False
        if not valid:
            record.update(state="NOT_SENT", code="FUNDING_OBSERVATION_CHANGED")
            await self._write(key, record)
            return record
        record.update(state="DISPATCHING", nonce=signed["nonce"], expires_after=expiry)
        await self._write(key, record)
        ack, rejected = False, False
        try:
            response = await venue.transport.post("/exchange", signed)
            ack = response.get("status") == "ok" and response.get("response", {}).get("type") == "default"
            rejected = response.get("status") == "err"
        except Exception:
            pass
        try:
            after = await self._snapshot(venue)
            confirmed = (ack and Decimal(after["spot"]) == Decimal(before["spot"]) - amount
                         and Decimal(after["perp"]) == Decimal(before["perp"]) + amount)
            record["after"] = after
        except Exception:
            confirmed = False
        record.update(state="CONFIRMED" if confirmed else "REJECTED" if rejected else "UNKNOWN",
                      ack=ack, completed_ts=models.utc_now_iso())
        if rejected:
            record["code"] = "FUNDING_VENUE_REJECTED"
        await self._write(key, record)
        return record

"""Independent account configuration; only the GridRuntime owns credentials.

Verification is read-only. An authorization relationship is not live venue
certification and never enables orders, fund transfers or agent approvals.
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import uuid
from dataclasses import replace
from decimal import Decimal

from . import models
from .adapters.base import AdapterError
from .adapters.hyperliquid import _address
from .adapters.hl_account import HyperliquidAccountVenue
from .credentials import CredentialError, CredentialVault
from .hl_authorization import check_authorization

KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}\Z")
PUBLIC_FIELDS = ("account_id", "name", "venue", "network", "account_address",
                 "signer_address", "revision", "credential_ref", "created_ts", "updated_ts", "verification")


class GridAccounts:
    def __init__(self, runtime, vault: CredentialVault, transport_factory, *, observe_streams=False, temporary=False,
                 funding_allowance="0", commissioning_allowance="0", venue_keys=("hl",), providers=None):
        self.runtime, self.vault, self.transport_factory = runtime, vault, transport_factory
        self._lock = asyncio.Lock()
        self.observe_streams = observe_streams
        self.temporary = temporary
        self._streams = {}
        from .venue_registry import spec_for
        from .connections import ConnectionAccounts
        self.venue_keys = tuple(venue_keys)
        for key in self.venue_keys:
            spec_for(key)
        if len({spec_for(key).protocol for key in self.venue_keys}) != 1:
            raise ValueError("GRID_RUNTIME_PROTOCOL_CONFLICT")
        self.connections = ConnectionAccounts(self, providers or {})
        if any(row.get('venue', 'hl') not in self.venue_keys for row in vault.records.values()):
            raise ValueError('GRID_VAULT_RUNTIME_SCOPE_CONFLICT')
        from .hl_funding import PerpetualFunding
        self.funding = PerpetualFunding(runtime, allowance=funding_allowance)
        from .hl_commission import PassiveCommissioning
        self.commissioning = PassiveCommissioning(runtime, allowance=commissioning_allowance)
        # Atomic, non-destructive upgrade of old account records. Grid configs
        # already store their own market; credentials and revisions stay intact.
        if any("symbol" in row for row in vault.records.values()):
            records = copy.deepcopy(vault.records)
            for row in records.values():
                if "symbol" in row:
                    row.pop("symbol")
                    row["verification"] = {"status": "unverified", "checks": [], "checked_ts": None}
            vault.commit(records)

    @staticmethod
    def public(record):
        if record.get('venue', 'hl') != 'hl':
            from .connections import public_record
            return public_record(record)
        result = {key: copy.deepcopy(record[key]) for key in PUBLIC_FIELDS if key in record}
        result["has_credential"] = bool(record.get("private_key"))
        result["live_eligible"] = False
        result["market_scope"] = "main_perpetuals"
        return result

    def list(self):
        rows = []
        for row in self.vault.records.values():
            value = self.public(row)
            if row.get('venue', 'hl') != 'hl':
                preview=self.runtime.venues.get(row['account_id'])
                value.update(stream={'connected': False, 'gap': True, 'observing': False},
                    signer_prepared=False, account_observation={'ready': False},
                    commissioning={'protected_roundtrip_passed': False}, live_markets=[],
                    preview_available=bool(preview and getattr(preview,'preview_only',False)),
                    preview_capabilities=copy.deepcopy(getattr(preview,'preview_capabilities',{})))
                rows.append(value)
                continue
            venue = self.runtime.venues.get(row["account_id"])
            value["stream"] = {"connected": bool(venue and venue.stream_connected),
                               "gap": bool(not venue or venue.stream_gap),
                               "observing": row["account_id"] in self._streams}
            value["signer_prepared"] = bool(venue and venue.signer)
            observer = getattr(venue, 'observations', None)
            observed = observer.snapshot() if observer is not None else None
            value['account_observation'] = {'ready': bool(observed),
                'protocol': observed['protocol'] if observed else None,
                'generation': observed['generation'] if observed else None}
            value["commissioning"] = self.commissioning_summary(row)
            value['live_markets'] = self.runtime.live_release.summary(row['account_id']) if self.runtime.live_release else []
            value['live_eligible'] = bool(value['live_markets']) and os.getenv('GRID_REAL_DISPATCH','0')=='1'
            rows.append(value)
        live = any(row['live_eligible'] for row in rows)
        return {"accounts": rows,
                "execution_mode": self.runtime.execution_mode,
                "live_eligible": live, "storage": "encrypted-local",
                "temporary": self.temporary,
                "real_dispatch": live}

    def venue_catalog(self):
        from .venue_registry import SPECS
        return {'venues': [s.public(configured=s.key in self.venue_keys,
                    implemented=s.key == 'hl' or s.key in self.connections.providers) for s in SPECS]}

    def commissioning_summary(self,row):
        records = [r for _, r in self.commissioning.records()
                   if r.get("account") == row["account_id"] and r.get("credential_revision") == row["revision"]]
        passed = [r for r in records if r.get("kind") in ("protected_ioc", "position_protected_ioc")
                  and r.get("state") == "CONFIRMED" and r.get("flat") is True
                  and r.get("protection_verified") is True and r.get("filled_roundtrip") is True]
        if not passed:
            return {"protected_roundtrip_passed": False}
        recent = max(passed, key=lambda r:r.get("completed_ts", ""))
        prearmed = any(r.get("kind") == "position_protected_ioc"
                       and r.get("position_stop_survived_entry") is True
                       and r.get("protection", {}).get("mode") == "full_position"
                       and r.get("protected_position_observation", {}).get("isPositionTpsl") is True
                       for r in passed)
        return {"protected_roundtrip_passed": True, "market": recent["market"],
                "notional": recent["notional"], "completed_ts": recent["completed_ts"],
                "scope": "single-prearmed-position-stop-roundtrip" if prearmed else "single-protected-ioc-roundtrip",
                "prearmed_position_stop_passed": prearmed, "live_eligible": False}

    def _record(self, account_id):
        if account_id not in self.vault.records:
            raise CredentialError("ACCOUNT_NOT_FOUND", "网格账户不存在", 404)
        return self.vault.records[account_id]

    def _check_mutable(self, account_id, *, allow_control=False):
        row = self.runtime.store.read(lambda c: c.execute(
            "SELECT 1 FROM grids WHERE account_id=? AND status!='CLOSED' LIMIT 1", (account_id,)).fetchone())
        if row:
            raise CredentialError("ACCOUNT_IN_USE", "此账户还有未结束的网格或保留仓位，请先结束并核对")
        if not allow_control:
            import json
            pending = self.runtime.store.read(lambda c: [json.loads(r[0]) for r in c.execute(
                "SELECT value FROM runtime_state WHERE key LIKE 'self-funding:%' OR key LIKE 'passive-commission:%'")])
            if any(r.get("account") == account_id and r.get("state") in
                   ("PREPARED", "DISPATCHING", "UNKNOWN", "FILLED_REQUIRES_EXIT") for r in pending):
                raise CredentialError("ACCOUNT_CONTROL_UNRESOLVED", "账户操作尚未终结，请先核对；不能替换凭证", 422)

    @staticmethod
    def _identity(network, owner):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"grid:hl:{network}:{owner}"))

    async def save(self, params):
        if isinstance(params, dict) and params.get('venue', 'hl') != 'hl':
            from .venue_registry import VENUES
            if params.get('venue') not in VENUES:
                raise CredentialError('GRID_VENUE_UNSUPPORTED', '不支持此交易所', 400)
            return await self.connections.save(params)
        if 'hl' not in self.venue_keys:
            raise CredentialError('VENUE_RUNTIME_UNAVAILABLE', '此服务未配置 Hyperliquid', 503)
        async with self._lock, self.runtime.account_control:
            if self.runtime.execution_mode != "production":
                raise CredentialError("REAL_ACCOUNT_IN_SIMULATION", "请在独立的生产网格服务中配置真实账户", 403)
            if not isinstance(params, dict):
                raise CredentialError("VALIDATION_ERROR", "账户参数必须是对象", 400)
            rid = params.get("request_id")
            if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
            # The hash/receipt is encrypted with the account records, never in
            # the trading journal, SQLite request history or HTTP logs.
            request_hash = models.sha256_hex({"action": "save", "params": params})
            if rid in self.vault.receipts:
                receipt = self.vault.receipts[rid]
                if receipt["hash"] != request_hash:
                    raise CredentialError("IDEMPOTENCY_CONFLICT", "此请求编号已经用于另一操作")
                return copy.deepcopy(receipt["result"])
            name, network = params.get("name"), params.get("network")
            secret = params.get("private_key", "")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
                raise CredentialError("VALIDATION_ERROR", "账户名称需为 1–64 个字符", 400)
            if network not in ("mainnet", "testnet"):
                raise CredentialError("VALIDATION_ERROR", "请选择主网或测试网", 400)
            try:
                owner = _address(params.get("account_address"))
            except AdapterError:
                raise CredentialError("ACCOUNT_ADDRESS_INVALID", "资金账户地址应为 0x 开头的 40 位十六进制地址", 400) from None
            aid = self._identity(network, owner)
            old = self.vault.records.get(aid)
            if old and old.get('venue', 'hl') != 'hl':
                raise CredentialError('ACCOUNT_EXISTS', '此资金风险域已通过其他入口配置，不能重复接入')
            requested = params.get("account_id")
            if requested and requested != aid:
                raise CredentialError("ACCOUNT_IDENTITY_IMMUTABLE", "替换凭证不能更改资金账户或网络；请新增账户")
            if old and not requested:
                raise CredentialError("ACCOUNT_EXISTS", "此网络的资金账户已配置，请使用替换凭证")
            if requested and not old:
                raise CredentialError("ACCOUNT_NOT_FOUND", "网格账户不存在", 404)
            if old:
                if params.get("expected_revision") != old["revision"]:
                    raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
                self._check_mutable(aid)
                if secret == "":
                    secret = old["private_key"]
            if not isinstance(secret, str) or not KEY.fullmatch(secret):
                raise CredentialError("PRIVATE_KEY_INVALID", "私钥格式不正确；内容不会写入日志", 400)
            from eth_account import Account
            try:
                wallet = Account.from_key(secret)
            except Exception:
                raise CredentialError("PRIVATE_KEY_INVALID", "私钥无效；内容不会写入日志", 400) from None
            signer = wallet.address.lower()
            if any(r["network"] == network and r.get("signer_address") == signer and r["account_id"] != aid for r in self.vault.records.values()):
                raise CredentialError("SIGNER_ALREADY_CONFIGURED", "请为此网格账户使用独立的 API 代理签名钱包")
            now = models.utc_now_iso()
            revision = uuid.uuid4().hex
            row = {"account_id": aid, "name": name.strip(), "venue": "hl", "network": network,
                   "account_address": owner, "signer_address": signer,
                   "private_key": secret, "revision": revision, "credential_ref": f"grid-credential:{aid}:{revision}",
                   "created_ts": old["created_ts"] if old else now, "updated_ts": now,
                   "verification": {"status": "unverified", "checks": [], "checked_ts": None}}
            result = {"account": self.public(row)}
            receipts = {**self.vault.receipts, rid: {"hash": request_hash, "result": result}}
            self.vault.commit({**self.vault.records, aid: row}, receipts)
            await self._stop_stream(aid)
            self._close_signer(aid)
            self.runtime.venues.pop(aid, None)
            self.runtime.executors.pop(aid, None)
            return result

    async def delete(self, params):
        async with self._lock, self.runtime.account_control:
            rid = params.get("request_id")
            if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
            request_hash = models.sha256_hex({"action": "delete", "params": params})
            if rid in self.vault.receipts:
                receipt = self.vault.receipts[rid]
                if receipt["hash"] != request_hash:
                    raise CredentialError("IDEMPOTENCY_CONFLICT", "此请求编号已经用于另一操作")
                return copy.deepcopy(receipt["result"])
            aid = params.get("account_id")
            row = self._record(aid)
            if params.get("expected_revision") != row["revision"]:
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            self._check_mutable(aid)
            result = {"deleted": True, "account_id": aid}
            receipts = {**self.vault.receipts, rid: {"hash": request_hash, "result": result}}
            self.vault.commit({k: v for k, v in self.vault.records.items() if k != aid}, receipts)
            await self._stop_stream(aid)
            self._close_signer(aid)
            self.runtime.venues.pop(aid, None)
            self.runtime.executors.pop(aid, None)
            return result

    async def resolve_owner(self, params):
        """Correct an agent entered as owner, using its on-chain delegation.

        Never select a subaccount or another wallet by balance. Existing grids
        and identity collisions block the correction. The secret stays in vault.
        """
        async with self._lock, self.runtime.account_control:
            rid = params.get("request_id")
            if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
            request_hash = models.sha256_hex({"action": "resolve_owner", "params": params})
            if rid in self.vault.receipts:
                receipt = self.vault.receipts[rid]
                if receipt["hash"] != request_hash:
                    raise CredentialError("IDEMPOTENCY_CONFLICT", "请求编号已用于另一操作")
                return copy.deepcopy(receipt["result"])
            row = copy.deepcopy(self._record(params.get("account_id")))
            if row.get('venue', 'hl') != 'hl':
                raise CredentialError('ACCOUNT_CONTROL_UNSUPPORTED', '此交易所不支持此账户操作', 422)
            if params.get("expected_revision") != row["revision"]:
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            old_id = row["account_id"]
            self._check_mutable(old_id)
            if row["account_address"] != row["signer_address"]:
                raise CredentialError("OWNER_RESOLUTION_NOT_APPLICABLE", "此账户未将签名代理填作资金账户")
            transport = self.transport_factory(row["network"])
            try:
                role = await transport.post("/info", {"type": "userRole", "user": row["account_address"]})
                if role.get("role") != "agent":
                    raise AdapterError("HL_AGENT_ROLE_REQUIRED")
                owner = _address(role.get("data", {}).get("user"))
                venue = HyperliquidAccountVenue(transport, account_address=owner, network=row["network"])
                await check_authorization(venue, row["signer_address"])
            except Exception:
                raise CredentialError("OWNER_RESOLUTION_UNVERIFIED", "无法核对代理与资金账户的有效授权，配置保持不变", 422) from None
            new_id = self._identity(row["network"], owner)
            if new_id == old_id or new_id in self.vault.records:
                raise CredentialError("ACCOUNT_EXISTS", "目标资金账户已配置，不能合并凭证")
            self._check_mutable(new_id)
            revision = uuid.uuid4().hex
            row.update(account_id=new_id, account_address=owner, revision=revision,
                credential_ref=f"grid-credential:{new_id}:{revision}", updated_ts=models.utc_now_iso(),
                verification={"status": "unverified", "checks": [], "checked_ts": None})
            result = {"account": self.public(row), "previous_account_id": old_id, "resolved": True}
            records = {aid: value for aid, value in self.vault.records.items() if aid != old_id}
            records[new_id] = row
            self.vault.commit(records, {**self.vault.receipts, rid: {"hash": request_hash, "result": result}})
            await self._stop_stream(old_id)
            self._close_signer(old_id)
            self.runtime.venues.pop(old_id, None)
            self.runtime.executors.pop(old_id, None)
            return result

    async def fund_perpetuals(self, params):
        async with self._lock, self.runtime.account_control:
            row = self._record(params.get("account_id"))
            if row.get('venue','hl')!='hl':
                raise CredentialError('VENUE_ACTION_UNSUPPORTED','此交易所不使用 Hyperliquid 的资金划转流程',422)
            if row["revision"] != params.get("expected_revision"):
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            self._check_mutable(row["account_id"], allow_control=True)
            venue = self.runtime.venues.get(row["account_id"])
            executor = self.runtime.executors.get(row["account_id"])
            if venue is None or executor is None:
                raise CredentialError("FUNDING_SIGNER_UNVERIFIED", "请先完成账户授权和签名接入检查", 422)
            async with executor._lock:
                return await self.funding.fund(row, venue, params)

    async def commission_probe(self, params):
        async with self._lock, self.runtime.account_control:
            row = self._record(params.get("account_id"))
            if row.get('venue','hl')!='hl':
                raise CredentialError('VENUE_ACTION_UNSUPPORTED','请使用此交易所独立的实盘验证流程',422)
            if row["revision"] != params.get("expected_revision"):
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            self._check_mutable(row["account_id"], allow_control=True)
            venue = self.runtime.venues.get(row["account_id"])
            executor = self.runtime.executors.get(row["account_id"])
            if venue is None or executor is None:
                raise CredentialError("ACCOUNT_UNVERIFIED", "请先检查接入", 422)
            async with executor._lock:
                return await self.commissioning.probe(row, venue, params)

    def commission_status(self, params):
        row = self._record(params.get('account_id'))
        rid = params.get('request_id')
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            raise CredentialError('REQUEST_ID_REQUIRED', '缺少有效请求编号', 400)
        key = 'passive-commission:' + models.sha256_hex([row['account_id'], rid])
        record = dict(self.commissioning.records()).get(key)
        if (record is None or record.get('credential_revision') != row['revision']
                or record.get('account') != row['account_id'] or record.get('request_id') != rid):
            return {'request_id': rid, 'state': 'NOT_FOUND'}
        fields = ('request_id', 'kind', 'state', 'flat', 'completed_ts', 'coexistence_passed',
                  'protection_verified', 'position_stop_survived_entry', 'filled_roundtrip', 'code')
        return {field: copy.deepcopy(record[field]) for field in fields if field in record}

    async def markets(self, network, venue='hl'):
        if venue != 'hl':
            return {'markets': await self.connections.markets(venue, network), 'venue': venue, 'network': network}
        if network not in ("mainnet", "testnet"):
            raise CredentialError("VALIDATION_ERROR", "请选择主网或测试网", 400)
        try:
            meta = await self.transport_factory(network).post("/info", {"type": "meta"})
            rows = [{"symbol": r["name"], "max_leverage": r["maxLeverage"]} for r in meta["universe"] if not r.get("isDelisted")]
            return {"markets": rows, "network": network}
        except Exception:
            raise CredentialError("MARKET_UNAVAILABLE", "暂时无法读取交易所币种，请稍后重试", 503) from None

    async def _attach(self, row, venue):
        aid = row["account_id"]
        venue.display_name, venue.credential_ref = row["name"], row["credential_ref"]
        venue._account = replace(venue._account, credential_revision=row["revision"])
        # No signer is armed during read-only verification. Stored credentials
        # cannot bypass live qualification, precision or recovery certification.
        if aid in self.runtime.venues:
            self._check_mutable(aid)
            await self._stop_stream(aid)
            old = self.runtime.venues[aid]
            if old.signer:
                old.signer.close()
            self.runtime.venues.pop(aid)
            self.runtime.executors.pop(aid, None)
        await self.runtime.add_venue(venue)
        if self.observe_streams:
            stop = asyncio.Event()
            task = asyncio.create_task(venue.run_stream(stop), name=f"grid-hl-observe:{aid}")
            self._streams[aid] = (stop, task)

    async def verify(self, params):
        if self._record(params.get('account_id')).get('venue', 'hl') != 'hl':
            return await self.connections.verify(params)
        async with self._lock, self.runtime.account_control:
            row = copy.deepcopy(self._record(params.get("account_id")))
            if params.get("expected_revision") != row["revision"]:
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            self._check_mutable(row["account_id"])
            venue = HyperliquidAccountVenue(self.transport_factory(row["network"]), account_address=row["account_address"],
                                         network=row["network"])
            checks = []
            evidence = {}
            def add(key, state, message):
                checks.append({"key": key, "state": state, "message": message})
            try:
                abstraction = await venue._info("userAbstraction", user=venue.owner)
                role = await venue._info("userRole", user=venue.owner)
                evidence["account_role"] = role.get("role")
                if role.get("role") == "agent":
                    add("account_address", "fail", "资金账户地址填成了 API 代理地址；请使用代理关联的主账户地址")
                    if venue.owner == row["signer_address"]:
                        evidence["suggested_owner"] = _address(role.get("data", {}).get("user"))
                authorization = None
                try:
                    authorization = await check_authorization(venue, row["signer_address"])
                except AdapterError as error:
                    reason = str(error)
                    auth_messages = {"HL_NAMED_AGENT_REQUIRED": "请使用在交易所设置名称、可查询有效期的 API 代理钱包",
                        "HL_AGENT_EXPIRED_OR_EXPIRING": "API 代理凭证已过期或即将过期，请在交易所更新授权",
                        "HL_AGENT_VALIDITY_UNVERIFIED": "无法核对 API 代理有效期，暂不能准备签名"}
                    add("authorization", "fail", auth_messages.get(reason, "签名地址未获此资金账户授权，或账户类型暂不支持"))
                auth = authorization is not None
                if auth:
                    add("authorization", "pass", "资金账户、签名授权与有效期已核对")
                standard = abstraction in ("default", "disabled")
                add("account_mode", "pass" if standard else "fail", "标准永续账户" if standard else "统一账户／组合保证金模式尚未认证，暂不能开单")
                markets, fees, state, open_orders = await asyncio.gather(venue.catalog_markets(),
                    venue._info("userFees", user=venue.owner), venue._info("clearinghouseState", user=venue.owner),
                    venue._info("frontendOpenOrders", user=venue.owner))
                supported_mode = standard
                try:
                    snapshot = await venue.snapshot_for_state(state, abstraction, open_orders)
                    supported_mode = standard or bool((snapshot.evidence or {}).get("unified_scope"))
                except Exception:
                    snapshot = venue.snapshot_from_state(state, abstraction)
                if supported_mode and not standard:
                    mode_check = next(c for c in checks if c["key"] == "account_mode")
                    mode_check.update(state="pass", message="统一账户 · USDC / 主永续全仓；其他永续市场占用已排除")
                    evidence["unified_scope"] = snapshot.evidence["unified_scope"]
                available = next((Decimal(v) for asset, v in snapshot.assets if asset == "USDC"), Decimal(0))
                # Show account evidence, without asserting withdrawable is the
                # margin balance. Trading capital remains fail-closed below.
                evidence.update({"available_margin": models.canonical_decimal_text(available) if supported_mode else None,
                    "asset": "USDC", "account_mode": abstraction, "market_count": len(markets),
                    "maker_fee_rate": models.canonical_decimal_text(models.parse_decimal_text(fees["userAddRate"], "maker_fee")),
                    "taker_fee_rate": models.canonical_decimal_text(models.parse_decimal_text(fees["userCrossRate"], "taker_fee")),
                    "open_orders": len(open_orders), "position_markets": [r["position"]["coin"] for r in state["assetPositions"] if Decimal(r["position"]["szi"]) != 0]})
                if authorization:
                    evidence["authorization"] = authorization.to_dict()
                add("capital", "pass" if supported_mode and available > 0 else "fail", f"可用保证金 {evidence['available_margin'] or '待认证'} USDC" if available > 0 else "尚无可用保证金；请自行向独立网格账户入金")
                if not supported_mode:
                    checks[-1]["message"] = "账户模式尚未支持，不能从标准永续余额判断资金是否充足"
                    try:
                        spot = await venue._info("spotClearinghouseState", user=venue.owner)
                        balances = [b for b in spot["balances"] if b.get("coin") == "USDC" and b.get("token") == 0]
                        if len(balances) == 1:
                            total = models.parse_decimal_text(balances[0]["total"], "spot_usdc")
                            held = models.parse_decimal_text(balances[0]["hold"], "spot_usdc_hold")
                            if total < 0 or held < 0 or held > total:
                                raise AdapterError("HL_BALANCES_UNVERIFIED")
                            evidence["spot_usdc_total"] = models.canonical_decimal_text(total)
                            evidence["spot_usdc_hold"] = models.canonical_decimal_text(held)
                            checks[-1]["message"] = f"账户 USDC 余额 {evidence['spot_usdc_total']}；当前模式的可开单保证金尚未认证"
                    except Exception:
                        evidence["spot_balance_verified"] = False
                if standard and available == 0 and role.get("role") != "agent":
                    try:
                        spot = await venue._info("spotClearinghouseState", user=venue.owner)
                        balances = [b for b in spot["balances"] if b.get("coin") == "USDC"]
                        if len(balances) == 1:
                            total = models.parse_decimal_text(balances[0]["total"], "spot_usdc")
                            held = models.parse_decimal_text(balances[0]["hold"], "spot_usdc_hold")
                            if total < 0 or held < 0 or held > total:
                                raise ValueError()
                            evidence["spot_usdc_available"] = models.canonical_decimal_text(total - held)
                            if total > held:
                                checks[-1]["message"] = f"现货有 {evidence['spot_usdc_available']} USDC，但永续保证金为 0；需在同账户内划转至永续"
                    except Exception:
                        evidence["spot_balance_verified"] = False
                clean = not open_orders and not evidence["position_markets"]
                add("clean_account", "pass" if clean else "fail", "账户无现有挂单与仓位" if clean else "账户存在挂单或仓位，请先在交易所自行处理")
                add("markets", "pass" if markets else "fail", f"已读取 {len(markets)} 个主永续合约与账户费率；创建网格时选择币种")
                venue.account_mode_confirmed = bool(auth and supported_mode)
                await self._attach(row, venue)
                if authorization and supported_mode:
                    try:
                        self._prepare_signer(row, venue, authorization)
                        release = self.runtime.live_release
                        if callable(getattr(release, 'restore', None)):
                            # Recheck native qualification and restore the new
                            # market views' bounded history after root replacement.
                            await release.restore()
                        add("signer", "pass", "网格签名钱包及独立签名锁已准备；开单仍未启用")
                    except AdapterError:
                        add("signer", "fail", "签名钱包正被其他网格进程使用，或签名配置校验失败")
            except (AdapterError, KeyError, TypeError, ValueError, AttributeError):
                add("connection", "fail", "交易所连接或响应校验失败；请检查地址、网络后重试")
            except CredentialError:
                raise
            except Exception:
                # Never reflect an exception that might contain submitted data.
                add("connection", "fail", "接入校验暂时失败，请稍后重试")
            commission = self.commissioning_summary(row)
            executed = commission["protected_roundtrip_passed"]
            add("execution", "pending", "BTC 整个仓位止损先挂置、后入场及减仓退出已验证；持续网格和历史恢复仍待认证"
                if commission.get("prearmed_position_stop_passed") else "BTC 小额入场、原生止损挂置与减仓退出已验证；持续网格保护和历史恢复仍待认证" if executed
                else "成交历史覆盖、断线恢复与保护认证仍未完成；真实开单未启用")
            verification = {"status": "checks_passed" if checks and all(c["state"] != "fail" for c in checks) else "failed",
                            "checked_ts": models.utc_now_iso(), "checks": checks, "evidence": evidence,
                            "live_eligible": False}
            row["verification"] = verification
            self.vault.commit({**self.vault.records, row["account_id"]: row})
            return {"account": self.public(row)}

    async def restore(self):
        """Restore observed adapters before runtime startup; never trust old checks."""
        failures = []
        for row in self.vault.records.values():
            if row.get('venue', 'hl') != 'hl':
                # The Bulk bridge restores public account/market previews.
                # Stored checks never arm a signer or grant trading.
                restored=await self.connections.restore_preview(row)
                if row['venue']=='bulk' and callable(getattr(self.connections.providers.get('bulk'),'preview_venue',None)) and not restored:
                    failures.append(row['account_id'])
                continue
            try:
                venue = HyperliquidAccountVenue(self.transport_factory(row["network"]), account_address=row["account_address"],
                                             network=row["network"])
                await self._attach(row, venue)
                try:
                    authorization = await check_authorization(venue, row["signer_address"])
                    abstraction = await venue._info("userAbstraction", user=venue.owner)
                    mode_supported = abstraction in ("disabled", "default")
                    if abstraction == "unifiedAccount":
                        mode_supported = bool((await venue.account_snapshot()).evidence.get("unified_scope"))
                    if mode_supported:
                        venue.account_mode_confirmed = True
                        self._prepare_signer(row, venue, authorization)
                except AdapterError:
                    failures.append(row["account_id"])
            except Exception:
                failures.append(row["account_id"])
        # Cleanup of an already journaled real probe is authorized by its
        # original bounded request, even if new commissioning is disabled.
        for key, record in self.commissioning.records():
            if record["state"] not in ("PREPARED", "DISPATCHING", "UNKNOWN", "FILLED_REQUIRES_EXIT"):
                continue
            row = self.vault.records.get(record["account"])
            venue = self.runtime.venues.get(record["account"])
            if not row or not venue or row["revision"] != record["credential_revision"]:
                failures.append(record["account"])
                continue
            try:
                await self.commissioning.reconcile(row, venue, key, record)
            except Exception:
                failures.append(record["account"])
        return failures

    def _prepare_signer(self, row, venue, authorization):
        from eth_account import Account
        from .adapters.hyperliquid import HyperliquidSigner
        signer = HyperliquidSigner(Account.from_key(row["private_key"]), self.runtime.store,
            network=row["network"], lock_dir=str(self.vault.directory.parent / "grid-signer-locks"),
            vault_address=authorization.vault_address)
        if signer.address != row["signer_address"]:
            signer.close()
            raise AdapterError("HL_CREDENTIAL_IDENTITY_MISMATCH")
        venue.signer = signer
        venue.control_store = self.runtime.store
        async def recheck():
            evidence = await check_authorization(venue, signer.address)
            if evidence.vault_address != signer.vault_address:
                raise AdapterError("HL_SIGNER_ACCOUNT_MISMATCH")
            return evidence
        venue.authorization_check = recheck
        # Preparation does not grant execution. Live qualification and the
        # service dispatch policy still control every action.

    async def _stop_stream(self, aid):
        stream = self._streams.pop(aid, None)
        if stream:
            stream[0].set()
            stream[1].cancel()
            await asyncio.gather(stream[1], return_exceptions=True)

    def start_stream(self, aid):
        """Own the private account stream of an execution-capable venue.

        Used by the venue worker for the armed bulk factory; the stream stop
        stays with this manager so shutdown and replacement remain single-owner.
        """
        venue = self.runtime.venues.get(aid)
        if venue is None or not hasattr(venue, 'run_stream'):
            return None
        stop = asyncio.Event()
        task = asyncio.create_task(venue.run_stream(stop), name=f"grid-account-stream:{aid}")
        self._streams[aid] = (stop, task)
        return task

    def _close_signer(self, aid):
        venue = self.runtime.venues.get(aid)
        if venue and venue.signer:
            venue.signer.close()
            venue.signer = None

    def drain_unused_observations(self):
        # Public account streams can contain external activity. Before any
        # grid exists, do not accumulate those messages in an unbounded queue.
        active = self.runtime.store.read(lambda c: {r[0] for r in c.execute("SELECT DISTINCT account_id FROM grids WHERE status!='CLOSED'")})
        for aid in self._streams:
            if aid not in active and aid in self.runtime.venues:
                self.runtime.venues[aid].poll()

    async def close(self):
        await self.connections.close()
        for aid in list(self._streams):
            await self._stop_stream(aid)
        for venue in list(self.runtime.venues.values()):
            feed = getattr(venue, 'risk_feed', None)
            if feed is not None:
                try:
                    await feed.close()
                except Exception:
                    pass
        for aid in self.vault.records:
            self._close_signer(aid)

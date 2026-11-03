"""Encrypted configuration and read-only checks for new grid integrations.

This is deliberately separate from Hyperliquid's reviewed commissioning path.
Providers cannot sign trades or grant execution through a connection check.
"""
import asyncio
import copy
import hashlib
import uuid
import time

from . import models
from .credentials import CredentialError
from .venue_registry import account_identity, spec_for, validate_fields


def public_record(row):
    return {key: copy.deepcopy(row[key]) for key in (
        "account_id", "name", "venue", "network", "account_address",
        "revision", "credential_ref", "created_ts", "updated_ts", "verification") if key in row} | {
        "has_credential": True, "live_eligible": False,
        "market_scope": "stablecoin_linear_perpetuals",
        "identity_verified": False,
        "credential_identity": {key: value for key, value in row.get("credentials", {}).items()
                                if key in ("account_address", "account_index", "api_key_index")},
    }


def connection_owner(spec, credentials):
    if spec.key == 'bulk':
        from .key_formats import solana_public_key
        return solana_public_key(credentials['secret_key'])
    if "account_address" in credentials:
        return credentials["account_address"]
    if "account_index" in credentials:
        return "index:" + credentials["account_index"]
    # An API key fingerprint identifies a connection, NOT an independently
    # verified economic account. Such records can never acquire live grants.
    values = {f.key: credentials[f.key] for f in spec.credentials if not f.secret}
    if not values:
        raise CredentialError("ACCOUNT_IDENTITY_UNAVAILABLE", "此交易所尚需实现资金账户身份识别，暂不能保存", 422)
    return "connection:" + hashlib.sha256(models.canonical_json_bytes(values)).hexdigest()


class ConnectionAccounts:
    def __init__(self, manager, providers):
        self.manager = manager
        self.providers = providers
        self._catalog_cache = {}
        self._catalog_tasks = {}
        self._preview_errors = set()

    async def attach_preview(self, row):
        """Register a read-only bridge; never pass credentials to its factory."""
        m=self.manager;aid=row['account_id'];old=m.runtime.venues.get(aid)
        factory=getattr(self.providers.get(row['venue']),'preview_venue',None) if row['venue']=='bulk' else None
        if not callable(factory):return False
        if old is not None:
            if not getattr(old,'preview_only',False) or old.signer or old.write_enabled or old.live_qualified:
                raise CredentialError('ACCOUNT_PREVIEW_REPLACEMENT_REFUSED','此账户的交易接入不可由预览替换',409)
            if old.credential_ref==row['credential_ref'] and old._account.credential_revision==row['revision']:
                return True
            m._check_mutable(aid)
            # A committed credential rotation must not leave an old adapter
            # usable, even when building the replacement fails.
            m.runtime.venues.pop(aid);m.runtime.executors.pop(aid,None)
        try:
            venue=factory(public_record(row),store=m.runtime.store)
            identity=await venue.resolve_account()
            if (identity.account_id!=aid or identity.venue!=row['venue'] or identity.owner!=row['account_address']
                    or identity.deployment!=row['venue']+':'+row['network'] or identity.credential_revision!=row['revision']
                    or venue.credential_ref!=row['credential_ref'] or not getattr(venue,'preview_only',False)
                    or venue.signer or venue.write_enabled or venue.live_qualified
                    or venue.protocol is not None or venue.order_transport is not None):
                raise CredentialError('ACCOUNT_PREVIEW_SCOPE_INVALID','账户预览范围未通过检查',422)
            venue.display_name=row['name']
            await m.runtime.add_venue(venue)
        except Exception:
            self._preview_errors.add(aid)
            raise
        self._preview_errors.discard(aid)
        return True

    async def restore_preview(self, row):
        # Saving credentials succeeds offline even if a local bridge is not
        # available. A later check/restart may restore it, without signing.
        try:return await self.attach_preview(row)
        except Exception:return False

    async def markets(self, venue, network):
        spec = spec_for(venue)
        if venue not in self.manager.venue_keys or network not in spec.networks:
            raise CredentialError('VENUE_RUNTIME_UNAVAILABLE', '交易所或网络未配置', 503)
        if venue not in self.providers:
            raise CredentialError('VENUE_OBSERVATION_UNAVAILABLE', '市场读取尚未实现', 422)
        key = venue, network
        cached = self._catalog_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return copy.deepcopy(cached[1])
        if key not in self._catalog_tasks:
            async def fetch():
                try:
                    rows = await asyncio.wait_for(self.providers[venue].markets(network), 25)
                    self._catalog_cache[key] = time.monotonic() + 30, rows
                    return rows
                finally:
                    self._catalog_tasks.pop(key, None)
            task = asyncio.create_task(fetch(), name=f'grid-catalog:{venue}:{network}')
            task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            self._catalog_tasks[key] = task
        return copy.deepcopy(await asyncio.shield(self._catalog_tasks[key]))

    async def catalog(self):
        async def one(row):
            result = dict(account_ref=row['account_id'], name=row['name'], venue=row['venue'],
                credential_ref=row['credential_ref'], simulated=False, live_eligible=False,
                observation_only=True, observation_source='public_catalog', markets=[],
                read_only_reason='当前仅支持接入观察；资金风险、订单恢复与保护尚未完成认证，不能预览或开单')
            if row['account_id'] in self._preview_errors:
                result['read_only_reason']='账户已保存，资金规模预览暂未就绪；请检查接入后重试。真实开单未启用。'
            try:
                result['markets'] = await self.markets(row['venue'], row['network'])
            except Exception:
                result.update(observation_status='unavailable', read_only_reason='市场读取暂不可用或尚未实现；凭证配置已保留，真实交易未启用')
            return result
        return await asyncio.gather(*(one(copy.deepcopy(r)) for r in self.manager.vault.records.values()
                                      if r.get('venue', 'hl') != 'hl'
                                      and r['account_id'] not in self.manager.runtime.venues))

    async def close(self):
        tasks = list(self._catalog_tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def save(self, params):
        m = self.manager
        async with m._lock, m.runtime.account_control:
            if m.runtime.execution_mode != "production":
                raise CredentialError("REAL_ACCOUNT_IN_SIMULATION", "模拟服务不能保存真实账户", 403)
            spec = spec_for(params.get("venue"))
            if spec.key not in m.venue_keys:
                raise CredentialError("VENUE_RUNTIME_UNAVAILABLE", "此交易所需使用独立网格执行服务", 503)
            if params.get("network") not in spec.networks:
                raise CredentialError("VALIDATION_ERROR", "此交易所不支持所选网络", 400)
            rid = params.get("request_id")
            if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise CredentialError("REQUEST_ID_REQUIRED", "缺少有效请求编号", 400)
            digest = models.sha256_hex({"action": "save", "params": params})
            receipt = m.vault.receipts.get(rid)
            if receipt:
                if receipt["hash"] != digest:
                    raise CredentialError("IDEMPOTENCY_CONFLICT", "请求编号已用于另一操作")
                return copy.deepcopy(receipt["result"])
            name = params.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
                raise CredentialError("VALIDATION_ERROR", "账户名称需为 1–64 个字符", 400)
            old = m.vault.records.get(params.get("account_id"))
            if params.get("account_id") and old is None:
                raise CredentialError("ACCOUNT_NOT_FOUND", "网格账户不存在", 404)
            if old:
                if (old["venue"], old["network"]) != (spec.key, params["network"]):
                    raise CredentialError("ACCOUNT_IDENTITY_IMMUTABLE", "更换交易所或网络请新增账户")
                if old["revision"] != params.get("expected_revision"):
                    raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
                m._check_mutable(old["account_id"])
                attached=m.runtime.venues.get(old['account_id'])
                if spec.key=='bulk' and attached is not None and (not getattr(attached,'preview_only',False)
                        or attached.signer or attached.write_enabled or attached.live_qualified):
                    raise CredentialError('ACCOUNT_PREVIEW_REPLACEMENT_REFUSED','此账户的交易接入不可由预览替换',409)
            try:
                credentials = validate_fields(spec, params.get("credentials"), old.get("credentials") if old else None)
            except ValueError:
                raise CredentialError("CREDENTIAL_FORMAT_INVALID", "请核对交易所要求的凭证字段与格式", 400) from None
            owner = connection_owner(spec, credentials)
            aid = account_identity(spec.key, params["network"], owner)
            if old and old["account_id"] != aid:
                raise CredentialError("ACCOUNT_IDENTITY_IMMUTABLE", "账户身份发生变化，请新增账户")
            if not old and aid in m.vault.records:
                raise CredentialError("ACCOUNT_EXISTS", "此资金账户或接入凭证已配置")
            # Key-scoped APIs do not prove two keys belong to distinct capital.
            # Until a native economic identity is available keep only one such
            # connection in the deployment, and never attach it for execution.
            if owner.startswith("connection:") and any(
                r["venue"] == spec.key and r["network"] == params["network"] and r["account_id"] != aid
                for r in m.vault.records.values()
            ):
                raise CredentialError("ACCOUNT_SCOPE_UNVERIFIED", "资金账户身份尚未认证，此部署暂只能保存一个接入配置", 422)
            now, revision = models.utc_now_iso(), uuid.uuid4().hex
            row = dict(account_id=aid, name=name.strip(), venue=spec.key, network=params["network"],
                account_address=owner, credentials=credentials, revision=revision,
                credential_ref=f"grid-credential:{aid}:{revision}", created_ts=old["created_ts"] if old else now,
                updated_ts=now, verification={"status": "unverified", "checks": [], "checked_ts": None})
            result = {"account": public_record(row)}
            m.vault.commit({**m.vault.records, aid: row},
                {**m.vault.receipts, rid: {"hash": digest, "result": result}})
            await self.restore_preview(row)
            return result

    async def verify(self, params):
        m = self.manager
        async with m._lock, m.runtime.account_control:
            row = copy.deepcopy(m._record(params.get("account_id")))
            if params.get("expected_revision") != row["revision"]:
                raise CredentialError("CREDENTIAL_REVISION_CONFLICT", "凭证已更新，请刷新页面")
            m._check_mutable(row["account_id"])
            checks, evidence = [], {}
            provider = self.providers.get(row["venue"])
            if provider is None:
                checks.append(dict(key="adapter", state="pending", message="凭证已保存；交易所的网格适配与恢复保护验证尚未完成"))
            else:
                try:
                    # Providers may only issue allowlisted native read requests.
                    result = await asyncio.wait_for(provider.check(row), timeout=30)
                    checks.extend(result["checks"])
                    evidence = result["evidence"]
                except Exception:
                    checks.append(dict(key="connection", state="fail", message="连接或交易所响应校验失败，请检查账户配置后重试"))
            checks.append(dict(key="execution", state="pending", message="真实网格、持仓保护、历史恢复及资金账户身份认证尚未完成；真实开单未启用"))
            authenticated_read = any(c['key'] in ('authorization', 'read_authorization') and c['state'] == 'pass' for c in checks)
            state = "failed" if any(c["state"] == "fail" for c in checks) else "checks_passed" if authenticated_read else "pending"
            row["verification"] = dict(status=state, checked_ts=models.utc_now_iso(), checks=checks,
                                        evidence=evidence, live_eligible=False)
            m.vault.commit({**m.vault.records, row["account_id"]: row})
            await self.restore_preview(row)
            return {"account": public_record(row)}

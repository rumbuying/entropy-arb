"""Grid-only integration catalogue. Declared capabilities never grant trading.

No imports from the legacy registry, signer SDKs, environment credentials or
network clients. Deployment aliases share a protocol, not a qualification.
"""
from dataclasses import dataclass
import re
import uuid

REQUIRED_LIVE_CAPABILITIES = (
    'limit_orders', 'client_id_binding', 'order_terminal_query', 'open_orders_snapshot',
    'private_fills_stream', 'fills_backfill', 'stable_execution_key', 'funding_history',
    'portfolio_risk', 'reduce_only', 'position_protection', 'stream_recovery', 'small_residual_close',
)


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = False
    kind: str = "text"

    def public(self):
        return dict(key=self.key, label=self.label, secret=self.secret,
                    kind=self.kind, required=True)


@dataclass(frozen=True)
class GridVenueSpec:
    key: str
    label: str
    protocol: str
    networks: tuple
    credentials: tuple
    documentation: str
    client_id_kind: str = "string"
    client_id_max: int | None = None
    prerequisites: tuple = ()
    settlement_asset: str = 'USDC'
    client_id_width: int = 0

    def public(self, *, configured=False, implemented=False):
        return dict(venue=self.key, label=self.label, protocol=self.protocol,
            networks=list(self.networks), credential_fields=[f.public() for f in self.credentials],
            documentation=self.documentation, configured=configured,
            connection_supported=configured and implemented,
            status="observation_only" if configured and implemented else "unavailable",
            reason=("可保存并检查接入；真实交易需按账户和市场单独验收" if configured and implemented
                    else "此服务尚未配置该交易所" if not configured else "网格适配与恢复保护验证尚未完成"),
            market_scope="stablecoin_linear_perpetuals", settlement_asset=self.settlement_asset,
            prerequisites=list(self.prerequisites), required_live_capabilities=list(REQUIRED_LIVE_CAPABILITIES),
            live_eligible=False, qualification_scope='account+credential_revision+deployment+market+direction+policy')


F = CredentialField
HL_FIELDS = (F("account_address", "资金账户地址（不是代理地址）", kind="evm_address"),
             F("private_key", "API 代理钱包私钥", True, "evm_private_key"))
LIGHTER_FIELDS = (F("account_index", "资金账户索引", kind="uint"),
                  F("api_key_index", "API Key 索引", kind="uint"),
                  F("api_private_key", "API 签名私钥", True, "hex"))
SPECS = (
    GridVenueSpec("hl", "Hyperliquid", "hl", ("mainnet", "testnet"), HL_FIELDS,
        "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api", "hex128"),
    GridVenueSpec("lighter", "Lighter", "lighter", ("mainnet", "testnet"), LIGHTER_FIELDS,
        "https://apidocs.lighter.xyz/", "numeric", 2**48-1),
    GridVenueSpec("lighter-rh", "Lighter-RH", "lighter", ("mainnet",), LIGHTER_FIELDS,
        "https://apidocs.lighter.xyz/", "numeric", 2**48-1,
        ("独立部署端点、签名域及账户需分别核验",)),
    GridVenueSpec("tradexyz", "TradeXYZ", "hl", ("mainnet",), HL_FIELDS,
        "https://docs.trade.xyz/", "hex128", prerequisites=("共享 HL 资金风险域，不得注册到另一写入服务",)),
    GridVenueSpec("katana", "Katana", "katana", ("mainnet", "testnet"),
        (F("account_address", "资金钱包地址", kind="evm_address"), F("api_key", "API Key"),
         F("api_secret", "API Secret", True), F("private_key", "钱包签名私钥", True, "evm_private_key")),
        "https://api-docs-v1-perps.katana.network/", settlement_asset='vbUSDC'),
    GridVenueSpec("backpack", "Backpack", "backpack", ("mainnet",),
        (F("api_key", "API 公钥", kind="ed25519_public"), F("api_secret", "API 签名私钥", True, "ed25519_secret")),
        "https://docs.backpack.exchange/", "numeric", 2**32-1),
    GridVenueSpec("bulk", "Bulk", "bulk", ("mainnet", "testnet"),
        (F("secret_key", "资金账户钱包私钥（当前仅主钱包）", True, "base58_secret"),), "https://docs.bulk.trade/",
        prerequisites=("首批仅主钱包全仓；代理钱包、子账户及逐仓需要分别实现和验收",)),
    GridVenueSpec("ondo", "Ondo", "ondo", ("mainnet", "testnet"),
        (F("api_key_id", "API Key ID"), F("api_secret", "API Secret", True)), "https://docs.ondoperps.xyz/"),
    GridVenueSpec("arcus", "Arcus", "arcus", ("mainnet", "testnet"),
        (F("account_address", "资金账户地址", kind='evm_address'), F("api_key", "API 公钥", kind='hex32'),
         F("api_secret", "API 签名私钥", True, 'hex32')), "https://docs.arcus.xyz/"),
    GridVenueSpec("perpl", "Perpl", "perpl", ("mainnet",),
        (F("api_key", "API Key"), F("api_secret", "API 签名私钥", True, 'hex32')), "https://docs.perpl.xyz/",
        prerequisites=("订单寿命、到期续挂及 Open/Close 持仓语义需专项验证",), settlement_asset='AUSD'),
)
VENUES = {s.key: s for s in SPECS}


def spec_for(key):
    try:
        return VENUES[key]
    except (KeyError, TypeError):
        raise ValueError("GRID_VENUE_UNSUPPORTED") from None


def account_identity(venue, network, owner):
    spec = spec_for(venue)
    if network not in spec.networks or not isinstance(owner, str) or not owner:
        raise ValueError("GRID_ACCOUNT_IDENTITY_INVALID")
    if spec.protocol == 'hl' or spec.key in ('katana', 'arcus'):
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', owner):
            raise ValueError('GRID_ACCOUNT_IDENTITY_INVALID')
        owner = owner.lower()
    # Preserve all existing HL account IDs. TradeXYZ shares that risk domain.
    domain = "hl" if spec.protocol == "hl" else spec.key
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"grid:{domain}:{network}:{owner}"))


def validate_fields(spec, values, previous=None):
    """Format checks only; native authorization is a separate read-only step."""
    if not isinstance(values, dict) or set(values) - {f.key for f in spec.credentials}:
        raise ValueError("GRID_CREDENTIAL_FIELDS_INVALID")
    result = {}
    for field in spec.credentials:
        value = values.get(field.key, "")
        if value == "" and previous:
            value = previous.get(field.key, "")
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or value != value.strip():
            raise ValueError("GRID_CREDENTIAL_FORMAT_INVALID")
        if field.kind == "uint":
            if not re.fullmatch(r"0|[1-9][0-9]{0,19}", value) or int(value) > 2**63-1:
                raise ValueError("GRID_CREDENTIAL_FORMAT_INVALID")
        if field.kind == "evm_address":
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
                raise ValueError("GRID_CREDENTIAL_FORMAT_INVALID")
            value = value.lower()
        if field.kind in ("evm_private_key", "hex", 'hex32'):
            raw = value.removeprefix("0x")
            if not re.fullmatch(r"[0-9a-fA-F]+", raw) or len(raw) % 2 or (field.kind in ('evm_private_key', 'hex32') and len(raw) != 64):
                raise ValueError("GRID_CREDENTIAL_FORMAT_INVALID")
        if field.kind == 'base58_secret':
            from .key_formats import solana_public_key
            solana_public_key(value)
        if field.kind.startswith("ed25519_"):
            import base64
            try:
                decoded = base64.b64decode(value, validate=True)
                if len(decoded) not in ((32,) if field.kind == "ed25519_public" else (32, 64)):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("GRID_CREDENTIAL_FORMAT_INVALID") from None
        result[field.key] = value
    return result

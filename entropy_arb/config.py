"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + hedge venue) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--hedge). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from .maker import MakerParams

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz", "katana")

# venues the BASE (entropy) leg may run on. Historically hard-wired to
# Hyperliquid; "lighter" unlocks the Lighter-vs-Katana line where the whole
# edge lives (BASIS-EXPLORE.md: Katana maker + Lighter taker ≈ 0.95bp toll).
BASE_VENUES = ("hl", "lighter", "lighter-rh", "katana")

# venues that implement the maker contract (maker_capable=True) and may run
# with maker.enabled — the console pre-flights this at launch
MAKER_VENUES = ("katana",)


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class KatanaCreds:
    """Katana Perps API credentials: HMAC key/secret for request auth plus
    the EOA private key whose signature authorizes every order (EIP-712).
    The wallet address is derived from the private key; KATANA_WALLET
    overrides it only for exotic setups."""
    api_key: Optional[str]
    api_secret: Optional[str]
    private_key: Optional[str]
    wallet_address: Optional[str] = None

    @property
    def complete(self) -> bool:
        return (bool(self.api_key) and bool(self.api_secret)
                and bool(self.private_key))


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter" | "katana"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None
    # katana
    katana_creds: Optional[KatanaCreds] = None


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    max_signal_edge_bps: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    recorder_max_spread_bps: float
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # embedded web ui (read-only state server; opt-in)
    web_enabled: bool
    web_host: str
    web_port: int
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL
    # maker mode (MAKER-DESIGN.md)
    maker: MakerParams = None

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
            if v.kind == "katana" and not (v.katana_creds
                                           and v.katana_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "entropy": {
        # symbol: base-leg ticker when it differs from --symbol, e.g.
        # base "BTC" on Lighter vs hedge "BTC-USD" on Katana. Defaults to
        # --symbol (mirrors hedge.symbol).
        "symbol": str,
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        # symbol: hedge-leg ticker when it differs from --symbol, e.g.
        # entropy "ANTH" vs lighter-rh "ANTHROPIC". Defaults to --symbol.
        "symbol": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        # refuse to chase an executable edge this far out (bps, 0 = off):
        # dislocations far outside the band are almost always a phantom
        # top-of-book on the thin entropy book — the entry leg never fills
        # while the hedge leg does
        "max_signal_edge_bps": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
        # drop 1s samples whose top-of-book is wider than this (bps of mid);
        # a lone far-out quote on a thin venue otherwise fabricates a
        # hundreds-of-bps premium and a phantom executable edge. 0 = off.
        "max_spread_bps": float,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
    # session-aware band auto-calibration, driven by tools/auto_band.py:
    # midline = trailing per-ET-session premium median, width = k * session
    # stdev (optionally recency-weighted) with a floor derived from measured
    # trade slippage
    "auto_band": {
        "enabled": bool,
        "window_days": float,
        "width_k": float,
        "min_width_bps": float,
        # recency-weighted session stdev: weight halves every N hours, so a
        # volatility spike fades out in days rather than window-lengths.
        # 0 = plain stdev over the whole window
        "sigma_halflife_h": float,
        # slippage floor input: its own (shorter) lookback and a minimum
        # number of complete fills before the floor is trusted at all
        "slip_lookback_days": float,
        "slip_min_fills": int,
        # skip calibrating a profile whose engine log has been idle this
        # many minutes (engine down); 0 disables the check
        "skip_engine_down_min": float,
    },
    # maker mode (MAKER-DESIGN.md §7): mutually exclusive with the taker
    # band strategy — when enabled, the band scanner does not run
    "maker": {
        "enabled": bool,
        "edge_bps": float,
        "costs_bps": float,
        "requote_bps": float,
        "requote_sec": float,
        "size_base": float,
        "sides": str,
        "hedge_batch_ms": int,
        "max_hedge_failures": int,
        "hedge_retry_sec": float,
        "interval_sec": float,
        "vol_widen_k": float,
        "vol_widen_window_min": int,
        "vol_widen_cap_bps": float,
        "trades_csv": str,
        "selection_csv": str,
    },
    "web": {
        "enabled": bool,
        "host": str,
        "port": int,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


def read_band(path: str) -> "tuple[float, float, float]":
    """Fully-validated (midline_bps, upper_bps, lower_bps) from a config or
    profile file. Used by the engine's hot-reload so a file edit (manual, or
    written by tools/auto_band.py) reaches a running worker without a
    restart. Raises ConfigError on invalid files — callers decide whether to
    keep the current band."""
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    _validate(raw, _SCHEMA)
    thr = raw.get("thresholds") or {}
    missing = [k for k in ("midline_bps", "upper_bps", "lower_bps")
               if k not in thr]
    if missing:
        raise ConfigError(f"'thresholds' missing {', '.join(missing)}")
    return (float(thr["midline_bps"]), float(thr["upper_bps"]),
            float(thr["lower_bps"]))


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


def _env_first_s(*names: str) -> Optional[str]:
    """First name that is set — explicit leg-specific vars beat the shared
    fallback. Presence is tested with `is not None` so a value like "0" wins
    instead of falling through."""
    for n in names:
        v = _env_s(n)
        if v is not None:
            return v
    return None


def _env_first_i(*names: str) -> Optional[int]:
    """Integer twin of _env_first_s (account index 0 is a valid value)."""
    for n in names:
        v = _env_i(n)
        if v is not None:
            return v
    return None


def lighter_creds(leg: str) -> LighterCreds:
    """Credentials for one Lighter leg.

    ``leg`` is "BASE" or "HEDGE". The leg-specific triple
    (LIGHTER_BASE_* / LIGHTER_HEDGE_*) wins when present; otherwise the shared
    LIGHTER_* triple is used, so single-account setups need no new variables.

    Why per-leg: the two zkLighter deployments (mainnet chain 304 vs
    Robinhood chain 466324) are separate accounts with separate API keys.
    Trading Lighter-mainnet as the base leg while another worker hedges on
    lighter-rh needs both key sets in the same .env — one shared triple would
    make the two lines impossible to run side by side.
    """
    leg = leg.upper()
    return LighterCreds(
        _env_first_i(f"LIGHTER_{leg}_ACCOUNT_INDEX", "LIGHTER_ACCOUNT_INDEX"),
        _env_first_i(f"LIGHTER_{leg}_API_KEY_INDEX", "LIGHTER_API_KEY_INDEX"),
        _env_first_s(f"LIGHTER_{leg}_API_PRIVATE_KEY",
                     "LIGHTER_API_PRIVATE_KEY"))


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str,
                base_venue: str = "hl") -> Config:
    # override=True: .env is the single source of truth. The console spawns
    # workers with an inherited environment that may hold STALE HL_*/LIGHTER_*
    # values (an earlier load_config call mutated the console's os.environ);
    # with the default override=False a stale env var would silently beat a
    # freshly-saved .env. See HANDOVER "单一事实源" design rule.
    load_dotenv(env_file, override=True)
    try:
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")
    if base_venue not in BASE_VENUES:
        raise ConfigError(
            f"--base must be one of {list(BASE_VENUES)}, got {base_venue!r} / "
            f"--base 必须是 {list(BASE_VENUES)} 之一")
    if base_venue == hedge_venue:
        raise ConfigError(
            f"--base {base_venue!r} and --hedge {hedge_venue!r} are the same "
            f"deployment — the two legs must be different venues / 两条腿不能"
            f"是同一个市场")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    mk = raw.get("maker") or {}
    maker = MakerParams(
        enabled=bool(mk.get("enabled", False)),
        edge_bps=float(mk.get("edge_bps", 2.0)),
        costs_bps=float(mk.get("costs_bps", 5.5)),
        requote_bps=float(mk.get("requote_bps", 1.0)),
        requote_sec=float(mk.get("requote_sec", 30.0)),
        size_base=float(mk.get("size_base", 0.005)),
        sides=str(mk.get("sides", "both")),
        hedge_batch_ms=int(mk.get("hedge_batch_ms", 250)),
        max_hedge_failures=int(mk.get("max_hedge_failures", 3)),
        hedge_retry_sec=float(mk.get("hedge_retry_sec", 0.5)),
        interval_sec=float(mk.get("interval_sec", 0.5)),
        vol_widen_k=float(mk.get("vol_widen_k", 2.0)),
        vol_widen_window_min=int(mk.get("vol_widen_window_min", 120)),
        vol_widen_cap_bps=float(mk.get("vol_widen_cap_bps", 15.0)),
        trades_csv=str(mk.get("trades_csv", "logs/maker-trades.csv")),
        selection_csv=str(mk.get("selection_csv",
                                 "logs/maker-selection.csv")))
    if maker.enabled:
        if maker.sides not in ("both", "bid", "ask"):
            raise ConfigError(
                f"maker.sides must be one of both|bid|ask, got "
                f"{maker.sides!r}")
        for name, val in (("edge_bps", maker.edge_bps),
                          ("costs_bps", maker.costs_bps),
                          ("requote_bps", maker.requote_bps),
                          ("requote_sec", maker.requote_sec),
                          ("size_base", maker.size_base),
                          ("hedge_retry_sec", maker.hedge_retry_sec),
                          ("interval_sec", maker.interval_sec)):
            if val <= 0:
                raise ConfigError(
                    f"maker.{name} must be > 0, got {val}")
        if maker.hedge_batch_ms < 0 or maker.max_hedge_failures < 1:
            raise ConfigError(
                "maker.hedge_batch_ms must be >= 0 and "
                "maker.max_hedge_failures >= 1")
        if maker.vol_widen_k < 0 or maker.vol_widen_cap_bps < 0:
            raise ConfigError(
                "maker.vol_widen_k / vol_widen_cap_bps must be >= 0")
        if maker.vol_widen_window_min < 10:
            raise ConfigError(
                f"maker.vol_widen_window_min must be >= 10, got "
                f"{maker.vol_widen_window_min}")

    entropy_dex = _get(raw, "entropy", "dex", "io")
    if base_venue == "hl" and hedge_venue == "tradexyz" \
            and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / 两条腿是同一个市场")

    # base-leg ticker override (mirrors hedge.symbol): the two venues may
    # name the same market differently, e.g. "BTC" on Lighter vs "BTC-USD"
    # on Katana
    entropy_symbol = str(_get(raw, "entropy", "symbol", symbol)
                         or symbol).strip().upper()

    if base_venue == "lighter" or base_venue == "lighter-rh":
        entropy = VenueConf(
            key="entropy", kind="lighter",
            label="LIGHTER" if base_venue == "lighter" else "LIGHTER-RH",
            symbol=entropy_symbol,
            fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
            cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
            lighter_profile=LIGHTER_PROFILES[base_venue],
            lighter_creds=lighter_creds("BASE"),
        )
    elif base_venue == "katana":
        entropy = VenueConf(
            key="entropy", kind="katana", label="KATANA",
            symbol=entropy_symbol,
            fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 1.9)),
            cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 30)),
            katana_creds=KatanaCreds(
                _env_s("KATANA_API_KEY"),
                _env_s("KATANA_API_SECRET"),
                _env_s("KATANA_PRIVATE_KEY"),
                _env_s("KATANA_WALLET")),
        )
    else:
        entropy = VenueConf(
            key="entropy", kind="hl", label="ENTROPY",
            symbol=entropy_symbol,
            fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
            cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
            hl_dex=entropy_dex,
            hl_creds=HLCreds(_env_s("HL_PRIVATE_KEY"),
                             _env_s("HL_ACCOUNT_ADDRESS")),
        )

    # hedge-leg ticker override: the two venues may name the same market
    # differently (e.g. entropy "ANTH" vs lighter-rh "ANTHROPIC")
    hedge_symbol = str(_get(raw, "hedge", "symbol", symbol) or symbol).strip().upper()

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=hedge_symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 120)),
            hl_dex="xyz",
            hl_creds=HLCreds(
                _env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                _env_s("HL_ACCOUNT_ADDRESS_XYZ") or _env_s("HL_ACCOUNT_ADDRESS")),
        )
    elif hedge_venue == "katana":
        hedge = VenueConf(
            key="hedge", kind="katana", label="KATANA",
            symbol=hedge_symbol,
            # live market-level taker fee is ~1.9 bps (the API serves the
            # exact value; the config number stays the explicit source of
            # truth so a fee change can never silently move the thresholds)
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.9)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            katana_creds=KatanaCreds(
                _env_s("KATANA_API_KEY"),
                _env_s("KATANA_API_SECRET"),
                _env_s("KATANA_PRIVATE_KEY"),
                _env_s("KATANA_WALLET")),
        )
    else:
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=hedge_symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 0.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=lighter_creds("HEDGE"),
        )

    return Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        max_signal_edge_bps=float(_get(raw, "execution", "max_signal_edge_bps", 0.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_get(raw, "recorder", "csv", "logs/minutes.csv"),
        recorder_max_spread_bps=float(
            _get(raw, "recorder", "max_spread_bps", 50.0)),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
        web_enabled=bool(_get(raw, "web", "enabled", False)),
        web_host=str(_get(raw, "web", "host", "127.0.0.1")),
        web_port=int(_get(raw, "web", "port", 8787)),
        maker=maker,
    )

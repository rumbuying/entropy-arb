r"""Strict DTOs and value contracts for the grid product (spec §4, handoff §4).

Rules encoded here and nowhere else:

- Decimal values cross every boundary as TEXT matching
  ``^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`` — no exponent, no whitespace, no
  NaN/Inf, no JSON numbers, no bool. Canonical form strips redundant
  fraction zeros; negative zero becomes ``0``; canonicalization never
  passes through float.
- IDs and big native integers are strings end to end (JSON-safe, JS-Number
  safe). ``grid_count``/``revision``/``schema_version`` are real ints;
  bools and ``20.0`` are rejected.
- Timestamps are timezone-aware ISO 8601. Source time vs receive time are
  separate fields. Tests inject a clock; nothing here reads wall time into
  a hash.
- Availability: unknown is ``None`` + a reason string, never 0/false.
- DTOs are frozen dataclasses with explicit ``from_dict``/``to_dict``.
  Unknown dict keys raise ``GridModelError``; coercion is forbidden.
- Hashing: ``config_hash`` = SHA-256 of canonical UTF-8 JSON (sort_keys,
  compact separators, ensure_ascii=False, allow_nan=False) of the config's
  ``to_dict``; ``plan_hash`` binds the exact field list in
  ``PLAN_HASH_FIELDS`` (documented in planning/grid/CONTRACTS.md).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
CALC_VERSION = "grid-calc-v1"

DECIMAL_TEXT_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

DIRECTIONS = ("long", "short", "neutral")
SPACINGS = ("arithmetic", "geometric")
MARGIN_MODES = ("cross", "isolated")
INITIAL_POSITIONS = ("deferred", "immediate")
PLAN_KINDS = ("fixture", "observed")
VALIDATION_STATUSES = ("VALID", "INVALID", "BLOCKED")
CAP_EVIDENCE_STATES = ("DOCUMENTED", "IMPLEMENTED_UNVERIFIED", "TEST_VERIFIED",
                       "LIVE_VERIFIED", "UNKNOWN", "UNSUPPORTED")
SUBMIT_STATES = ("ACCEPTED", "REJECTED", "UNKNOWN")
LOOKUP_STATES = ("FOUND_OPEN", "FOUND_TERMINAL", "NOT_FOUND_TRANSIENT",
                 "ABSENT_FINAL", "UNKNOWN")
GRID_STATUSES = ("DRAFT", "READY", "WAITING_TRIGGER", "WAITING_RANGE",
                 "STARTING", "RUNNING", "PAUSING", "PAUSED", "REDUCE_ONLY",
                 "RECONFIGURING", "RECOVERING", "STOPPING", "CLOSED",
                 "STOPPED_WITH_POSITION", "NEEDS_ATTENTION")
DESIRED_MODES = ("ACTIVE", "PAUSED", "REDUCE_ONLY", "STOPPED")
INTENT_STATES = ("PLANNED", "DISPATCHING", "ACCEPTED", "OPEN", "PARTIAL",
                 "FILLED", "REJECTED", "CANCELED", "CANCEL_REQUESTED",
                 "CANCEL_PENDING", "UNKNOWN", "EXPIRED_EPOCH", "BLOCKED_SLOT")
INTENT_TERMINAL = ("FILLED", "REJECTED", "CANCELED", "EXPIRED_EPOCH")


class GridModelError(ValueError):
    """Strict-contract violation with a stable reason code (handoff §4)."""

    def __init__(self, reason: str, message: str,
                 field: Optional[str] = None) -> None:
        super().__init__(f"{reason}: {message}" + (f" ({field})" if field
                                                   else ""))
        self.reason = reason
        self.message = message
        self.field = field


# ------------------------------------------------------------- value parsing

def parse_decimal_text(value: Any, field: str, *,
                       allow_negative: bool = True) -> Decimal:
    """Strict DecimalText -> Decimal. Rejects bool/float/int/non-string/
    exponent/whitespace/NaN. ``allow_negative=False`` rejects negatives."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise GridModelError("DECIMAL_STRING_REQUIRED",
                             f"{field} must be a decimal string", field)
    if not DECIMAL_TEXT_RE.match(value):
        raise GridModelError(
            "DECIMAL_TEXT_FORMAT",
            f"{field} is not a plain decimal string: {value!r}", field)
    try:
        d = Decimal(value)
    except InvalidOperation:  # pragma: no cover - regex already excludes
        raise GridModelError("DECIMAL_TEXT_FORMAT",
                             f"{field} unparseable: {value!r}", field)
    if not d.is_finite():
        raise GridModelError("DECIMAL_TEXT_FORMAT",
                             f"{field} not finite: {value!r}", field)
    if not allow_negative and d < 0:
        raise GridModelError("DECIMAL_NEGATIVE_FORBIDDEN",
                             f"{field} must be >= 0", field)
    return d


def canonical_decimal_text(d: Decimal) -> str:
    """Canonical plain decimal text: no exponent, no trailing fraction
    zeros, ``-0`` -> ``0``. Input must already be a Decimal (callers parse
    the raw text themselves and keep it for audit)."""
    if not isinstance(d, Decimal):
        raise GridModelError("DECIMAL_OBJECT_REQUIRED",
                             f"canonicalize expects Decimal, got {type(d)!r}")
    if not d.is_finite():
        raise GridModelError("DECIMAL_TEXT_FORMAT",
                             "cannot canonicalize non-finite value")
    if d == 0:
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def decimal_field(value: Any, field: str, *, allow_negative: bool = True) \
        -> str:
    """Parse + canonicalize in one step; returns canonical text."""
    return canonical_decimal_text(
        parse_decimal_text(value, field, allow_negative=allow_negative))


def parse_strict_int(value: Any, field: str, *, minimum: Optional[int] = None,
                     maximum: Optional[int] = None) -> int:
    """True int only: bool is rejected, ``20.0`` (float) is rejected."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GridModelError("INTEGER_REQUIRED",
                             f"{field} must be a real integer", field)
    if minimum is not None and value < minimum:
        raise GridModelError("INTEGER_RANGE",
                             f"{field} below minimum {minimum}", field)
    if maximum is not None and value > maximum:
        raise GridModelError("INTEGER_RANGE",
                             f"{field} above maximum {maximum}", field)
    return value


def parse_iso_ts(value: Any, field: str) -> datetime:
    """Timezone-aware ISO 8601 only; naive timestamps are rejected."""
    if isinstance(value, bool) or not isinstance(value, str) \
            or not _TS_RE.match(value):
        raise GridModelError("TIMESTAMP_FORMAT",
                             f"{field} must be ISO-8601 with timezone",
                             field)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise GridModelError("TIMESTAMP_FORMAT",
                             f"{field} missing timezone", field)
    return dt


def format_iso_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise GridModelError("TIMESTAMP_FORMAT", "naive datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds") \
        .replace("+00:00", "Z")


def utc_now_iso(clock: Optional[Any] = None) -> str:
    """Injectable-clock timestamp source. ``clock`` is a zero-arg callable
    returning a datetime (tests); production passes None."""
    dt = clock() if clock is not None else datetime.now(timezone.utc)
    return format_iso_ts(dt)


# -------------------------------------------------------------- dict helpers

def _reject_unknown(d: Dict[str, Any], known: Tuple[str, ...],
                    cls: str) -> None:
    unknown = sorted(set(d) - set(known))
    if unknown:
        raise GridModelError("UNKNOWN_FIELD",
                             f"unknown fields for {cls}: {unknown}")


def _require(d: Dict[str, Any], key: str, cls: str) -> Any:
    if key not in d or d[key] is None:
        raise GridModelError("MISSING_FIELD", f"{cls}.{key} is required", key)
    return d[key]


def _opt(d: Dict[str, Any], key: str) -> Any:
    return d.get(key)


def _opt_bool(d: Dict[str, Any], key: str, default: bool) -> bool:
    value = d.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise GridModelError("VALIDATION_ERROR", f"{key} must be a boolean", key)
    return value


# ------------------------------------------------------------------- hashing

def canonical_json_bytes(payload: Any) -> bytes:
    """sort_keys + compact separators + ensure_ascii=False + no NaN. This is
    the ONLY serializer that feeds hashes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


# ------------------------------------------------------------------ identity

@dataclass(frozen=True)
class SignerScope:
    """Exchange nonce/signing concurrency scope — independent of
    account_id. HL agent address, Lighter account_index|api_key_index etc.
    resolve separately per venue (spec §4.1)."""
    venue: str
    deployment: str
    scope: str            # canonical identity inside venue+deployment

    def to_dict(self) -> Dict[str, Any]:
        return {"venue": self.venue, "deployment": self.deployment,
                "scope": self.scope}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignerScope":
        _reject_unknown(d, ("venue", "deployment", "scope"), "SignerScope")
        return cls(venue=_require(d, "venue", "SignerScope"),
                   deployment=_opt(d, "deployment") or "",
                   scope=_require(d, "scope", "SignerScope"))


@dataclass(frozen=True)
class AccountIdentity:
    """The economic account: venue + deployment + authenticated owner.
    Different API keys of one account share one account_id; profile names
    and key nicknames never enter this identity (handoff §4)."""
    account_id: str            # stable UUID minted by the resolver
    venue: str
    deployment: str            # "" is a distinct, valid value (HL main dex)
    owner: str                 # authenticated owner/subaccount identity
    fixture: bool = False      # fixture identities can never trade live
    credential_revision: str = ""

    @property
    def auth_key(self) -> str:
        return "|".join((self.venue, self.deployment, self.owner))

    def to_dict(self) -> Dict[str, Any]:
        return {"account_id": self.account_id, "venue": self.venue,
                "deployment": self.deployment, "owner": self.owner,
                "fixture": self.fixture,
                "credential_revision": self.credential_revision}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AccountIdentity":
        _reject_unknown(d, ("account_id", "venue", "deployment", "owner",
                            "fixture", "credential_revision"),
                        "AccountIdentity")
        return cls(
            account_id=_require(d, "account_id", "AccountIdentity"),
            venue=_require(d, "venue", "AccountIdentity"),
            deployment=_opt(d, "deployment") or "",
            owner=_require(d, "owner", "AccountIdentity"),
            fixture=bool(d.get("fixture", False)),
            credential_revision=_opt(d, "credential_revision") or "")


@dataclass(frozen=True)
class MarketIdentity:
    """deployment + native instrument id + dex + product type + settlement
    asset. The display symbol is NOT a key. HL main-dex ``""`` is distinct
    from any named dex."""
    deployment: str
    native_instrument_id: str
    dex: str                   # "" distinct from named dexes
    product_type: str          # linear_perp only in v1
    settlement_asset: str

    def __post_init__(self) -> None:
        for name in ("deployment", "native_instrument_id", "dex",
                     "product_type", "settlement_asset"):
            v = getattr(self, name)
            if not isinstance(v, str) or "|" in v:
                raise GridModelError(
                    "MARKET_IDENTITY_FORMAT",
                    f"{name} must be a string without '|'", name)
        if self.product_type != "linear_perp":
            raise GridModelError("UNSUPPORTED_PRODUCT",
                                 f"product {self.product_type!r} not "
                                 "supported in v1")

    @property
    def market_key(self) -> str:
        return "|".join((self.deployment, self.dex,
                         self.native_instrument_id, self.product_type,
                         self.settlement_asset))

    def to_dict(self) -> Dict[str, Any]:
        return {"deployment": self.deployment,
                "native_instrument_id": self.native_instrument_id,
                "dex": self.dex, "product_type": self.product_type,
                "settlement_asset": self.settlement_asset}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MarketIdentity":
        _reject_unknown(d, ("deployment", "native_instrument_id", "dex",
                            "product_type", "settlement_asset"),
                        "MarketIdentity")
        return cls(deployment=_opt(d, "deployment") or "",
                   native_instrument_id=_require(d, "native_instrument_id",
                                                 "MarketIdentity"),
                   dex=_opt(d, "dex") or "",
                   product_type=_require(d, "product_type", "MarketIdentity"),
                   settlement_asset=_require(d, "settlement_asset",
                                             "MarketIdentity"))


# ------------------------------------------------------------ config + plan

@dataclass(frozen=True)
class CapitalPlan:
    total: str                # DecimalText, >= 0
    reserve: str              # DecimalText, >= 0, < total
    asset: str                # settlement/quote asset

    def to_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "reserve": self.reserve,
                "asset": self.asset}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CapitalPlan":
        _reject_unknown(d, ("total", "reserve", "asset"), "CapitalPlan")
        cap = cls(total=decimal_field(_require(d, "total", "CapitalPlan"),
                                      "capital.total"),
                  reserve=decimal_field(_require(d, "reserve", "CapitalPlan"),
                                        "capital.reserve"),
                  asset=str(_require(d, "asset", "CapitalPlan")))
        if Decimal(cap.total) <= 0:
            raise GridModelError("VALIDATION_ERROR", "capital.total must "
                                 "be > 0", "capital.total")
        if Decimal(cap.reserve) < 0 or Decimal(cap.reserve) >= Decimal(cap.total):
            raise GridModelError("VALIDATION_ERROR",
                                 "capital.reserve must satisfy "
                                 "0 <= reserve < total", "capital.reserve")
        return cap


@dataclass(frozen=True)
class ServiceCoveragePolicy:
    max_open_notional: str
    max_seconds: int

    def to_dict(self):
        return {'max_open_notional': self.max_open_notional, 'max_seconds': self.max_seconds}

    @classmethod
    def from_dict(cls, value):
        field = 'protection.service_coverage'
        _reject_unknown(value, ('max_open_notional', 'max_seconds'), field)
        cap = decimal_field(_require(value, 'max_open_notional', field), field+'.max_open_notional')
        if Decimal(cap) <= 0:
            raise GridModelError('VALIDATION_ERROR', 'coverage notional limit must be positive', field)
        seconds = parse_strict_int(_require(value, 'max_seconds', field), field+'.max_seconds', minimum=1, maximum=30)
        return cls(cap, seconds)


@dataclass(frozen=True)
class PriceProtection:
    lower_stop: Optional[str] = None
    upper_stop: Optional[str] = None
    service_coverage: Optional[ServiceCoveragePolicy] = None

    def to_dict(self):
        result = {'exit_action': 'flatten'}
        if self.lower_stop is not None:
            result['stop_loss'] = {'type': 'price', 'source': 'mark', 'operator': 'lte', 'value': self.lower_stop}
        if self.upper_stop is not None:
            result['upper_stop_loss'] = {'type': 'price', 'source': 'mark', 'operator': 'gte', 'value': self.upper_stop}
        if self.service_coverage is not None:
            result['service_coverage'] = self.service_coverage.to_dict()
        return result

    @classmethod
    def from_dict(cls, value, direction):
        _reject_unknown(value, ('stop_loss', 'upper_stop_loss', 'take_profit', 'exit_action', 'service_coverage'), 'protection')
        if 'take_profit' in value:
            raise GridModelError('FEATURE_UNAVAILABLE', 'take profit protection is not yet implemented', 'protection.take_profit')
        if value.get('exit_action', 'flatten') != 'flatten':
            raise GridModelError('FEATURE_UNAVAILABLE', 'price protection currently requires flatten', 'protection.exit_action')
        stops = {}
        for key in ('stop_loss', 'upper_stop_loss'):
            if key not in value:
                continue
            rule = value[key]
            _reject_unknown(rule, ('type', 'source', 'operator', 'value'), 'protection.' + key)
            if rule.get('type') != 'price' or rule.get('source') != 'mark':
                raise GridModelError('FEATURE_UNAVAILABLE', 'protection requires a mark-price rule', 'protection.' + key)
            operator = rule.get('operator')
            if operator not in ('lte', 'gte'):
                raise GridModelError('VALIDATION_ERROR', 'protection operator must be lte or gte', 'protection.' + key)
            price = decimal_field(_require(rule, 'value', 'protection.' + key), 'protection.' + key + '.value')
            if Decimal(price) <= 0 or operator in stops:
                raise GridModelError('VALIDATION_ERROR', 'protection prices must be positive and directions unique', 'protection')
            stops[operator] = price
        required = {'lte'} if direction == 'long' else {'gte'} if direction == 'short' else {'lte', 'gte'}
        if set(stops) != required:
            raise GridModelError('VALIDATION_ERROR', 'stop directions do not cover the grid direction', 'protection')
        if len(stops) == 2 and Decimal(stops['lte']) >= Decimal(stops['gte']):
            raise GridModelError('VALIDATION_ERROR', 'lower stop must be below upper stop', 'protection')
        return cls(lower_stop=stops.get('lte'), upper_stop=stops.get('gte'),
            service_coverage=ServiceCoveragePolicy.from_dict(value['service_coverage']) if 'service_coverage' in value else None)


@dataclass(frozen=True)
class ProfitProtection:
    basis: str
    take_profit: Optional[str] = None
    stop_loss: Optional[str] = None

    def to_dict(self):
        result = {'basis': self.basis, 'source': 'net_ledger',
                  'denominator': 'initial_allocation', 'exit_action': 'flatten'}
        for key in ('take_profit', 'stop_loss'):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        return result

    @classmethod
    def from_dict(cls, value):
        _reject_unknown(value, ('basis', 'source', 'denominator', 'exit_action',
                                'take_profit', 'stop_loss'), 'profit_protection')
        if (value.get('basis') not in ('net_pnl', 'roi')
                or value.get('source') != 'net_ledger'
                or value.get('denominator') != 'initial_allocation'
                or value.get('exit_action') != 'flatten'):
            raise GridModelError('FEATURE_UNAVAILABLE', 'unsupported profit protection rule', 'profit_protection')
        thresholds = {}
        for key in ('take_profit', 'stop_loss'):
            if key in value:
                number = decimal_field(value[key], 'profit_protection.' + key)
                if (Decimal(number) <= 0 if key == 'take_profit' else Decimal(number) >= 0):
                    raise GridModelError('VALIDATION_ERROR', 'take profit must be positive; stop loss must be negative', 'profit_protection.' + key)
                thresholds[key] = number
        if not thresholds:
            raise GridModelError('VALIDATION_ERROR', 'at least one profit threshold is required', 'profit_protection')
        return cls(basis=value['basis'], **thresholds)


@dataclass(frozen=True)
class GridConfig:
    """The strict create/preview configuration (spec §4.2). Mark price stops
    are explicit; unsupported advanced fields (trailing/templates) are rejected with
    FEATURE_UNAVAILABLE rather than silently ignored."""
    schema_version: int
    strategy_type: str           # futures_grid only in v1
    account_ref: str             # account_id (UUID)
    credential_ref: str
    market_ref: str              # market_key
    direction: str
    spacing: str
    lower_price: str             # DecimalText raw display input
    upper_price: str
    grid_count: int              # number of INTERVALS; levels = N+1
    capital: CapitalPlan
    leverage: int
    margin_mode: str
    initial_position: str        # deferred | immediate (immediate: long/short)
    start: str                   # immediate | price | timestamp (v1:
                                 #   immediate validated, others FEATURE_)
    execution_policy: str = "post_only"   # post_only | gtc
    protection: Optional[PriceProtection] = None
    profit_protection: Optional[ProfitProtection] = None
    # True (default): the runtime budget revalidation pauses the grid when the
    # plan's adverse requirement no longer fits the account (RISK_BUDGET_
    # EXCEEDED). False: the operator chose to keep the grid RUNNING instead —
    # the observation is recorded and the venue remains the final margin gate.
    pause_on_budget: bool = True

    # canonical (tick-rounded) bounds are computed by the planner, never
    # user-typed; they live on the Plan, not here.
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "strategy_type": self.strategy_type,
            "account_ref": self.account_ref,
            "credential_ref": self.credential_ref,
            "market_ref": self.market_ref,
            "direction": self.direction,
            "spacing": self.spacing,
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "grid_count": self.grid_count,
            "capital": self.capital.to_dict(),
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "initial_position": self.initial_position,
            "start": self.start,
            "execution_policy": self.execution_policy,
        }
        # Only an explicit opt-out enters the canonical form: the default
        # (True) must hash exactly like configs created before the field
        # existed, or every running grid's operator-trial record would
        # mismatch its config hash and revoke its own scope
        # (OPERATOR_TRIAL_SCOPE_REVOKED, 2026-11-06 19:27).
        if not self.pause_on_budget:
            result["pause_on_budget"] = False
        if self.protection is not None:
            result['protection'] = self.protection.to_dict()
        if self.profit_protection is not None:
            result['profit_protection'] = self.profit_protection.to_dict()
        return result

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GridConfig":
        _reject_unknown(d, ("schema_version", "strategy_type", "account_ref",
                            "credential_ref", "market_ref", "direction",
                            "spacing", "lower_price", "upper_price",
                            "grid_count", "capital", "leverage",
                            "margin_mode", "initial_position", "start",
                            "execution_policy", "protection", "profit_protection", "trailing",
                            "pause_on_budget"),
                        "GridConfig")
        # P1 features declared in the spec example: refuse, never ignore
        for p1 in ("trailing",):
            if p1 in d:
                raise GridModelError(
                    "FEATURE_UNAVAILABLE",
                    f"field {p1} is P1 and not yet implemented", p1)
        cfg = cls(
            schema_version=parse_strict_int(_require(d, "schema_version",
                                                     "GridConfig"),
                                            "schema_version",
                                            minimum=1, maximum=1),
            strategy_type=_require(d, "strategy_type", "GridConfig"),
            account_ref=_require(d, "account_ref", "GridConfig"),
            credential_ref=_require(d, "credential_ref", "GridConfig"),
            market_ref=_require(d, "market_ref", "GridConfig"),
            direction=_require(d, "direction", "GridConfig"),
            spacing=_require(d, "spacing", "GridConfig"),
            lower_price=decimal_field(_require(d, "lower_price", "GridConfig"),
                                      "lower_price"),
            upper_price=decimal_field(_require(d, "upper_price", "GridConfig"),
                                      "upper_price"),
            grid_count=parse_strict_int(_require(d, "grid_count",
                                                 "GridConfig"),
                                        "grid_count"),
            capital=CapitalPlan.from_dict(_require(d, "capital",
                                                   "GridConfig")),
            leverage=parse_strict_int(_require(d, "leverage", "GridConfig"),
                                      "leverage", minimum=1, maximum=100),
            margin_mode=_require(d, "margin_mode", "GridConfig"),
            initial_position=_require(d, "initial_position", "GridConfig"),
            start=_require(d, "start", "GridConfig"),
            execution_policy=str(_opt(d, "execution_policy") or "post_only"),
            protection=PriceProtection.from_dict(d['protection'], d.get('direction')) if 'protection' in d else None,
            profit_protection=ProfitProtection.from_dict(d['profit_protection']) if 'profit_protection' in d else None,
            pause_on_budget=_opt_bool(d, "pause_on_budget", True),
        )
        cfg.validate_enums()
        return cfg

    def validate_enums(self) -> None:
        if self.strategy_type != "futures_grid":
            raise GridModelError("VALIDATION_ERROR",
                                 f"strategy_type {self.strategy_type!r} "
                                 "unsupported", "strategy_type")
        if self.direction not in DIRECTIONS:
            raise GridModelError("VALIDATION_ERROR",
                                 f"direction {self.direction!r}", "direction")
        if self.spacing not in SPACINGS:
            raise GridModelError("VALIDATION_ERROR",
                                 f"spacing {self.spacing!r}", "spacing")
        if self.margin_mode not in MARGIN_MODES:
            raise GridModelError("VALIDATION_ERROR",
                                 f"margin_mode {self.margin_mode!r}",
                                 "margin_mode")
        if self.initial_position not in INITIAL_POSITIONS:
            raise GridModelError("VALIDATION_ERROR",
                                 f"initial_position "
                                 f"{self.initial_position!r}",
                                 "initial_position")
        if self.initial_position == "immediate" \
                and self.direction == "neutral":
            raise GridModelError("VALIDATION_ERROR",
                                 "immediate initial position requires a "
                                 "directional grid", "initial_position")
        if self.start != "immediate":
            raise GridModelError("FEATURE_UNAVAILABLE",
                                 f"start type {self.start!r} is P1",
                                 "start")
        if self.execution_policy not in ("post_only", "gtc"):
            raise GridModelError("VALIDATION_ERROR",
                                 f"execution_policy "
                                 f"{self.execution_policy!r}",
                                 "execution_policy")
        if Decimal(self.lower_price) <= 0 or Decimal(self.upper_price) <= 0:
            raise GridModelError("VALIDATION_ERROR",
                                 "prices must be positive", "lower_price")
        if not Decimal(self.lower_price) < Decimal(self.upper_price):
            raise GridModelError("VALIDATION_ERROR",
                                 "lower_price must be < upper_price",
                                 "lower_price")

    @property
    def config_hash(self) -> str:
        """SHA-256 of the canonical form of this config. Raw display inputs
        and secrets never enter (they are not fields)."""
        return sha256_hex(self.to_dict())


@dataclass(frozen=True)
class PriceLevel:
    """One canonical grid line: level_index + price. Logic layers and
    actual OrderIntents are separate (handoff §4.1)."""
    level_index: int
    price: str                  # canonical DecimalText

    def to_dict(self) -> Dict[str, Any]:
        return {"level_index": self.level_index, "price": self.price}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PriceLevel":
        _reject_unknown(d, ("level_index", "price"), "PriceLevel")
        return cls(level_index=parse_strict_int(
                       _require(d, "level_index", "PriceLevel"),
                       "level_index", minimum=0),
                   price=decimal_field(_require(d, "price", "PriceLevel"),
                                       "price"))


@dataclass(frozen=True)
class CycleLayout:
    """A logical loop: cycle_index + entry/exit level indices. For long:
    entry=P_i, exit=P_{i+1}; short is symmetric (spec §8.1)."""
    cycle_index: int
    entry_level: int
    exit_level: int

    def to_dict(self) -> Dict[str, Any]:
        return {"cycle_index": self.cycle_index,
                "entry_level": self.entry_level,
                "exit_level": self.exit_level}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CycleLayout":
        _reject_unknown(d, ("cycle_index", "entry_level", "exit_level"),
                        "CycleLayout")
        return cls(parse_strict_int(_require(d, "cycle_index", "CycleLayout"),
                                    "cycle_index", minimum=0),
                   parse_strict_int(_require(d, "entry_level", "CycleLayout"),
                                    "entry_level", minimum=0),
                   parse_strict_int(_require(d, "exit_level", "CycleLayout"),
                                    "exit_level", minimum=0))


@dataclass(frozen=True)
class CandidateOrder:
    """A concrete initial order the plan wants sent (post post-only /
    bid-ask filtering). Logical layout lives in PriceLevel/CycleLayout."""
    level_index: int
    side: str                   # buy | sell
    qty_base: Optional[str]     # None when sizing unknown (reason attached)
    price: str
    purpose: str                # initial_open | initial_exit | rebalance
    status: str                 # SEND_CANDIDATE | WAIT_POST_ONLY |
                                # WAITING_RANGE | BLOCKED_*
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"level_index": self.level_index, "side": self.side,
                "qty_base": self.qty_base, "price": self.price,
                "purpose": self.purpose, "status": self.status,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CandidateOrder":
        _reject_unknown(d, ("level_index", "side", "qty_base", "price",
                            "purpose", "status", "reason"), "CandidateOrder")
        qb = _opt(d, "qty_base")
        return cls(level_index=parse_strict_int(
                       _require(d, "level_index", "CandidateOrder"),
                       "level_index", minimum=0),
                   side=_require(d, "side", "CandidateOrder"),
                   qty_base=decimal_field(qb, "qty_base") if qb is not None
                   else None,
                   price=decimal_field(_require(d, "price", "CandidateOrder"),
                                       "price"),
                   purpose=_require(d, "purpose", "CandidateOrder"),
                   status=_require(d, "status", "CandidateOrder"),
                   reason=_opt(d, "reason"))


@dataclass(frozen=True)
class Blocker:
    code: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Blocker":
        _reject_unknown(d, ("code", "reason"), "Blocker")
        return cls(code=_require(d, "code", "Blocker"),
                   reason=_require(d, "reason", "Blocker"))


PLAN_HASH_FIELDS = (
    "calc_version", "config_hash", "rules_revision", "capability_revision",
    "risk_snapshot_id", "account", "market", "quote_snapshot",
    "reference", "normalized_lower", "normalized_upper", "levels",
    "cycles", "qty_per_grid", "qty_reason", "initial_position_qty",
    "candidates", "plan_kind", "validation_status",
)
"""Exact plan_hash binding (handoff §4.2): canonical config + revisions +
account/market + risk snapshot id + quote snapshot identity + the actual
computed content. Random request ids and receive times are excluded."""


@dataclass(frozen=True)
class Plan:
    """Minimum plan output (handoff §4.1). fixture plans never yield
    live_eligible; business errors are INVALID/BLOCKED with stable codes —
    never a fake-healthy plan."""
    schema_version: int
    calc_version: str
    plan_kind: str                 # fixture | observed
    validation_status: str         # VALID | INVALID | BLOCKED
    live_eligible: bool            # M0: always False
    config_hash: str
    plan_hash: str
    rules_revision: str
    capability_revision: str
    risk_snapshot_id: Optional[str]   # None when no risk model (BLOCKED)
    account: str                      # account_id
    market: str                       # market_key
    reference_source: Optional[str]   # mark | last | None
    reference_price: Optional[str]
    normalized_lower: Optional[str]
    normalized_upper: Optional[str]
    levels: Tuple[PriceLevel, ...]
    cycles: Tuple[CycleLayout, ...]
    qty_per_grid: Optional[str]       # None + qty_reason when unsolved
    qty_reason: Optional[str]
    initial_position_qty: str         # "0" when none / unknown blocked
    candidate_orders: Tuple[CandidateOrder, ...]
    blockers: Tuple[Blocker, ...]
    code: Optional[str] = None        # INVALID/BLOCKED stable error code
    message: Optional[str] = None
    center_index: Optional[int] = None
    initialization_status: Optional[str] = None  # e.g. WAITING_RANGE
    quote_snapshot_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "calc_version": self.calc_version,
            "plan_kind": self.plan_kind,
            "validation_status": self.validation_status,
            "live_eligible": self.live_eligible,
            "config_hash": self.config_hash,
            "plan_hash": self.plan_hash,
            "rules_revision": self.rules_revision,
            "capability_revision": self.capability_revision,
            "risk_snapshot_id": self.risk_snapshot_id,
            "account": self.account,
            "market": self.market,
            "reference_source": self.reference_source,
            "reference_price": self.reference_price,
            "normalized_lower": self.normalized_lower,
            "normalized_upper": self.normalized_upper,
            "levels": [l.to_dict() for l in self.levels],
            "cycles": [c.to_dict() for c in self.cycles],
            "qty_per_grid": self.qty_per_grid,
            "qty_reason": self.qty_reason,
            "initial_position_qty": self.initial_position_qty,
            "candidate_orders": [c.to_dict() for c in self.candidate_orders],
            "blockers": [b.to_dict() for b in self.blockers],
            "code": self.code,
            "message": self.message,
            "center_index": self.center_index,
            "initialization_status": self.initialization_status,
            "quote_snapshot_id": self.quote_snapshot_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Plan":
        _reject_unknown(d, tuple(f.name for f in dc_fields(cls)), "Plan")
        return cls(
            schema_version=parse_strict_int(_require(d, "schema_version",
                                                     "Plan"),
                                            "schema_version", minimum=1),
            calc_version=_require(d, "calc_version", "Plan"),
            plan_kind=_require(d, "plan_kind", "Plan"),
            validation_status=_require(d, "validation_status", "Plan"),
            live_eligible=bool(_require(d, "live_eligible", "Plan")),
            config_hash=_require(d, "config_hash", "Plan"),
            plan_hash=_require(d, "plan_hash", "Plan"),
            rules_revision=_require(d, "rules_revision", "Plan"),
            capability_revision=_require(d, "capability_revision", "Plan"),
            risk_snapshot_id=_opt(d, "risk_snapshot_id"),
            account=_require(d, "account", "Plan"),
            market=_require(d, "market", "Plan"),
            reference_source=_opt(d, "reference_source"),
            reference_price=(_opt(d, "reference_price") or None),
            normalized_lower=_opt(d, "normalized_lower"),
            normalized_upper=_opt(d, "normalized_upper"),
            levels=tuple(PriceLevel.from_dict(x)
                         for x in (_require(d, "levels", "Plan"))),
            cycles=tuple(CycleLayout.from_dict(x) for x in d.get("cycles", ())),
            qty_per_grid=_opt(d, "qty_per_grid"),
            qty_reason=_opt(d, "qty_reason"),
            initial_position_qty=str(_require(d, "initial_position_qty",
                                              "Plan")),
            candidate_orders=tuple(CandidateOrder.from_dict(x)
                                   for x in d.get("candidate_orders", ())),
            blockers=tuple(Blocker.from_dict(x)
                           for x in d.get("blockers", ())),
            code=_opt(d, "code"),
            message=_opt(d, "message"),
            center_index=(_opt(d, "center_index") if _opt(d,
                                                          "center_index")
                          is None else parse_strict_int(d["center_index"],
                                                        "center_index",
                                                        minimum=0)),
            initialization_status=_opt(d, "initialization_status"),
            quote_snapshot_id=_opt(d, "quote_snapshot_id"),
        )


# ------------------------------------------------------- market + capability

@dataclass(frozen=True)
class InstrumentRules:
    """Native instrument constraints. Unknown limits are None + reason;
    nothing defaults to a permissive value (spec §5)."""
    market: str                    # market_key
    native_id: str
    product_type: str
    base_asset: str
    settlement_asset: str
    tick: str                      # DecimalText > 0
    qty_step: str                  # DecimalText > 0 (qty_base units)
    min_qty: Optional[str]
    min_notional: Optional[str]
    max_orders: Optional[int]
    multiplier: str                # native contract -> qty_base factor
    maker_fee_rate: Optional[str]  # fraction, e.g. "0.0002"
    taker_fee_rate: Optional[str]
    maintenance_tiers: Tuple[Dict[str, str], ...] = ()  # raw tier rows
    revision: str = ""
    source: str = ""
    unknown_reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"market": self.market, "native_id": self.native_id,
                "product_type": self.product_type,
                "base_asset": self.base_asset,
                "settlement_asset": self.settlement_asset,
                "tick": self.tick, "qty_step": self.qty_step,
                "min_qty": self.min_qty, "min_notional": self.min_notional,
                "max_orders": self.max_orders,
                "multiplier": self.multiplier,
                "maker_fee_rate": self.maker_fee_rate,
                "taker_fee_rate": self.taker_fee_rate,
                "maintenance_tiers": [dict(t) for t in self.maintenance_tiers],
                "revision": self.revision, "source": self.source,
                "unknown_reasons": list(self.unknown_reasons)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstrumentRules":
        _reject_unknown(d, tuple(f.name for f in dc_fields(cls)),
                        "InstrumentRules")
        return cls(
            market=_require(d, "market", "InstrumentRules"),
            native_id=_require(d, "native_id", "InstrumentRules"),
            product_type=_require(d, "product_type", "InstrumentRules"),
            base_asset=_require(d, "base_asset", "InstrumentRules"),
            settlement_asset=_require(d, "settlement_asset",
                                      "InstrumentRules"),
            tick=decimal_field(_require(d, "tick", "InstrumentRules"),
                               "tick"),
            qty_step=decimal_field(_require(d, "qty_step", "InstrumentRules"),
                                   "qty_step"),
            min_qty=(decimal_field(_opt(d, "min_qty"), "min_qty")
                     if _opt(d, "min_qty") is not None else None),
            min_notional=(decimal_field(_opt(d, "min_notional"),
                                        "min_notional")
                          if _opt(d, "min_notional") is not None else None),
            max_orders=(parse_strict_int(_opt(d, "max_orders"), "max_orders",
                                         minimum=1)
                        if _opt(d, "max_orders") is not None else None),
            multiplier=decimal_field(_require(d, "multiplier",
                                              "InstrumentRules"),
                                     "multiplier"),
            maker_fee_rate=(decimal_field(_opt(d, "maker_fee_rate"),
                                          "maker_fee_rate",
                                          allow_negative=True)
                            if _opt(d, "maker_fee_rate") is not None
                            else None),
            taker_fee_rate=(decimal_field(_opt(d, "taker_fee_rate"),
                                          "taker_fee_rate")
                            if _opt(d, "taker_fee_rate") is not None
                            else None),
            maintenance_tiers=tuple(dict(t) for t in
                                    d.get("maintenance_tiers", ())),
            revision=str(_opt(d, "revision") or ""),
            source=str(_opt(d, "source") or ""),
            unknown_reasons=tuple(d.get("unknown_reasons", ())))


@dataclass(frozen=True)
class CapabilityEvidence:
    """One venue/deployment/capability evidence row (handoff §7)."""
    venue: str
    deployment: str
    capability: str
    status: str               # CAP_EVIDENCE_STATES
    scope: str = ""
    evidence: str = ""        # source/URL/symbol
    checked_date: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"venue": self.venue, "deployment": self.deployment,
                "capability": self.capability, "status": self.status,
                "scope": self.scope, "evidence": self.evidence,
                "checked_date": self.checked_date, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CapabilityEvidence":
        _reject_unknown(d, ("venue", "deployment", "capability", "status",
                            "scope", "evidence", "checked_date", "notes"),
                        "CapabilityEvidence")
        ev = cls(venue=_require(d, "venue", "CapabilityEvidence"),
                 deployment=_opt(d, "deployment") or "",
                 capability=_require(d, "capability",
                                     "CapabilityEvidence"),
                 status=_require(d, "status", "CapabilityEvidence"),
                 scope=str(_opt(d, "scope") or ""),
                 evidence=str(_opt(d, "evidence") or ""),
                 checked_date=str(_opt(d, "checked_date") or ""),
                 notes=str(_opt(d, "notes") or ""))
        if ev.status not in CAP_EVIDENCE_STATES:
            raise GridModelError("VALIDATION_ERROR",
                                 f"capability status {ev.status!r}",
                                 "status")
        return ev


@dataclass(frozen=True)
class MarketSnapshot:
    """Quote snapshot. Unavailable fields are None + reason — never 0."""
    market: str
    source: str
    ts: str                       # ISO ts of the source data
    mark: Optional[str] = None
    bid: Optional[str] = None
    ask: Optional[str] = None
    last: Optional[str] = None
    mark_reason: Optional[str] = None
    book_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"market": self.market, "source": self.source, "ts": self.ts,
                "mark": self.mark, "bid": self.bid, "ask": self.ask,
                "last": self.last, "mark_reason": self.mark_reason,
                "book_reason": self.book_reason}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MarketSnapshot":
        _reject_unknown(d, ("market", "source", "ts", "mark", "bid", "ask",
                            "last", "mark_reason", "book_reason"),
                        "MarketSnapshot")
        num = ("mark", "bid", "ask", "last")
        return cls(market=_require(d, "market", "MarketSnapshot"),
                   source=_require(d, "source", "MarketSnapshot"),
                   ts=str(_require(d, "ts", "MarketSnapshot")),
                   **{k: (decimal_field(d[k], k) if d.get(k) is not None
                          else None) for k in num},
                   mark_reason=_opt(d, "mark_reason"),
                   book_reason=_opt(d, "book_reason"))


@dataclass(frozen=True)
class RiskSnapshot:
    """Account risk inputs for the planner (per-currency amounts; never
    summed across assets). Unknown = None + reason."""
    account: str
    snapshot_id: str
    ts: str
    assets: Tuple[Tuple[str, str], ...]   # (asset, DecimalText free)
    margin_mode: Optional[str]
    position_qty_base: Optional[str]      # signed; None + reason unknown
    position_reason: Optional[str] = None
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"account": self.account, "snapshot_id": self.snapshot_id,
                "ts": self.ts, "assets": [list(a) for a in self.assets],
                "margin_mode": self.margin_mode,
                "position_qty_base": self.position_qty_base,
                "position_reason": self.position_reason,
                "source": self.source}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RiskSnapshot":
        _reject_unknown(d, ("account", "snapshot_id", "ts", "assets",
                            "margin_mode", "position_qty_base",
                            "position_reason", "source"), "RiskSnapshot")
        assets = []
        for a in d.get("assets", ()):
            if not (isinstance(a, (list, tuple)) and len(a) == 2):
                raise GridModelError("VALIDATION_ERROR",
                                     "assets rows are [asset, amount]",
                                     "assets")
            assets.append((str(a[0]),
                           decimal_field(a[1], "assets.amount")))
        pos = _opt(d, "position_qty_base")
        return cls(account=_require(d, "account", "RiskSnapshot"),
                   snapshot_id=_require(d, "snapshot_id", "RiskSnapshot"),
                   ts=str(_require(d, "ts", "RiskSnapshot")),
                   assets=tuple(assets),
                   margin_mode=_opt(d, "margin_mode"),
                   position_qty_base=(decimal_field(pos,
                                                    "position_qty_base")
                                      if pos is not None else None),
                   position_reason=_opt(d, "position_reason"),
                   source=str(_opt(d, "source") or ""))


# ---------------------------------------------------------------- execution

@dataclass(frozen=True)
class ExecutionEvent:
    """Precise fill record (G05). Decimal-only; the float FillEvent of the
    legacy maker engine never feeds these values (spec §2)."""
    execution_key: str          # stable dedup key (adapter-defined scope)
    account: str
    market: str
    order_client_id: Optional[str]
    exchange_order_id: Optional[str]
    trade_id: Optional[str]     # native, string form
    side: str                   # buy | sell
    qty_base: str
    price: str
    fee_amount: Optional[str]
    fee_asset: Optional[str]
    is_maker: Optional[bool]
    exchange_ts: str
    received_ts: str
    source: str                 # stream | rest_backfill | adapter_specific
    source_seq: Optional[str]   # ordered source cursor, string (big ints)
    finality: str = "final"     # final | provisional | unknown

    def to_dict(self) -> Dict[str, Any]:
        return {"execution_key": self.execution_key, "account": self.account,
                "market": self.market,
                "order_client_id": self.order_client_id,
                "exchange_order_id": self.exchange_order_id,
                "trade_id": self.trade_id, "side": self.side,
                "qty_base": self.qty_base, "price": self.price,
                "fee_amount": self.fee_amount, "fee_asset": self.fee_asset,
                "is_maker": self.is_maker, "exchange_ts": self.exchange_ts,
                "received_ts": self.received_ts, "source": self.source,
                "source_seq": self.source_seq, "finality": self.finality}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExecutionEvent":
        _reject_unknown(d, tuple(f.name for f in dc_fields(cls)),
                        "ExecutionEvent")
        opt_num = {"fee_amount": True}
        side = _require(d, "side", "ExecutionEvent")
        if side not in ("buy", "sell"):
            raise GridModelError("VALIDATION_ERROR", f"side {side!r}",
                                 "side")
        fee = _opt(d, "fee_amount")
        return cls(
            execution_key=_require(d, "execution_key", "ExecutionEvent"),
            account=_require(d, "account", "ExecutionEvent"),
            market=_require(d, "market", "ExecutionEvent"),
            order_client_id=_opt(d, "order_client_id"),
            exchange_order_id=_opt(d, "exchange_order_id"),
            trade_id=_opt(d, "trade_id"),
            side=side,
            qty_base=decimal_field(_require(d, "qty_base", "ExecutionEvent"),
                                   "qty_base"),
            price=decimal_field(_require(d, "price", "ExecutionEvent"),
                                "price"),
            fee_amount=(decimal_field(fee, "fee_amount", allow_negative=True)
                        if fee is not None else None),
            fee_asset=_opt(d, "fee_asset"),
            is_maker=(bool(d["is_maker"])
                      if _opt(d, "is_maker") is not None else None),
            exchange_ts=str(_require(d, "exchange_ts", "ExecutionEvent")),
            received_ts=str(_require(d, "received_ts", "ExecutionEvent")),
            source=_require(d, "source", "ExecutionEvent"),
            source_seq=(str(_opt(d, "source_seq"))
                        if _opt(d, "source_seq") is not None else None),
            finality=str(_opt(d, "finality") or "final"))


@dataclass(frozen=True)
class OrderIntent:
    """Persistent order intent (G06). cause_key dedupes refills; epoch
    invalidates unsent requests on pause/stop."""
    intent_id: str
    grid_id: str
    cause_key: str              # unique per causal event
    revision: int               # active config revision that created it
    level_index: int
    side: str
    qty_base: str
    price: str
    policy: str                 # post_only | gtc | ioc | reduce_only_ioc
    reduce_only: bool
    epoch: int
    state: str = "PLANNED"
    client_id: Optional[str] = None
    created_ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"intent_id": self.intent_id, "grid_id": self.grid_id,
                "cause_key": self.cause_key, "revision": self.revision,
                "level_index": self.level_index, "side": self.side,
                "qty_base": self.qty_base, "price": self.price,
                "policy": self.policy, "reduce_only": self.reduce_only,
                "epoch": self.epoch, "state": self.state,
                "client_id": self.client_id,
                "created_ts": self.created_ts}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OrderIntent":
        _reject_unknown(d, tuple(f.name for f in dc_fields(cls)),
                        "OrderIntent")
        side = _require(d, "side", "OrderIntent")
        if side not in ("buy", "sell"):
            raise GridModelError("VALIDATION_ERROR", f"side {side!r}",
                                 "side")
        return cls(
            intent_id=_require(d, "intent_id", "OrderIntent"),
            grid_id=_require(d, "grid_id", "OrderIntent"),
            cause_key=_require(d, "cause_key", "OrderIntent"),
            revision=parse_strict_int(_require(d, "revision", "OrderIntent"),
                                      "revision", minimum=1),
            level_index=parse_strict_int(
                _require(d, "level_index", "OrderIntent"), "level_index",
                minimum=0),
            side=side,
            qty_base=decimal_field(_require(d, "qty_base", "OrderIntent"),
                                   "qty_base"),
            price=decimal_field(_require(d, "price", "OrderIntent"),
                                "price"),
            policy=_require(d, "policy", "OrderIntent"),
            reduce_only=bool(d.get("reduce_only", False)),
            epoch=parse_strict_int(_require(d, "epoch", "OrderIntent"),
                                   "epoch", minimum=0),
            state=str(_opt(d, "state") or "PLANNED"),
            client_id=_opt(d, "client_id"),
            created_ts=_opt(d, "created_ts"))

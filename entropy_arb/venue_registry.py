"""The single registration point for every supported venue.

Adding an exchange = writing ``venue_<name>.py`` (adapter + module-level
``make_venue`` / ``make_public_feed`` / ``list_markets_catalog`` hooks) and
appending ONE ``VenueSpec`` here. Everything else — the CLI venue lists,
config leg construction, credentials validation and the secrets page,
funding support, the discovery catalog and feed factories, the console UI —
derives from this table. See ADD-A-VENUE.zh-CN.md.

Import direction: this module is imported by config/engine/console/discovery
and holds ONLY stdlib + declarative data. config dataclasses and the venue
modules are resolved lazily inside functions, so importing the registry never
pulls a signing SDK and never creates an import cycle.

Keys vs kinds: the registry is keyed by the CLI/venue NAME ("lighter-rh",
"tradexyz"); ``kind`` names the adapter class ("lighter", "hl"). Several keys
may share one kind — that is what made lighter-rh a zero-adapter integration
and it stays the cheap path for re-skins of an existing exchange.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Display order for every derived list. Keep new venues appended so existing
# UI ordering (and the golden snapshot) stays stable.
_ORDER: Tuple[str, ...] = ("hl", "lighter", "lighter-rh", "tradexyz",
                           "katana", "backpack", "bulk", "ondo", "arcus")


@dataclass(frozen=True)
class CredField:
    """One credential input: the field it fills on the venue's creds
    dataclass, the env fallback chain (first SET variable wins — explicit
    per-leg variables beat shared ones), the secrets-page format kind
    (an id into console.secrets' validator table) and whether trading
    requires it. ``{LEG}`` in a variable name expands to BASE/HEDGE for
    venues whose two deployments are separate accounts (Lighter)."""
    field: str
    env: Tuple[str, ...]
    key_kind: str
    required: bool = True


@dataclass(frozen=True)
class VenueSpec:
    key: str                       # CLI/registry key ("lighter-rh", "bulk")
    kind: str                      # adapter kind == VenueConf.kind
    label: str                     # VenueConf.label (base role)
    label_hedge: Optional[str]     # hedge-role label override (lighter-rh: RH)
    display: str                   # exchange group name (console UI)
    base: bool                     # may run the entropy leg
    hedge: bool                    # may run the hedge leg
    maker_capable: bool            # implements the maker contract
    funding_supported: bool        # has fetch_funding (funding attribution)
    in_discovery: bool             # appears in discovery VENUE_KEYS
    leg_fee_bps: float             # load_config taker_fee_bps DEFAULT (yaml wins)
    opm_base: int                  # max_orders_per_min default, entropy leg
    opm_hedge: int                 # max_orders_per_min default, hedge leg
    discovery_fee_bps: Optional[float]   # DEFAULT_TAKER_FEE_BPS entry
    creds_dataclass: str           # class name in entropy_arb.config
    creds_group: str               # secrets-page group id (hl is "entropy")
    creds: Tuple[CredField, ...] = ()
    requirements: Tuple[str, ...] = ()   # secrets VENUE_REQUIREMENTS entry
    fee_variants: Tuple[Tuple[str, float], ...] = ()   # hl: (":io", 0.9)...
    module: str = ""               # venue module path (lazy hooks below)

    def label_for(self, role: str) -> str:
        if role == "hedge" and self.label_hedge:
            return self.label_hedge
        return self.label


HL_CREDS = (
    CredField("private_key", ("HL_PRIVATE_KEY",), "private_key"),
    CredField("account_address", ("HL_ACCOUNT_ADDRESS",), "address",
              required=False),
)
XYZ_CREDS = (
    CredField("private_key", ("HL_PRIVATE_KEY_XYZ", "HL_PRIVATE_KEY"),
              "private_key"),
    CredField("account_address",
              ("HL_ACCOUNT_ADDRESS_XYZ", "HL_ACCOUNT_ADDRESS"), "address",
              required=False),
)
LIGHTER_CREDS = (
    CredField("account_index",
              ("LIGHTER_{LEG}_ACCOUNT_INDEX", "LIGHTER_ACCOUNT_INDEX"), "int"),
    CredField("api_key_index",
              ("LIGHTER_{LEG}_API_KEY_INDEX", "LIGHTER_API_KEY_INDEX"), "int"),
    CredField("api_private_key",
              ("LIGHTER_{LEG}_API_PRIVATE_KEY", "LIGHTER_API_PRIVATE_KEY"),
              "hex_key"),
)

VENUES: Dict[str, VenueSpec] = {s.key: s for s in (
    VenueSpec(
        key="hl", kind="hl", label="ENTROPY", label_hedge=None,
        display="HL(io)", base=True, hedge=False, maker_capable=False,
        funding_supported=False, in_discovery=True,
        leg_fee_bps=0.0, opm_base=120, opm_hedge=120,
        discovery_fee_bps=4.5,
        fee_variants=((":io", 0.9), (":xyz", 1.0)),
        creds_dataclass="HLCreds", creds_group="entropy", creds=HL_CREDS,
        requirements=("HL_PRIVATE_KEY",),
        module="entropy_arb.venue_hl"),
    VenueSpec(
        key="lighter", kind="lighter", label="LIGHTER", label_hedge=None,
        display="Lighter", base=True, hedge=True, maker_capable=False,
        funding_supported=False, in_discovery=True,
        leg_fee_bps=0.0, opm_base=120, opm_hedge=30,
        discovery_fee_bps=0.0,
        creds_dataclass="LighterCreds", creds_group="lighter", creds=LIGHTER_CREDS,
        requirements=("LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                      "LIGHTER_API_PRIVATE_KEY"),
        module="entropy_arb.venue_lighter"),
    VenueSpec(
        key="lighter-rh", kind="lighter", label="LIGHTER-RH",
        label_hedge="RH", display="Lighter-RH", base=True, hedge=True,
        maker_capable=False, funding_supported=False, in_discovery=True,
        leg_fee_bps=0.0, opm_base=120, opm_hedge=30,
        discovery_fee_bps=0.0,
        creds_dataclass="LighterCreds", creds_group="lighter-rh", creds=LIGHTER_CREDS,
        requirements=("LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                      "LIGHTER_API_PRIVATE_KEY"),
        module="entropy_arb.venue_lighter"),
    VenueSpec(
        key="tradexyz", kind="hl", label="XYZ", label_hedge=None,
        display="trade.xyz", base=False, hedge=True, maker_capable=False,
        funding_supported=False, in_discovery=False,
        leg_fee_bps=1.0, opm_base=120, opm_hedge=120,
        discovery_fee_bps=None,
        creds_dataclass="HLCreds", creds_group="tradexyz", creds=XYZ_CREDS,
        # the shared HL keys satisfy a tradexyz leg (XYZ_* are overrides)
        requirements=("HL_PRIVATE_KEY",),
        module="entropy_arb.venue_hl"),
    VenueSpec(
        key="katana", kind="katana", label="KATANA", label_hedge=None,
        display="Katana", base=True, hedge=True, maker_capable=True,
        funding_supported=True, in_discovery=True,
        # live market-level taker fee is ~1.9 bps (the API serves the exact
        # value; the config number stays the explicit source of truth so a
        # fee change can never silently move the thresholds)
        leg_fee_bps=1.9, opm_base=30, opm_hedge=30,
        discovery_fee_bps=None,       # filled from the venue's own /markets
        creds_dataclass="KatanaCreds", creds_group="katana",
        creds=(
            CredField("api_key", ("KATANA_API_KEY",), "uuid"),
            CredField("api_secret", ("KATANA_API_SECRET",), "token"),
            CredField("private_key", ("KATANA_PRIVATE_KEY",), "private_key"),
            CredField("wallet_address", ("KATANA_WALLET",), "address",
                      required=False),
        ),
        requirements=("KATANA_API_KEY", "KATANA_API_SECRET",
                      "KATANA_PRIVATE_KEY"),
        module="entropy_arb.venue_katana"),
    VenueSpec(
        key="backpack", kind="backpack", label="BACKPACK", label_hedge=None,
        display="Backpack", base=True, hedge=True, maker_capable=True,
        funding_supported=False, in_discovery=True,
        # tier-1 perp taker fee is 5.0bp on the EU entity — VERIFY your
        # account's actual tier before trusting this default
        leg_fee_bps=5.0, opm_base=120, opm_hedge=120,
        discovery_fee_bps=2.5,        # tier 2-5 bps — verify against your tier
        creds_dataclass="BackpackCreds", creds_group="backpack",
        creds=(
            CredField("api_key", ("BACKPACK_API_KEY",), "b64_32"),
            CredField("api_secret", ("BACKPACK_API_SECRET",), "b64_32"),
        ),
        requirements=("BACKPACK_API_KEY", "BACKPACK_API_SECRET"),
        module="entropy_arb.venue_backpack"),
    VenueSpec(
        key="bulk", kind="bulk", label="BULK", label_hedge=None,
        display="Bulk", base=True, hedge=True, maker_capable=True,
        funding_supported=True, in_discovery=True,
        # tier-0 taker fee is 3.5bp (GET /feeState serves the live tier
        # ladder; maker is 0bp) — same explicit-source-of-truth rule
        leg_fee_bps=3.5, opm_base=30, opm_hedge=30,
        discovery_fee_bps=3.5,
        creds_dataclass="BulkCreds", creds_group="bulk",
        creds=(CredField("secret_key", ("BULK_SECRET_KEY",), "b58_key"),),
        requirements=("BULK_SECRET_KEY",),
        module="entropy_arb.venue_bulk"),
    VenueSpec(
        key="ondo", kind="ondo", label="ONDO", label_hedge=None,
        display="Ondo", base=True, hedge=True, maker_capable=True,
        funding_supported=True, in_discovery=True,
        # /v1/markets serves maker 0.01% / taker 0.025% live; the config
        # default is the documented base tier — same explicit-source-of-
        # truth rule as the other venues
        leg_fee_bps=2.5, opm_base=30, opm_hedge=30,
        discovery_fee_bps=2.5,
        creds_dataclass="OndoCreds", creds_group="ondo",
        creds=(
            CredField("api_key", ("ONDO_KEY_ID",), "token"),
            CredField("api_secret", ("ONDO_API_SECRET",), "token"),
        ),
        requirements=("ONDO_KEY_ID", "ONDO_API_SECRET"),
        module="entropy_arb.venue_ondo"),
    VenueSpec(
        key="arcus", kind="arcus", label="ARCUS", label_hedge=None,
        display="Arcus", base=True, hedge=True, maker_capable=True,
        funding_supported=True, in_discovery=True,
        # feeTiers serves maker 0 / taker 225 ppm (base tier; VIP tiers
        # rebate maker) — same explicit-source-of-truth rule
        leg_fee_bps=2.25, opm_base=30, opm_hedge=30,
        discovery_fee_bps=2.25,
        creds_dataclass="ArcusCreds", creds_group="arcus",
        creds=(
            CredField("address", ("ARCUS_ADDRESS",), "address"),
            CredField("api_key", ("ARCUS_API_KEY",), "hex_key"),
            CredField("secret_key", ("ARCUS_SECRET_KEY",), "hex_key"),
        ),
        requirements=("ARCUS_ADDRESS", "ARCUS_API_KEY", "ARCUS_SECRET_KEY"),
        module="entropy_arb.venue_arcus"),
)}


# ------------------------------------------------------------- lookups

def spec(venue: str) -> VenueSpec:
    """Spec for a CLI venue key; ``hl:<dex>`` resolves to the hl spec."""
    v = VENUES.get(venue)
    if v is None and venue.startswith("hl:"):
        v = VENUES.get("hl")
    if v is None:
        raise KeyError(f"unknown venue {venue!r} (registered: {_ORDER})")
    return v


def spec_by_kind(kind: str) -> VenueSpec:
    for s in VENUES.values():
        if s.kind == kind:
            return s
    raise KeyError(f"no venue registered for kind {kind!r}")


def hedge_venues() -> Tuple[str, ...]:
    return tuple(k for k in _ORDER if VENUES[k].hedge)


def base_venues() -> Tuple[str, ...]:
    return tuple(k for k in _ORDER if VENUES[k].base)


def maker_venues() -> Tuple[str, ...]:
    return tuple(k for k in _ORDER if VENUES[k].maker_capable)


def discovery_keys() -> Tuple[str, ...]:
    return tuple(k for k in _ORDER if VENUES[k].in_discovery)


def default_taker_fees() -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for k in _ORDER:
        s = VENUES[k]
        if not s.in_discovery:
            continue
        out[k] = s.discovery_fee_bps
        for suffix, fee in s.fee_variants:
            out[k + suffix] = fee
    return out


def _venue_module(venue: str):
    return importlib.import_module(spec(venue).module)


# ------------------------------------------------------- credentials layer

def creds_from_env(vspec: VenueSpec, role: str) -> Any:
    """Build the venue's creds dataclass from the process environment.

    ``role`` is "base"|"hedge"; it selects the {LEG} variable set. The
    first SET variable of each chain wins (empty/whitespace counts as
    unset — .env is the single source of truth, see config._env_first_s).
    """
    from . import config as _cfg
    leg = "HEDGE" if role == "hedge" else "BASE"
    vals: Dict[str, Any] = {}
    for f in vspec.creds:
        val: Optional[str] = None
        for name in f.env:
            v = _cfg._env_s(name.replace("{LEG}", leg))
            if v is not None:
                val = v
                break
        if val is not None and f.key_kind == "int":
            val = int(val)  # type: ignore[assignment]
        vals[f.field] = val
    return getattr(_cfg, vspec.creds_dataclass)(**vals)


def creds_complete(vc) -> bool:
    """True when every REQUIRED cred field of the leg's kind is set."""
    if getattr(vc, "creds", None) is None:
        return False
    for f in spec_by_kind(vc.kind).creds:
        if f.required and getattr(vc.creds, f.field, None) in (None, ""):
            return False
    return True


# ---------------------------------------------------------- config legs

def build_leg_conf(venue: str, role: str, symbol: str, raw: dict,
                   dex: str = ""):
    """The VenueConf the engine would build for one leg.

    ``role`` is "base"|"hedge" (yaml section entropy/hedge). ``dex`` is the
    raw entropy.dex value and only matters for kind hl: the base leg runs
    the configured dex, a hedge leg defaults to "xyz" (trade.xyz). Fee /
    order-rate defaults come from the spec; the yaml file always wins.
    """
    from .config import LIGHTER_PROFILES, VenueConf, _get
    vs = spec(venue)
    section = "hedge" if role == "hedge" else "entropy"
    kind_dex = ""
    profile = None
    if vs.kind == "hl":
        kind_dex = dex if role == "base" else (dex or "xyz")
    elif vs.kind == "lighter":
        profile = LIGHTER_PROFILES[venue]
    return VenueConf(
        key=section, kind=vs.kind, label=vs.label_for(role), symbol=symbol,
        fee_bps=float(_get(raw, section, "taker_fee_bps", vs.leg_fee_bps)),
        cap_usd=float(_get(raw, section, "max_position_usd", 1000.0)),
        orders_per_min=int(_get(raw, section, "max_orders_per_min",
                                vs.opm_hedge if role == "hedge"
                                else vs.opm_base)),
        hl_dex=kind_dex,
        lighter_profile=profile,
        creds=creds_from_env(vs, role),
    )


# ------------------------------------------------------------ factories

def make_venue_client(vc, session, settle_timeout):
    """The ONE venue factory (engine and console ops share it)."""
    mod = importlib.import_module(spec_by_kind(vc.kind).module)
    return mod.make_venue(vc, session, settle_timeout)


def make_public_feed(listing, book, notify, session=None):
    """Public book feed for a discovery MarketListing."""
    mod = _venue_module(listing.venue)
    return mod.make_public_feed(listing, book, notify, session=session)


def catalog_impl(venue: str):
    """Async ``(session, venue, dex)`` -> List[MarketListing] for one venue
    key (public REST only, no credentials)."""
    return _venue_module(venue).list_markets_catalog


# --------------------------------------------------------------- diag

def diag_conf(venue: str, symbol: str, role: str, dex: str):
    """The VenueConf diagnostics would use for this leg, straight from the
    environment (defaults for fee/caps — diagnostics cares about
    reachability, not economics).

    Legacy contract (webui §9): tradeXYZ is diagnosed as venue=hl
    role=hedge dex=xyz — never venue "tradexyz", which raises here.
    """
    from .config import LIGHTER_PROFILES, VenueConf
    if venue == "tradexyz":
        # legacy contract: the UI maps tradeXYZ to venue=hl role=hedge
        # dex=xyz; a direct tradexyz conf risks a wrong dex, so it raises
        raise ValueError(f"unknown venue {venue!r}")
    is_xyz = venue == "hl" and role == "hedge"
    vs = spec("tradexyz") if is_xyz else spec(venue)
    return VenueConf(
        key="hedge" if role == "hedge" else "entropy",
        kind=vs.kind,
        label=vs.label_for("hedge" if is_xyz else role),
        symbol=(symbol or "").strip().upper(),
        fee_bps=0.0, cap_usd=1000.0, orders_per_min=120,
        hl_dex=(dex or "xyz") if is_xyz else (dex if vs.kind == "hl" else ""),
        lighter_profile=(LIGHTER_PROFILES.get(venue)
                         if vs.kind == "lighter" else None),
        creds=creds_from_env(vs, role),
    )


# ------------------------------------------------- secrets-page derivation

def key_kinds() -> Dict[str, str]:
    """env var -> format-kind id for every credential variable of every
    venue (chain members included: the shared fallback and the {LEG}
    overrides validate the same way)."""
    out: Dict[str, str] = {}
    for k in _ORDER:
        for f in VENUES[k].creds:
            for env in f.env:
                for leg in ("BASE", "HEDGE"):
                    out[env.replace("{LEG}", leg)] = f.key_kind
    return out


def venue_requirements() -> Dict[str, Tuple[str, ...]]:
    """creds-group id -> minimal env keys that make the group tradeable."""
    return {VENUES[k].creds_group: VENUES[k].requirements for k in _ORDER}


# ------------------------------------------------------------- console UI

# Credential-card data for the console's API Keys page (webui/v2 renders
# /api/venue-catalog; the legacy v1 page still carries its own copy).
# `title_key` is the existing i18n key — a NEW venue passes a literal
# {"en","zh"} title instead and needs no strings.js edit. Cards render in
# list order. `affects`/`rel`/`need_group` replace the JS-side per-venue
# chains that used to be hand-synced with this table.
UI_CARDS: Dict[str, list] = {
    "hl": [{
        "id": "entropy", "order": 10, "title_key": "secrets.title.entropy",
        "keys": ["HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"],
        "diag": {"venue": "hl", "role": "base", "dexInput": True},
        "affects": {"base": ["hl"], "hedge": ["tradexyz"]},
        "rel": ["entropy"],
    }],
    "tradexyz": [{
        "id": "xyz", "order": 20, "title_key": "secrets.title.xyz",
        "keys": ["HL_PRIVATE_KEY_XYZ", "HL_ACCOUNT_ADDRESS_XYZ"],
        "source": "tradexyz",
        "diag": {"venue": "hl", "role": "hedge", "dex": "xyz"},
        "affects": {"hedge": ["tradexyz"]},
        "rel": ["tradexyz"],
    }],
    "lighter": [
        {"id": "lighter", "order": 30, "title_key": "v2.conn.title.lighter",
         "keys": ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                  "LIGHTER_API_PRIVATE_KEY"],
         "roles": True,
         "diag": {"venue": "lighter"},
         "affects": {"base": ["lighter", "lighter-rh"],
                     "hedge": ["lighter", "lighter-rh"]},
         "rel": ["lighter", "lighter-rh"],
         "deploymentNote": True},
        {"id": "lighter-base", "order": 50, "title_key": "v2.conn.title.base",
         "keys": ["LIGHTER_BASE_ACCOUNT_INDEX",
                  "LIGHTER_BASE_API_KEY_INDEX",
                  "LIGHTER_BASE_API_PRIVATE_KEY"],
         "completeness": "lighter-base",
         "diag": {"venue": "lighter", "role": "base"},
         "affects": {"base": ["lighter", "lighter-rh"]},
         "rel": ["lighter-base"]},
        {"id": "lighter-hedge", "order": 60, "title_key": "v2.conn.title.hedge",
         "keys": ["LIGHTER_HEDGE_ACCOUNT_INDEX",
                  "LIGHTER_HEDGE_API_KEY_INDEX",
                  "LIGHTER_HEDGE_API_PRIVATE_KEY"],
         "completeness": "lighter-hedge",
         "diag": {"venue": "lighter", "role": "hedge"},
         "affects": {"hedge": ["lighter", "lighter-rh"]},
         "rel": ["lighter-hedge"]},
    ],
    "lighter-rh": [{
        "id": "lighter-rh", "order": 40, "title_key": "v2.conn.title.rh",
        "keys": [],
        "sharedView": True,
        "diag": {"venue": "lighter-rh"},
        "affects": {"base": ["lighter", "lighter-rh"],
                    "hedge": ["lighter", "lighter-rh"]},
        "rel": ["lighter", "lighter-rh"],
    }],
    "katana": [{
        "id": "katana", "order": 70, "title_key": "secrets.title.katana",
        "keys": ["KATANA_API_KEY", "KATANA_API_SECRET",
                 "KATANA_PRIVATE_KEY", "KATANA_WALLET"],
        "diag": {"venue": "katana"},
        "affects": {"base": ["katana"], "hedge": ["katana"]},
        "rel": ["katana"],
    }],
    "backpack": [{
        "id": "backpack", "order": 80, "title_key": "secrets.title.backpack",
        "keys": ["BACKPACK_API_KEY", "BACKPACK_API_SECRET"],
        "diag": {"venue": "backpack"},
        "affects": {"base": ["backpack"], "hedge": ["backpack"]},
        "rel": ["backpack"],
    }],
    "ondo": [{
        "id": "ondo", "order": 100, "title_key": "secrets.title.ondo",
        "keys": ["ONDO_KEY_ID", "ONDO_API_SECRET"],
        "diag": {"venue": "ondo"},
        "affects": {"base": ["ondo"], "hedge": ["ondo"]},
        "rel": ["ondo"],
    }],
    "arcus": [{
        "id": "arcus", "order": 110, "title_key": "secrets.title.arcus",
        "keys": ["ARCUS_ADDRESS", "ARCUS_API_KEY", "ARCUS_SECRET_KEY"],
        "diag": {"venue": "arcus"},
        "affects": {"base": ["arcus"], "hedge": ["arcus"]},
        "rel": ["arcus"],
    }],
    "bulk": [{
        "id": "bulk", "order": 90, "title_key": "secrets.title.bulk",
        "keys": ["BULK_SECRET_KEY"],
        "diag": {"venue": "bulk"},
        "affects": {"base": ["bulk"], "hedge": ["bulk"]},
        "rel": ["bulk"],
    }],
}

# which secrets-group shows completeness for a leg on this venue (runs.js
# pre-flight lines): lighter's per-leg override groups, hl's "entropy"
NEED_GROUP: Dict[str, dict] = {
    "hl": {"base": "entropy"},
    "lighter": {"base": "lighter-base", "hedge": "lighter-hedge"},
    "lighter-rh": {"base": "lighter-base", "hedge": "lighter-hedge"},
}


def ui_catalog() -> dict:
    """The /api/venue-catalog payload: everything the console UI needs to
    render venue dropdowns and credential cards, derived from this table."""
    venues = [{
        "key": VENUES[k].key,
        "kind": VENUES[k].kind,
        "display": VENUES[k].display,
        "base": VENUES[k].base,
        "hedge": VENUES[k].hedge,
        "maker": VENUES[k].maker_capable,
        "funding": VENUES[k].funding_supported,
        "need": dict(NEED_GROUP.get(k, {})),
    } for k in _ORDER]
    cards = []
    for k in _ORDER:
        for c in UI_CARDS.get(k, []):
            card = dict(c)
            card["affects"] = {role: list(v)
                               for role, v in c["affects"].items()}
            card["rel"] = list(c["rel"])
            card["keys"] = list(c["keys"])
            cards.append(card)
    cards.sort(key=lambda c: c["order"])
    return {"venues": venues, "cards": cards}

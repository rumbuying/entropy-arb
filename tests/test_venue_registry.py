"""Venue-registry consistency invariants — the anti-drift net.

Every bug this suite catches is one that previously only surfaced as a
follow-up patch after a venue shipped (bulk's missing UI entries were
exactly this). The registry is the single registration point; these tests
pin everything that CLAIMS to derive from it to actually deriving from it:

* venue modules expose the three hooks the registry dispatches to;
* secrets requirements / key kinds and the .env.example documentation stay
  in lockstep with the credential specs;
* the console UI catalog covers every venue and references only real
  groups / env keys / diag venues;
* the discovery watchlist only names registered venues;
* the v2 frontend contains NO hand-copied venue lists.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from entropy_arb import venue_registry
from entropy_arb.config import BASE_VENUES, HEDGE_VENUES, MAKER_VENUES
from entropy_arb.discovery import DEFAULT_TAKER_FEE_BPS, VENUE_KEYS

REPO = Path(__file__).resolve().parent.parent

HOOKS = ("make_venue", "make_public_feed", "list_markets_catalog")


def test_config_tuples_derive_from_registry():
    assert HEDGE_VENUES == venue_registry.hedge_venues()
    assert BASE_VENUES == venue_registry.base_venues()
    assert MAKER_VENUES == venue_registry.maker_venues()
    assert VENUE_KEYS == venue_registry.discovery_keys()
    assert DEFAULT_TAKER_FEE_BPS == venue_registry.default_taker_fees()


def test_venue_modules_expose_registry_hooks():
    for key in venue_registry.VENUES:
        import importlib
        mod = importlib.import_module(venue_registry.VENUES[key].module)
        for hook in HOOKS:
            assert callable(getattr(mod, hook, None)), \
                f"{key}: module {mod.__name__} lacks {hook}()"


def test_secrets_requirements_cover_hedge_and_base():
    from entropy_arb.console.secrets import VENUE_REQUIREMENTS
    for key in HEDGE_VENUES:
        group = venue_registry.spec(key).creds_group
        assert group in VENUE_REQUIREMENTS, \
            f"hedge venue {key}: secrets group {group} missing"
        assert VENUE_REQUIREMENTS[group], \
            f"hedge venue {key}: empty requirements"
    for key in BASE_VENUES:
        assert venue_registry.spec(key).creds_group in VENUE_REQUIREMENTS


def test_secrets_key_kinds_match_env_example():
    """Every credential env var the registry knows must be documented in
    .env.example (a key the ops page can save but the file docs never
    mention is how deployments end up 'incomplete' on the box)."""
    from entropy_arb.console.secrets import KEY_KINDS
    example = (REPO / ".env.example").read_text()
    missing = [k for k in KEY_KINDS
               if k not in example and not k.startswith("LIGHTER_")]
    # LIGHTER_* share the documented shared-triple section; per-leg
    # overrides are documented once — check the stem instead
    assert not missing, f".env.example missing credential keys: {missing}"
    assert "LIGHTER_BASE_ACCOUNT_INDEX" in example
    assert "LIGHTER_HEDGE_ACCOUNT_INDEX" in example


def test_ui_catalog_internal_consistency():
    from entropy_arb.console.secrets import KEY_KINDS, VENUE_REQUIREMENTS
    cat = venue_registry.ui_catalog()
    keys = [v["key"] for v in cat["venues"]]
    assert keys == [k for k in venue_registry.VENUES]
    by_key = {v["key"]: v for v in cat["venues"]}
    known_groups = set(VENUE_REQUIREMENTS) | {
        "lighter-base", "lighter-hedge"}          # per-leg override views
    card_ids = set()
    for card in cat["cards"]:
        card_ids.add(card["id"])
        for env in card["keys"]:
            assert env in KEY_KINDS, \
                f"card {card['id']}: env key {env} unknown to secrets"
        for role in ("base", "hedge"):
            for v in card["affects"].get(role, []):
                assert v in by_key, \
                    f"card {card['id']}: affects unknown venue {v}"
        for g in card["rel"]:
            assert g in known_groups, \
                f"card {card['id']}: rel group {g} has no completeness view"
        diag = card["diag"]
        assert diag["venue"] in venue_registry.VENUES or \
            (diag["venue"] == "hl" and diag.get("role") == "hedge"), \
            f"card {card['id']}: diag venue {diag['venue']} not diagnosable"
    # every venue with credentials has at least one card
    for key, vs in venue_registry.VENUES.items():
        if vs.creds:
            ids = {c["id"] for c in cat["cards"]
                   if c["id"] == key or key in c["affects"].get("base", [])
                   or key in c["affects"].get("hedge", [])}
            assert ids or key in ("tradexyz",), \
                f"venue {key}: no UI card references it"
    # legacy diagnostics contract: tradexyz is diagnosed as hl/hedge
    xyz = [c for c in cat["cards"] if c["id"] == "xyz"][0]
    assert xyz["diag"] == {"venue": "hl", "role": "hedge", "dex": "xyz"}


def test_watchlist_venues_are_registered():
    import yaml
    wl = yaml.safe_load((REPO / "discovery-watchlist.yaml").read_text()) or {}
    unknown = [v for v in (wl.get("venues") or [])
               if v not in venue_registry.VENUES
               and not str(v).startswith("hl:")]
    assert not unknown, f"watchlist names unregistered venues: {unknown}"


@pytest.mark.parametrize("js", [
    "entropy_arb/webui/v2/connections.js",
    "entropy_arb/webui/v2/profiles.js",
    "entropy_arb/webui/v2/runs.js",
])
def test_v2_frontend_has_no_hand_copied_venue_lists(js):
    """The v2 pages must render venue lists from /api/venue-catalog. This
    is the test that would have caught the Runs dialog shipping without
    'bulk' (it did, for two days)."""
    src = (REPO / js).read_text()
    hardcoded = re.findall(r'\[(?:\s*"(?:hl|lighter|lighter-rh|tradexyz|'
                           r'katana|backpack|bulk)"\s*,\s*){2,}"(?:hl|lighter|'
                           r'lighter-rh|tradexyz|katana|backpack|bulk)"\s*\]',
                           src)
    assert not hardcoded, \
        f"{js}: hand-copied venue list {hardcoded} — use venueCatalog()"


def test_ops_diag_contract_unchanged_for_legacy_mapping():
    """venue=tradexyz raises (UI maps it to hl/hedge); hl+hedge maps to
    trade.xyz. Breaking this silently breaks the console 🩺 page."""
    from entropy_arb.console.ops import _diag_conf
    with pytest.raises(ValueError):
        _diag_conf("tradexyz", "BTC", "hedge", "", ".env")
    conf = _diag_conf("hl", "BTC", "hedge", "", ".env")
    assert conf.hl_dex == "xyz" and conf.label == "XYZ"
    assert _diag_conf("hl", "BTC", "base", "io", ".env").label == "ENTROPY"

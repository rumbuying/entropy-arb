"""Golden equivalence snapshot for the venue wiring layer (pre-refactor
baseline).

The venue-registry refactor replaces the per-venue if/elif chains in
config.load_config, engine/ops venue factories, secrets requirements and
discovery keys with spec-driven lookups. This test freezes the CURRENT
observable output of all of those paths and fails on any drift:

* load_config() for every valid (base, hedge) combination — both VenueConf
  trees incl. creds resolution from a fixed env, plus creds_complete;
* the HEDGE/BASE/MAKER venue tuples;
* discovery VENUE_KEYS + DEFAULT_TAKER_FEE_BPS;
* secrets VENUE_REQUIREMENTS + KEY_KINDS;
* ops._diag_conf for every CLI venue (label/kind/creds shape);
* console.venues.exchange_of normalization.

Regenerate with:  UPDATE_GOLDEN=1 python -m pytest tests/test_golden_legconf.py
Any other difference is a regression — the live system trades on these
values.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden" / "load_config.json"

FIXTURE_YAML = """\
thresholds:
  midline_bps: 4.0
  upper_bps: 3.0
  lower_bps: 3.0
entropy:
  dex: ""
  symbol: "BTCC"
hedge:
  symbol: "BTCX"
maker:
  enabled: false
"""

# Fixed fake credentials for EVERY env key the config layer reads. The per-leg
# LIGHTER_* overrides are deliberately ABSENT so the fallback chain
# (LIGHTER_<LEG>_* -> LIGHTER_*) is what gets captured.
FAKE_ENV = {
    "HL_PRIVATE_KEY": "0x" + "11" * 32,
    "HL_ACCOUNT_ADDRESS": "0x" + "22" * 20,
    "HL_PRIVATE_KEY_XYZ": "0x" + "33" * 32,
    "HL_ACCOUNT_ADDRESS_XYZ": "0x" + "44" * 20,
    "LIGHTER_ACCOUNT_INDEX": "4242",
    "LIGHTER_API_KEY_INDEX": "3",
    "LIGHTER_API_PRIVATE_KEY": "0x" + "55" * 40,
    "KATANA_API_KEY": "0f0e0d0c-1111-2222-3333-444455556666",
    "KATANA_API_SECRET": "katana-secret-token",
    "KATANA_PRIVATE_KEY": "0x" + "66" * 32,
    "KATANA_WALLET": "0x" + "77" * 20,
    "BACKPACK_API_KEY": "b" * 43 + "=",
    "BACKPACK_API_SECRET": "s" * 43 + "=",
    "BULK_SECRET_KEY": "5" * 87,
}


def _serialize(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return obj


@pytest.fixture()
def wired_env(monkeypatch, tmp_path):
    cfg = tmp_path / "golden.yaml"
    cfg.write_text(FIXTURE_YAML)
    env = tmp_path / ".env"
    env.write_text("# empty on purpose: env comes from monkeypatch\n")
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    # ensure per-leg overrides and stray keys are absent
    for k in list(os.environ):
        if k.startswith(("LIGHTER_BASE_", "LIGHTER_HEDGE_")):
            monkeypatch.delenv(k, raising=False)
    return cfg, env


def _matrix_snapshot(wired_env):
    cfg_path, env_path = wired_env
    from entropy_arb.config import (BASE_VENUES, HEDGE_VENUES,
                                    ConfigError, load_config)

    out: dict = {}
    for base in BASE_VENUES:
        for hedge in HEDGE_VENUES:
            combo = f"base={base}|hedge={hedge}"
            if base == hedge:
                with pytest.raises(ConfigError) as ei:
                    load_config(str(cfg_path), str(env_path), symbol="BTC",
                                hedge_venue=hedge, base_venue=base)
                out[combo] = {"error": str(ei.value)}
                continue
            cfg = load_config(str(cfg_path), str(env_path), symbol="BTC",
                              hedge_venue=hedge, base_venue=base)
            out[combo] = {
                "entropy": _serialize(cfg.entropy),
                "hedge": _serialize(cfg.hedge),
                "creds_complete": cfg.creds_complete,
            }
    return out


def _constants_snapshot():
    from entropy_arb import config, discovery
    from entropy_arb.console import secrets as sec
    from entropy_arb.console import venues as vmod

    return {
        "tuples": {
            "HEDGE_VENUES": list(config.HEDGE_VENUES),
            "BASE_VENUES": list(config.BASE_VENUES),
            "MAKER_VENUES": list(config.MAKER_VENUES),
        },
        "discovery": {
            "VENUE_KEYS": list(discovery.VENUE_KEYS),
            "DEFAULT_TAKER_FEE_BPS": _serialize(discovery.DEFAULT_TAKER_FEE_BPS),
        },
        "secrets": {
            "VENUE_REQUIREMENTS": _serialize(sec.VENUE_REQUIREMENTS),
            "KEY_KINDS": _serialize(sec.KEY_KINDS),
        },
    }


def _diag_snapshot(wired_env):
    from entropy_arb.console.ops import _diag_conf
    from entropy_arb.console.venues import exchange_of

    out: dict = {}
    for venue in ("hl", "tradexyz", "lighter", "lighter-rh", "katana",
                  "backpack", "bulk"):
        for role in ("base", "hedge"):
            try:
                conf = _diag_conf(venue, "BTC", role, "", ".env")
                body = _serialize(conf)
            except Exception as ei:  # noqa: BLE001 - shape is the contract
                body = {"error": f"{type(ei).__name__}: {ei}"}
            out[f"diag:{venue}|{role}"] = body
        out[f"exchange_of:{venue}"] = exchange_of(venue)
    return out


def _snapshot(wired_env) -> dict:
    return {
        "matrix": _matrix_snapshot(wired_env),
        "constants": _constants_snapshot(),
        "diag": _diag_snapshot(wired_env),
    }


def test_golden_wiring_snapshot(wired_env):
    snap = _snapshot(wired_env)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    assert GOLDEN.exists(), (
        "golden snapshot missing — run UPDATE_GOLDEN=1 pytest "
        "tests/test_golden_legconf.py on the PRE-refactor code first")
    golden = json.loads(GOLDEN.read_text())
    assert snap == golden, (
        "venue wiring drift vs golden baseline — if this change is "
        "intentional, regenerate with UPDATE_GOLDEN=1 and review the diff")

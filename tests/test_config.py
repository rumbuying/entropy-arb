"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh", base="hl"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge, base_venue=base)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def test_katana_hedge():
    cfg = load(MINIMAL + "\nhedge:\n  symbol: BTC-USD\n"
               "  taker_fee_bps: 1.9\n", symbol="BTC", hedge="katana")
    assert cfg.hedge.kind == "katana"
    assert cfg.hedge.label == "KATANA"
    assert cfg.hedge.symbol == "BTC-USD"
    assert cfg.hedge.fee_bps == 1.9
    assert cfg.hedge.katana_creds is not None
    assert not cfg.hedge.katana_creds.complete      # no keys in NO_ENV
    assert cfg.hedge_venue == "katana"


def test_katana_default_fee():
    cfg = load(MINIMAL, symbol="ETH", hedge="katana")
    assert cfg.hedge.fee_bps == 1.9                 # live market-level rate
    assert cfg.hedge.symbol == "ETH"                # alias still optional


def test_main_dex_entropy_leg():
    # entropy.dex "" selects the main Hyperliquid perp dex (BTC, ETH, ...)
    cfg = load(MINIMAL + "\nentropy:\n  dex: \"\"\n"
               "hedge:\n  symbol: BTC-USD\n", symbol="BTC", hedge="katana")
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == ""
    assert cfg.entropy.symbol == "BTC"
    assert cfg.hedge.symbol == "BTC-USD"


def test_hedge_symbol_alias():
    # the two venues may name the same market differently (ANTH vs ANTHROPIC)
    cfg = load(MINIMAL + "\nhedge:\n  symbol: ANTHROPIC\n",
               symbol="ANTH", hedge="lighter-rh")
    assert cfg.symbol == "ANTH"              # canonical, entropy leg
    assert cfg.entropy.symbol == "ANTH"
    assert cfg.hedge.symbol == "ANTHROPIC"   # alias on the hedge leg only


def test_hedge_symbol_defaults_to_cli_symbol():
    cfg = load(MINIMAL, symbol="SNDK", hedge="lighter-rh")
    assert cfg.entropy.symbol == "SNDK"
    assert cfg.hedge.symbol == "SNDK"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


# ----------------------------------------------------- base-leg venue (--base)

def test_lighter_base_katana_hedge():
    # the Lighter↔Katana line: premium = lighter/katana − 1, same convention
    # as tools/basis_probe.py — recorded bands transfer directly
    cfg = load(MINIMAL + "\nhedge:\n  symbol: BTC-USD\n",
               symbol="BTC", hedge="katana", base="lighter")
    assert cfg.entropy.kind == "lighter"
    assert cfg.entropy.label == "LIGHTER"
    assert cfg.entropy.lighter_profile.chain_id == 304   # lighter mainnet
    assert cfg.entropy.fee_bps == 0.0                    # lighter taker = 0
    assert cfg.entropy.symbol == "BTC"
    assert cfg.hedge.kind == "katana" and cfg.hedge.symbol == "BTC-USD"


def test_lighter_base_profile_selection():
    rh = load(MINIMAL, hedge="katana", base="lighter-rh")
    assert rh.entropy.kind == "lighter"
    assert rh.entropy.label == "LIGHTER-RH"
    assert rh.entropy.lighter_profile.chain_id == 466324


def test_katana_base_leg():
    cfg = load(MINIMAL, symbol="BTC", hedge="lighter", base="katana")
    assert cfg.entropy.kind == "katana" and cfg.hedge.kind == "lighter"
    assert cfg.entropy.fee_bps == 1.9


def test_base_symbol_alias():
    # entropy.symbol overrides the CLI symbol on the base leg only
    cfg = load(MINIMAL + "\nentropy:\n  symbol: BTC\n",
               symbol="XX", hedge="lighter", base="katana")
    assert cfg.symbol == "XX"
    assert cfg.entropy.symbol == "BTC"
    assert cfg.hedge.symbol == "XX"


def test_base_must_differ_from_hedge():
    expect_error(MINIMAL, "same deployment",
                 hedge="katana", base="katana")
    expect_error(MINIMAL, "same deployment",
                 hedge="lighter", base="lighter")


def test_base_unknown_venue_rejected():
    expect_error(MINIMAL, "--base", hedge="katana", base="binance")


def test_lighter_base_needs_lighter_creds():
    # NO_ENV has no LIGHTER_* keys → creds_complete must be False for live
    cfg = load(MINIMAL, symbol="BTC", hedge="katana", base="lighter")
    assert cfg.entropy.lighter_creds is not None
    assert not cfg.entropy.lighter_creds.complete
    assert not cfg.creds_complete


def test_lighter_creds_per_leg(tmp_path):
    """LIGHTER_BASE_*/LIGHTER_HEDGE_* override the shared LIGHTER_* triple so
    a Lighter-mainnet base leg can coexist with a lighter-rh hedge leg."""
    env = tmp_path / ".env"
    env.write_text(
        "LIGHTER_ACCOUNT_INDEX=11111\n"
        "LIGHTER_API_KEY_INDEX=7\n"
        "LIGHTER_API_PRIVATE_KEY=0x" + "a" * 80 + "\n"
        "LIGHTER_BASE_ACCOUNT_INDEX=12345\n"
        "LIGHTER_BASE_API_KEY_INDEX=3\n"
        "LIGHTER_BASE_API_PRIVATE_KEY=0x" + "b" * 80 + "\n")
    saved = os.environ.copy()
    try:
        # base leg picks the leg-specific triple
        cfg = load_config(write_tmp(MINIMAL), str(env), symbol="ETH",
                          hedge_venue="katana", base_venue="lighter")
        assert cfg.entropy.lighter_creds.account_index == 12345
        assert cfg.entropy.lighter_creds.api_key_index == 3
        assert cfg.entropy.lighter_creds.api_private_key == "0x" + "b" * 80
        assert cfg.entropy.lighter_creds.complete
        # the rh worker keeps reading the shared triple (no cross-talk)
        cfg2 = load_config(write_tmp(MINIMAL), str(env), symbol="ANTH",
                           hedge_venue="lighter-rh", base_venue="hl")
        assert cfg2.hedge.lighter_creds.account_index == 11111
        assert cfg2.hedge.lighter_creds.api_private_key == "0x" + "a" * 80
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_lighter_creds_fall_back_to_shared(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "LIGHTER_ACCOUNT_INDEX=11111\n"
        "LIGHTER_API_KEY_INDEX=7\n"
        "LIGHTER_API_PRIVATE_KEY=0x" + "a" * 80 + "\n")
    saved = os.environ.copy()
    try:
        cfg = load_config(write_tmp(MINIMAL), str(env), symbol="ETH",
                          hedge_venue="katana", base_venue="lighter")
        assert cfg.entropy.lighter_creds.account_index == 11111
        assert cfg.entropy.lighter_creds.complete
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

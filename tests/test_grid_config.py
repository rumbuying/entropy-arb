"""Grid config contract tests (handoff §4): DecimalText, strict ints,
unknown-field rejection, enum/cross validation, config_hash stability."""
import json

import pytest

from entropy_arb.grid import models


def base_config(**over):
    d = {
        "schema_version": 1,
        "strategy_type": "futures_grid",
        "account_ref": "00000000-0000-4000-8000-000000000001",
        "credential_ref": "fixture-cred",
        "market_ref": "|test|BTC-USD|linear_perp|USDC",
        "direction": "neutral",
        "spacing": "arithmetic",
        "lower_price": "100",
        "upper_price": "110",
        "grid_count": 20,
        "capital": {"total": "1000", "reserve": "200", "asset": "USDC"},
        "leverage": 3,
        "margin_mode": "cross",
        "initial_position": "deferred",
        "start": "immediate",
    }
    d.update(over)
    return d


class TestDecimalText:
    def test_canonical_strips_trailing_zeros(self):
        assert models.canonical_decimal_text(models.parse_decimal_text(
            "100.00", "v")) == "100"

    def test_negative_zero_becomes_zero(self):
        assert models.canonical_decimal_text(
            models.parse_decimal_text("-0", "v")) == "0"
        assert models.canonical_decimal_text(
            models.parse_decimal_text("-0.0", "v")) == "0"

    def test_rejects_json_number(self):
        with pytest.raises(models.GridModelError) as e:
            models.parse_decimal_text(100.0, "v")
        assert e.value.reason == "DECIMAL_STRING_REQUIRED"

    def test_rejects_bool(self):
        with pytest.raises(models.GridModelError):
            models.parse_decimal_text(True, "v")

    def test_rejects_exponent_and_whitespace(self):
        for bad in ("1e3", " 100", "100 ", "+100", "007", "1_000", ""):
            with pytest.raises(models.GridModelError):
                models.parse_decimal_text(bad, "v")

    def test_rejects_nan_inf(self):
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(models.GridModelError):
                models.parse_decimal_text(bad, "v")

    def test_negative_forbidden_flag(self):
        with pytest.raises(models.GridModelError) as e:
            models.parse_decimal_text("-1", "v", allow_negative=False)
        assert e.value.reason == "DECIMAL_NEGATIVE_FORBIDDEN"

    def test_no_float_in_canonicalization(self):
        # large precision survives: float would corrupt at 2^53
        raw = "12345678901234567890.123456789"
        d = models.parse_decimal_text(raw, "v")
        assert models.canonical_decimal_text(d) == raw


class TestStrictInt:
    def test_rejects_bool(self):
        with pytest.raises(models.GridModelError) as e:
            models.parse_strict_int(True, "n")
        assert e.value.reason == "INTEGER_REQUIRED"

    def test_rejects_float_even_integral(self):
        with pytest.raises(models.GridModelError):
            models.parse_strict_int(20.0, "n")

    def test_range(self):
        with pytest.raises(models.GridModelError):
            models.parse_strict_int(0, "n", minimum=1)
        assert models.parse_strict_int(5, "n", minimum=1, maximum=10) == 5


class TestTimestamps:
    def test_naive_rejected(self):
        with pytest.raises(models.GridModelError):
            models.parse_iso_ts("2026-10-11T00:00:00", "ts")

    def test_zulu_and_offset(self):
        assert models.parse_iso_ts("2026-10-11T00:00:00Z", "ts") is not None
        assert models.parse_iso_ts("2026-10-11T00:00:00+08:00", "ts") \
            is not None


class TestGridConfig:
    def test_from_dict_ok(self):
        cfg = models.GridConfig.from_dict(base_config())
        assert cfg.grid_count == 20
        assert cfg.capital.asset == "USDC"

    def test_unknown_field_rejected(self):
        with pytest.raises(models.GridModelError) as e:
            models.GridConfig.from_dict({**base_config(), "mystery": 1})
        assert e.value.reason == "UNKNOWN_FIELD"

    def test_p1_protection_feature_unavailable(self):
        with pytest.raises(models.GridModelError) as e:
            models.GridConfig.from_dict(
                {**base_config(),
                 "protection": {"stop_loss": {"type": "price"}}})
        assert e.value.reason == "FEATURE_UNAVAILABLE"

    def test_p1_trailing_feature_unavailable(self):
        with pytest.raises(models.GridModelError) as e:
            models.GridConfig.from_dict(
                {**base_config(), "trailing": {"mode": "off"}})
        assert e.value.reason == "FEATURE_UNAVAILABLE"

    def test_start_types_other_than_immediate_unavailable(self):
        with pytest.raises(models.GridModelError) as e:
            models.GridConfig.from_dict({**base_config(), "start": "price"})
        assert e.value.reason == "FEATURE_UNAVAILABLE"

    def test_reserve_must_be_below_total(self):
        with pytest.raises(models.GridModelError):
            models.GridConfig.from_dict(
                {**base_config(),
                 "capital": {"total": "100", "reserve": "100",
                             "asset": "USDC"}})

    def test_lower_upper_ordering(self):
        with pytest.raises(models.GridModelError):
            models.GridConfig.from_dict({**base_config(),
                                         "lower_price": "200",
                                         "upper_price": "100"})

    def test_immediate_requires_direction(self):
        with pytest.raises(models.GridModelError):
            models.GridConfig.from_dict(
                {**base_config(), "initial_position": "immediate"})

    def test_count_float_rejected(self):
        with pytest.raises(models.GridModelError) as e:
            models.GridConfig.from_dict({**base_config(), "grid_count": 20.0})
        assert e.value.reason == "INTEGER_REQUIRED"

    def test_config_hash_stable_and_input_sensitive(self):
        a = models.GridConfig.from_dict(base_config())
        b = models.GridConfig.from_dict(base_config())
        c = models.GridConfig.from_dict({**base_config(),
                                         "grid_count": 21})
        assert a.config_hash == b.config_hash
        assert a.config_hash != c.config_hash
        # hash is over canonical JSON of to_dict
        import hashlib
        want = hashlib.sha256(json.dumps(
            a.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        assert a.config_hash == want

    def test_roundtrip(self):
        cfg = models.GridConfig.from_dict(base_config())
        again = models.GridConfig.from_dict(cfg.to_dict())
        assert again == cfg


class TestPlanDto:
    def test_plan_roundtrip_and_hash_fields(self):
        plan = models.Plan(
            schema_version=1, calc_version="grid-calc-v1", plan_kind="fixture",
            validation_status="VALID", live_eligible=False,
            config_hash="x" * 64, plan_hash="y" * 64, rules_revision="r1",
            capability_revision="c1", risk_snapshot_id=None, account="a",
            market="m", reference_source="mark", reference_price="105",
            normalized_lower="100", normalized_upper="110",
            levels=(models.PriceLevel(0, "100"),),
            cycles=(models.CycleLayout(0, 0, 1),), qty_per_grid="0.01",
            qty_reason=None, initial_position_qty="0",
            candidate_orders=(), blockers=(),
            center_index=1, initialization_status=None,
            quote_snapshot_id="q1")
        d = plan.to_dict()
        again = models.Plan.from_dict(d)
        assert again == plan

    def test_plan_unknown_field_rejected(self):
        with pytest.raises(models.GridModelError):
            models.Plan.from_dict({"schema_version": 1, "nonsense": True})

    def test_level_price_must_be_decimal_text(self):
        with pytest.raises(models.GridModelError):
            models.PriceLevel.from_dict({"level_index": 0, "price": 100.5})


def test_pause_on_budget_is_an_optional_creation_choice():
    """2026-11-06: the operator asked for a creation-time choice: should
    budget exhaustion pause the grid? Default preserves the pause; false
    keeps the grid running (venue margin admission stays the final gate)."""
    default = models.GridConfig.from_dict(base_config())
    assert default.pause_on_budget is True
    off = models.GridConfig.from_dict(base_config(pause_on_budget=False))
    assert off.pause_on_budget is False
    assert off.to_dict()["pause_on_budget"] is False
    with pytest.raises(models.GridModelError):
        models.GridConfig.from_dict(base_config(pause_on_budget="no"))

"""R-02 M3a: importer + performance accept the new event schema.

Covers:
* import: schema-v2 taker_attempt / hedge_fill rows become RESOLVED fills
  with a traceable dedupe key; legacy schema-1 attempts stay unresolved
  evidence (never silently promoted);
* fills_from_events: one Fill per actually filled taker leg (real avg
  price required) + hedge_fill; no fill when a leg has no fill/price
  (never a fabricated 0 or limit price);
* replay: synthetic taker + hedge events reproduce the hand-computed FIFO
  gross/fee/net (±1e-6);
* the ledger gates are unchanged: with funding absent `net_pnl` stays null.

Temp DB / temp JSONL only; no network, no engine.

Run:  python3 -m pytest tests/test_m3a_fills.py
"""
import json
import os
import sys
import tempfile
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.console import ledger as L  # noqa: E402
from entropy_arb.console import performance as P  # noqa: E402
from entropy_arb.console.importer import import_events_jsonl  # noqa: E402
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402

D = L.D

TAKER_PAYLOAD = {
    "schema_version": 2, "fill_id": "run-1:1", "side": "buy_entropy",
    "symbol": "SNDK", "buy_venue": "ENTROPY", "sell_venue": "RH",
    "qty": 1.0, "buy_fill": 1.0, "sell_fill": 1.0,
    "buy_avg_px": 100.0, "sell_avg_px": 101.0,
    "buy_limit": 99.9, "sell_limit": 101.1,
    "buy_status": "filled", "sell_status": "filled", "ok": True,
    "fee": {"buy_amount": 0.1, "sell_amount": 0.101,
            "currency": "USD", "source": "configured_estimate"},
}
HEDGE_PAYLOAD = {
    "schema_version": 2, "fill_id": "run-1:2", "venue": "RH",
    "symbol": "SNDK", "side": "buy", "qty": 1.0, "avg_px": 100.5,
    "limit_px": 100.6, "reduce_only": True,
    "fee": {"amount": 0.1005, "currency": "USD",
            "source": "configured_estimate"},
}


def _event(event_type, payload, ts, venue=None, unresolved=0,
           event_id=None):
    return {"event_id": event_id or f"ev-{event_type}-{ts}",
            "event_type": event_type, "event_ts": ts,
            "payload_json": json.dumps(payload),
            "unresolved": unresolved,
            "dedupe_key": payload.get("fill_id"),
            "venue": venue, "instrument": payload.get("symbol")}


def test_fills_from_m3a_events():
    events = [_event("taker_attempt", TAKER_PAYLOAD, 1000.0),
              _event("hedge_fill", HEDGE_PAYLOAD, 1001.0, venue="RH")]
    fills = P.fills_from_events(events)
    assert len(fills) == 3
    buy, sell, hedge = fills
    assert (buy.account, buy.side, buy.qty, buy.price) == \
        ("ENTROPY", "buy", D("1"), D("100"))
    assert buy.fill_id == "run-1:1:buy" and buy.fee == D("0.1")
    assert (sell.account, sell.side, sell.qty, sell.price) == \
        ("RH", "sell", D("1"), D("101"))
    assert sell.fill_id == "run-1:1:sell" and sell.fee == D("0.101")
    assert (hedge.account, hedge.side, hedge.qty, hedge.price) == \
        ("RH", "buy", D("1"), D("100.5"))
    assert hedge.fill_id == "run-1:2" and hedge.fee == D("0.1005")


def test_fills_require_a_real_fill_and_price():
    # unfilled legs / missing avg price → no fill (no limit-price fallback)
    payload = dict(TAKER_PAYLOAD)
    payload.update({"buy_fill": 0.0, "buy_avg_px": None})
    fills = P.fills_from_events([_event("taker_attempt", payload, 1000.0)])
    assert len(fills) == 1 and fills[0].side == "sell"
    # hedge without venue avg price → no fabricated fill
    hp = dict(HEDGE_PAYLOAD)
    hp["avg_px"] = None
    assert P.fills_from_events(
        [_event("hedge_fill", hp, 1001.0)]) == []
    # legacy schema-1 taker attempt (no avg prices) → evidence only
    legacy = {"schema_version": 1, "side": "buy_entropy",
              "fee": {"amount": None, "source": "missing"}}
    assert P.fills_from_events(
        [_event("taker_attempt", legacy, 999.0)]) == []


def test_replay_matches_hand_calculation():
    fills = P.fills_from_events([
        _event("taker_attempt", TAKER_PAYLOAD, 1000.0),
        _event("hedge_fill", HEDGE_PAYLOAD, 1001.0, venue="RH")])
    # hand: RH short 1 @101 closed by hedge buy 1 @100.5 → gross +0.5;
    # fees 0.1 + 0.101 + 0.1005 = 0.3015; HL long 1 @100 marked at 100.5
    # → unrealized_end 0.5 (given); funding known 0.
    res = L.compute_period(
        fills_start=[], fills_period=fills,
        marks_start={}, marks_end={},
        funding_net=L.ZERO, fees_period=None,
        unresolved_in_scope=0,
        fills_have_ids=all(f.fill_id for f in fills),
        funding_source="venue_api", fee_source="venue_fill",
        mark_source=None,
        unrealized_start=L.ZERO, unrealized_end=D("0.5"))
    assert D(res["components"]["gross_realized"]) == D("0.5")
    assert D(res["components"]["trading_fees"]) == D("0.3015")
    assert abs(D(res["net_pnl"]) - D("0.6985")) < D("1e-6")
    # gate unchanged: funding missing → net must stay null
    res2 = L.compute_period(
        fills_start=[], fills_period=fills,
        marks_start={}, marks_end={},
        funding_net=None, fees_period=None, unresolved_in_scope=0,
        fills_have_ids=True, funding_source=None, fee_source="venue_fill",
        mark_source=None,
        unrealized_start=L.ZERO, unrealized_end=D("0.5"))
    assert res2["net_pnl"] is None
    assert any(m["code"] == "funding_missing" for m in res2["missing"])


def _write_events_file(tmp, run_id="run-1", strategy_id="str-1"):
    path = os.path.join(tmp, "logs", "events", f"{run_id}.jsonl")
    os.makedirs(os.path.dirname(path))
    lines = [
        {"schema_version": 2, "event_type": "taker_attempt",
         "run_id": run_id, "strategy_id": strategy_id, "event_ts": 1000.0,
         **TAKER_PAYLOAD},
        {"schema_version": 2, "event_type": "hedge_fill",
         "run_id": run_id, "strategy_id": strategy_id, "event_ts": 1001.0,
         **HEDGE_PAYLOAD},
        # legacy event: no fill id, no prices → stays unresolved evidence
        {"schema_version": 1, "event_type": "taker_attempt",
         "run_id": run_id, "strategy_id": strategy_id, "event_ts": 900.0,
         "side": "buy_entropy",
         "fee": {"amount": None, "source": "missing"}},
    ]
    with open(path, "w") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    return path


def test_importer_resolves_m3a_events_but_keeps_legacy_unresolved():
    tmp = tempfile.mkdtemp(prefix="m3a-import-")
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    try:
        path = _write_events_file(tmp)
        rep = import_events_jsonl(storage, path=path, run_id="run-1",
                                  strategy_id="str-1")
        assert rep["imported"] == 3
        rows = storage.events_for_strategy("str-1", limit=100)
        by_type = {}
        for r in rows:
            by_type.setdefault(r["event_type"], []).append(r)
        taker = by_type["taker_attempt"]
        v2 = [r for r in taker if json.loads(
            r["payload_json"]).get("fill_id") == "run-1:1"][0]
        assert v2["unresolved"] == 0
        assert v2["dedupe_key"] == "t:run-1:1"
        p = json.loads(v2["payload_json"])
        assert p["buy_avg_px"] == 100.0 and p["sell_avg_px"] == 101.0
        assert p["fee"]["source"] == "configured_estimate"
        hedge = by_type["hedge_fill"][0]
        assert hedge["unresolved"] == 0
        assert hedge["dedupe_key"] == "t:run-1:2"
        legacy = [r for r in taker if json.loads(
            r["payload_json"]).get("fill_id") is None][0]
        assert legacy["unresolved"] == 1
        assert legacy["dedupe_key"] is None
    finally:
        storage.close()


def test_performance_replays_fills_but_net_stays_null_without_funding():
    tmp = tempfile.mkdtemp(prefix="m3a-perf-")
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    try:
        sid = storage.create_strategy(
            name="SNDK", symbol="SNDK", type_="taker_basis",
            base_venue="hl", base_market="io",
            hedge_venue="lighter-rh")["id"]
        path = _write_events_file(tmp, strategy_id=sid)
        import_events_jsonl(storage, path=path, run_id="run-1",
                            strategy_id=sid)
        perf = P.performance_for_strategy(
            storage, strategy_id=sid, start_ts=950.0, end_ts=1100.0,
            timezone="Asia/Shanghai")
        # replay is wired in: the taker legs + hedge fill are real fills
        assert perf["coverage"]["complete_fills"] == 3
        assert D(perf["components"]["gross_realized"]) == D("0.5")
        # gates unchanged: no funding attribution → no net number
        assert perf["net_pnl"] is None
        assert perf["reconciliation_status"] == "incomplete"
        assert any(m["code"] == "funding_missing"
                   for m in perf["missing"])
    finally:
        storage.close()

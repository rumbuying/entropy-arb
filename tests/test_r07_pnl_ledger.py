"""R-07a backend tests: fill-ledger today PnL + manual fills.

Covers docs/25-r07-pnl-ledger-brief.md (backend part):

* ledger replay: inherited position + exchange avg-entry anchor, backward
  walk to local midnight, forward replay → realized today;
* honesty: unknown cost basis stays `unknown_qty` (never guessed), no
  anchor → realized null (not 0), missing external fill → chain_mismatch;
* independent chain cross-check against the ~5-min valuation position
  snapshot;
* POST /api/manual-fills: validation, idempotency, audit, and the ledger
  picking the recorded external fill up so the chain closes again.

All isolated: temp DB + temp profiles + fake venue probes (no network,
no production paths).

Run: python3 -m pytest tests/test_r07_pnl_ledger.py -q
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from entropy_arb.console import ops as ops_mod  # noqa: E402
from entropy_arb.console import pnl_ledger as PL  # noqa: E402
from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.storage import Storage  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
TOKEN = "r07-test"
_ORIG_PROBE = ops_mod.probe_account_cached


def D(x):
    return Decimal(str(x))


def _fill(ts, side, qty, price, fee=None, pnl_gross=None):
    return {"ts": ts, "seq": 1, "side": side, "qty": D(qty),
            "price": None if price is None else D(price),
            "fee": None if fee is None else D(fee),
            "fee_est": None,
            "pnl_gross": None if pnl_gross is None else D(pnl_gross)}


def _compute(fills, anchors, start=None):
    return PL.compute(fills={"base": fills.get("base", []),
                             "hedge": fills.get("hedge", [])},
                      anchors={"base": anchors.get("base", {}),
                               "hedge": anchors.get("hedge", {})},
                      day_start_ts=1000.0, now_ts=2000.0,
                      start_snapshot=start)


# ------------------------------------------------------------- pure ledger

def test_inherited_position_uses_exchange_cost_anchor():
    """A close of a pre-existing position: basis from the venue avg entry."""
    fills = {"base": [_fill(1500, "sell", "0.5", "110")]}
    res = _compute(fills, {"base": {"position": "0.5", "avg_entry": "100"}})
    assert D(res["realized_today"]) == D("5"), res
    assert D(res["unknown_qty"]) == 0, res
    assert res["reason"] is None, res
    assert D(res["legs"]["base"]["start_position"]) == D("1.0"), res


def test_flat_anchor_round_trip_is_fully_known():
    fills = {"base": [_fill(1400, "buy", "1", "100"),
                      _fill(1600, "sell", "1", "105")]}
    res = _compute(fills, {"base": {"position": "0", "avg_entry": None}},
                   start={"ts": 1100.0, "positions": {"base": 0}})
    assert D(res["realized_today"]) == D("5"), res
    assert res["reason"] is None, res
    assert D(res["legs"]["base"]["start_position"]) == 0, res


def test_one_known_anchor_is_enough_when_the_other_leg_has_no_fills():
    res = _compute({"base": [_fill(1500, "sell", "0.5", "110")]},
                   {"base": {"position": "0.5", "avg_entry": "100"}})
    assert D(res["realized_today"]) == D("5"), res
    assert res["reason"] is None, res


def test_no_anchor_is_null_not_zero():
    res = _compute({"base": [_fill(1500, "sell", "0.5", "110")]}, {})
    assert res["realized_today"] is None, res
    assert res["reason"] == "no_ledger", res
    assert res["legs"]["base"]["reason"] == "no_anchor", res
    assert D(res["unknown_qty"]) == D("0.5"), res


def test_inherited_basis_unknown_stays_unknown():
    """Current position flat, but today started with a position whose cost
    was never recorded → the inherited close is unknown, not guessed."""
    fills = {"base": [
        _fill(1200, "sell", "1", "110"),     # closes inherited inventory
        _fill(1400, "buy", "1", "100"),      # re-open (basis known from here)
        _fill(1600, "sell", "1", "105"),     # close of the new lot
    ]}
    res = _compute(fills, {"base": {"position": "0", "avg_entry": None}},
                   start={"ts": 1100.0, "positions": {"base": 1}})
    assert D(res["unknown_qty"]) == D("1"), res
    assert D(res["realized_today"]) == D("5"), res
    assert res["reason"] in ("inherited_unknown", "partial_unknown"), res


def test_unknown_price_fill_invalidates_basis():
    """A legacy schema-1 fill has quantity but no price: the position moves,
    the basis cannot be recovered → closes are unknown."""
    fills = {"base": [_fill(1300, "buy", "1", None),
                      _fill(1500, "sell", "1", "105")]}
    res = _compute(fills, {"base": {"position": "0", "avg_entry": None}},
                   start={"ts": 1100.0, "positions": {"base": 0}})
    assert D(res["unknown_qty"]) == D("1"), res
    assert D(res["realized_today"]) == 0, res
    assert res["reason"] == "partial_unknown", res


def test_chain_mismatch_when_external_fill_is_missing():
    """Valuation snapshot says the day started at 1.1, but known fills only
    explain a start position of 0.5 → the missing 0.6 is an un-recorded
    external trade: realized must be null, never silently unterstated."""
    fills = {"base": [_fill(1500, "sell", "0.5", "110")]}
    anchors = {"base": {"position": "0.6", "avg_entry": "100"}}
    # current 0.6 − known sell 0.5 → implies start 1.1, but snapshot says 0.5
    res = _compute(fills, anchors,
                   start={"ts": 1100.0, "positions": {"base": 0.5}})
    assert res["reason"] == "chain_mismatch", res
    assert res["realized_today"] is None, res


def test_chain_check_passes_with_recorded_external_fill():
    fills = {"base": [_fill(1500, "sell", "0.5", "110"),
                      _fill(1200, "sell", "0.6", "109")]}
    anchors = {"base": {"position": "0", "avg_entry": None}}
    res = _compute(fills, anchors,
                   start={"ts": 1100.0, "positions": {"base": 1.1}})
    # the chain now closes (no chain_mismatch), but the inherited 1.1 lot's
    # cost basis is still unknown: both closes stay unknown, never guessed
    assert res["reason"] != "chain_mismatch", res
    assert res["reason"] == "partial_unknown", res
    assert res["legs"]["base"]["reason"] == "inherited_unknown", res
    assert D(res["unknown_qty"]) == D("1.1"), res
    assert D(res["realized_today"]) == 0, res


def test_venue_reported_closed_pnl_covers_unknown_basis():
    """A flattened position leaves no avg entry to replay from; the venue's
    own closedPnl on the recorded manual fill is the authoritative figure."""
    fills = {"base": [_fill(1200, "sell", "1", "110", pnl_gross="5")]}
    res = _compute(fills, {"base": {"position": "0", "avg_entry": None}},
                   start={"ts": 1100.0, "positions": {"base": 1}})
    assert res["reason"] != "chain_mismatch", res
    assert D(res["realized_today"]) == D("5"), res
    assert D(res["unknown_qty"]) == 0, res


# --------------------------------------------------------------- endpoints

class _Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="r07-")
        self.root = os.path.join(self.tmp, "root")
        self.pdir = os.path.join(self.tmp, "profiles")
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        os.makedirs(self.pdir, exist_ok=True)
        self.storage = Storage(os.path.join(self.tmp, "data",
                                            "console-v2.sqlite3"))
        self.profiles = ProfilesManager(
            self.pdir, env_file=os.path.join(self.tmp, ".env"))
        self.secrets = SecretsManager(os.path.join(self.tmp, ".env"))
        self.sup = Supervisor(self.root, self.pdir, storage=self.storage)
        self.probes = {}
        self.probe_calls = []

    def profile(self, name, *, dex="io"):
        with open(os.path.join(self.pdir, f"{name}.yaml"), "w") as fh:
            fh.write(f"entropy:\n  dex: {dex}\n")

    def line(self, *, profile, symbol, base="hl", hedge="lighter-rh",
             strategy_id="str-r07", started=None):
        self.storage.create_run(
            run_id="run-r07", worker_id="w1", profile=profile, symbol=symbol,
            hedge=hedge, base=base, mode="live", pid=None,
            cmdline_hash="x", started_ts=started or (time.time() - 86400),
            strategy_id=strategy_id)
        self.storage.create_strategy(
            name=symbol, symbol=symbol, type_="taker", base_venue=base,
            base_market=symbol, hedge_venue=hedge) \
            if self.storage.get_strategy(strategy_id) is None else None
        return strategy_id

    def event(self, strategy_id, etype, ts, payload):
        self.storage.insert_event(
            event_id=f"ev-{etype}-{ts}", event_type=etype,
            import_batch="t", source_id=1, source_line=1, event_ts=ts,
            payload=payload, strategy_id=strategy_id,
            run_id="run-r07", unresolved=False)

    def app(self):
        async def fake_probe(venue, symbol, *, env_file, role="hedge",
                             dex=""):
            self.probe_calls.append({"venue": venue, "role": role,
                                     "symbol": symbol, "dex": dex})
            return self.probes.get((venue, role), {
                "equity": None, "free": None, "position": None,
                "entry_px": None, "error": "no data"})
        ops_mod.probe_account_cached = fake_probe
        return create_app(self.sup, self.profiles, self.secrets,
                          token=TOKEN, storage=self.storage,
                          start_background=False)

    def close(self):
        ops_mod.probe_account_cached = _ORIG_PROBE
        self.storage.close()


async def _req(app, method, path, json_body=None):
    srv = TestServer(app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with http.request(method, srv.make_url(path),
                                    json=json_body) as r:
                return r.status, await r.json()
    finally:
        await srv.close()


def _day_start():
    return datetime.now(TZ).replace(hour=0, minute=0, second=0,
                                    microsecond=0).timestamp()


def _skip_near_midnight():
    """The day window is real time: a test run right after local midnight
    cannot place deterministic "today" events in the past."""
    if time.time() - _day_start() < 300:
        import pytest
        pytest.skip("too close to local midnight for a deterministic window")


def test_pnl_lines_ledger_and_manual_fill_backfill():
    _skip_near_midnight()
    env = _Env()
    day = _day_start()
    try:
        env.profile("sndk-rh")
        env.line(profile="sndk-rh", symbol="SNDK")
        # both legs flat at the start of the day
        env.storage.save_valuation(
            strategy_id="str-r07", boundary="timeseries", ts=day + 600,
            mark_source="venue_mark",
            payload={"unrealized": 0, "positions": {"entropy": 0,
                                                    "hedge": 0}})
        env.event("str-r07", "taker_attempt", day + 3600, {
            "side": "sell_entropy", "buy_fill": 0.1, "sell_fill": 0.1,
            "buy_avg_px": 1010, "sell_avg_px": 1010,
            "buy_venue": "RH", "sell_venue": "ENTROPY", "ok": True})
        env.probes = {
            ("hl", "base"): {"equity": 1, "free": 1, "position": 0.0,
                             "entry_px": None, "error": None},
            ("lighter-rh", "hedge"): {"equity": 1, "free": 1,
                                      "position": 0.0, "entry_px": None,
                                      "error": None},
        }

        # 1) without recording the external fill the chain cannot close:
        #    expected day-start position is flat, the known fills say -0.1.
        status, body = asyncio.run(_req(env.app(), "GET",
                                        "/api/pnl/lines?days=2&limit=0"))
        assert status == 200, body
        line = body["lines"][0]
        assert "ledger" in line and line["ledger"] is not None, line
        assert line["ledger"]["reason"] == "chain_mismatch", line["ledger"]
        assert line["ledger"]["realized_today"] is None

        # 2) record the operator's external fill → chain closes
        payload = {"venue": "hl", "symbol": "SNDK", "side": "buy",
                   "qty": "0.1", "avg_px": "1008", "ts": day + 4000,
                   "note": "operator backfill", "closed_pnl": "0.2",
                   "closed_pnl_semantics": "gross"}
        status, created = asyncio.run(_req(env.app(), "POST",
                                           "/api/manual-fills", payload))
        assert status == 201, created
        assert created["inserted"] is True, created
        assert created["fill"]["scope"] == "HL(io)", created
        assert created["fill"]["closed_pnl"] == "0.2", created
        # the hedge leg was closed by the same operator action
        status, other = asyncio.run(_req(env.app(), "POST",
                                         "/api/manual-fills", {
                                             "venue": "lighter-rh",
                                             "symbol": "SNDK",
                                             "side": "sell", "qty": "0.1",
                                             "avg_px": "1008",
                                             "ts": day + 4001}))
        assert status == 201, other
        assert other["fill"]["scope"] == "Lighter-RH", other
        # idempotent re-post
        status, again = asyncio.run(_req(env.app(), "POST",
                                        "/api/manual-fills", payload))
        assert status == 200 and again["inserted"] is False, again

        status, listed = asyncio.run(_req(env.app(), "GET",
                                          "/api/manual-fills"))
        assert status == 200 and listed["count"] == 2, listed

        status, body = asyncio.run(_req(env.app(), "GET",
                                        "/api/pnl/lines?days=2&limit=0"))
        assert status == 200, body
        ledger = body["lines"][0]["ledger"]
        assert ledger["reason"] is None, ledger
        assert D(ledger["unknown_qty"]) == 0, ledger
        # by_venue keys must line up with the line's account scopes
        assert set(ledger["by_venue"]) == set(line["scopes"]), ledger
        hv = ledger["by_venue"]["HL(io)"]
        lv = ledger["by_venue"]["Lighter-RH"]
        assert D(hv["realized_gross"]) == D("0.2"), ledger
        assert D(lv["realized_gross"]) == D("-0.2"), ledger
        # no fee was recorded on the manual fills; engine events only carry
        # configured estimates → never labeled as actual charges
        assert hv["fee_source"] in ("configured_estimate", "unknown"), hv
        assert D(ledger["realized_today"]) == 0, ledger
        assert ledger["fee_total"]["source"] in (
            "configured_estimate", "unknown"), ledger["fee_total"]
    finally:
        env.close()


def test_manual_fill_validation():
    _skip_near_midnight()
    env = _Env()
    try:
        app = env.app()
        for bad in (
                {"venue": "hl", "symbol": "SNDK", "side": "hold",
                 "qty": "1", "avg_px": "2", "ts": time.time()},
                {"venue": "hl", "symbol": "SNDK", "side": "buy",
                 "qty": "0", "avg_px": "2", "ts": time.time()},
                {"venue": "hl", "symbol": "SNDK", "side": "buy",
                 "qty": "1", "avg_px": "-2", "ts": time.time()},
                {"venue": "hl", "symbol": "SNDK", "side": "buy",
                 "qty": "1", "avg_px": "2", "ts": time.time() + 3600},
                {"venue": "", "symbol": "SNDK", "side": "buy",
                 "qty": "1", "avg_px": "2", "ts": time.time()},
                {"venue": "katana", "symbol": "HYPE", "side": "buy",
                 "qty": "1", "avg_px": "2", "ts": time.time(),
                 "closed_pnl": "1"},
        ):
            status, out = asyncio.run(_req(env.app(), "POST",
                                           "/api/manual-fills", bad))
            assert status == 400, (bad, out)
    finally:
        env.close()

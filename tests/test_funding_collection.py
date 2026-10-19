"""R-02 T1: funding collection — payments vs. predicted-rate snapshots.

Two layers are covered:
* funding_payment_rows(): a payment API returns {ts, amount_usd}; the
  arcus/ondo adapters' fetch_funding returns a predicted-rate snapshot. The
  snapshot must be rejected EXPLICITLY (never counted as 0 funding).
* the real _funding_loop: one round with a fake venue confirms payments are
  inserted as resolved `funding` events and that a rate snapshot produces a
  coverage note instead of an exception / a fake 0.

Everything uses temp dirs/DB; no network, no production console, no engine.

Run:  python3 -m pytest tests/test_funding_collection.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager  # noqa: E402
from entropy_arb.console.server import (create_app,  # noqa: E402
                                        funding_payment_rows)
from entropy_arb.console.storage import Storage, new_run_id  # noqa: E402
from entropy_arb.console.supervisor import Supervisor, Worker  # noqa: E402

PROFILE_YAML = """\
thresholds:
  midline_bps: 0.0
  upper_bps: 3.0
  lower_bps: 3.0
entropy:
  dex: io
logging:
  trades_csv: logs/trades-NEAR-arcus.csv
"""

PAYMENT = {"ts": 1792500000.0, "amount_usd": -0.42, "market": "NEAR",
           "rate": 0.0001, "index_price": 2.5, "position_qty": 10.0}
RATE_SNAPSHOT = {"market": "NEAR", "rate": 0.0001, "interval_ends": 1792503600}


def test_funding_payment_rows_classification():
    assert funding_payment_rows([PAYMENT]) == ([PAYMENT], None)
    assert funding_payment_rows([]) == ([], None)
    # predicted-rate snapshot (arcus/ondo today) is NOT a payment
    assert funding_payment_rows([RATE_SNAPSHOT]) == \
        ([], "payment_api_unavailable")
    # mixed / malformed rows are rejected wholesale, never partially summed
    assert funding_payment_rows([PAYMENT, RATE_SNAPSHOT]) == \
        ([], "payment_api_unavailable")
    assert funding_payment_rows([None]) == ([], "payment_api_unavailable")
    assert funding_payment_rows([{"ts": 1.0}]) == \
        ([], "payment_api_unavailable")


def _env(fetch_rows):
    tmp = tempfile.mkdtemp(prefix="funding-")
    pdir = os.path.join(tmp, "profiles")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "P1.yaml"), "w") as fh:
        fh.write(PROFILE_YAML)
    storage = Storage(os.path.join(tmp, "data", "console-v2.sqlite3"))
    profiles = ProfilesManager(pdir, env_file=os.path.join(tmp, ".env"))
    secrets = SecretsManager(os.path.join(tmp, ".env"))
    sup = Supervisor(tmp, pdir, storage=storage)
    sid = storage.create_strategy(
        name="NEAR arcus", symbol="NEAR", type_="taker_basis",
        base_venue="hl", base_market="io",
        hedge_venue="arcus")["id"]
    rid = new_run_id()
    storage.create_run(
        run_id=rid, worker_id="w1", profile="P1", symbol="NEAR",
        hedge="arcus", base="hl", mode="live", pid=os.getpid(),
        cmdline_hash="h", started_ts=time.time() - 60, strategy_id=sid)
    w = Worker("w1", "P1", "NEAR", "arcus", "live", 0, base="hl")
    w.adopted_pid = os.getpid()
    w.run_id = rid
    w.started_ts = time.time() - 60
    sup.workers["w1"] = w

    async def fake_snapshot(wid):
        return None
    sup.snapshot = fake_snapshot

    class FakeVenue:
        async def load_market(self):
            return None

        def init_signer(self):
            return None

        async def fetch_funding(self):
            return fetch_rows

    import entropy_arb.console.ops as ops_mod
    orig = ops_mod._make_venue
    ops_mod._make_venue = lambda vc, sess, timeout: FakeVenue()
    return {"tmp": tmp, "storage": storage, "profiles": profiles,
            "secrets": secrets, "sup": sup, "sid": sid, "rid": rid,
            "ops_mod": ops_mod, "orig": orig}


def _run_one_round(env):
    async def run():
        app = create_app(env["sup"], env["profiles"], env["secrets"],
                         storage=env["storage"], start_background=False,
                         funding_collect=True)
        try:
            # the funding task's first (and only, before the 900s sleep)
            # round runs immediately; the valuation task sees no snapshot
            await asyncio.sleep(0.2)
        finally:
            tasks = [app.get("funding_task"), app.get("valuation_task")]
            for t in tasks:
                if t is not None:
                    t.cancel()
            for t in tasks:
                if t is not None:
                    try:
                        await t
                    except BaseException:
                        pass
        return app
    return asyncio.run(run())


def test_funding_round_inserts_payment_events():
    env = _env([PAYMENT])
    try:
        app = _run_one_round(env)
        events = env["storage"].events_for_strategy(
            env["sid"], limit=100)
        funding = [e for e in events if e["event_type"] == "funding"]
        assert len(funding) == 1
        assert funding[0]["unresolved"] == 0
        assert funding[0]["dedupe_key"] == f"f:arcus:NEAR:" \
                                           f"{int(PAYMENT['ts'] * 1000)}"
        coverage = app["funding_coverage"]
        arcus = coverage[f"{env['sid']}:arcus:hedge"]
        assert arcus["supported"] is True
        # HL base leg has no funding payment API → explicit note, not 0
        hl = coverage[f"{env['sid']}:hl:entropy"]
        assert hl["supported"] is False
        assert hl["reason"] == "venue_unsupported"
    finally:
        env["ops_mod"]._make_venue = env["orig"]
        env["storage"].close()


def test_funding_round_handles_rate_snapshot_without_raising(caplog):
    import logging
    env = _env([RATE_SNAPSHOT])
    try:
        caplog.set_level(logging.WARNING)
        app = _run_one_round(env)
        events = env["storage"].events_for_strategy(env["sid"], limit=100)
        assert [e for e in events if e["event_type"] == "funding"] == []
        arcus = app["funding_coverage"][f"{env['sid']}:arcus:hedge"]
        assert arcus["supported"] is False
        assert arcus["reason"] == "payment_api_unavailable"
        assert "付款" in arcus["note"]
        # T1/Chief: exactly one `funding unsupported: <venue>` WARNING per
        # round, no exception, wording carries the venue name
        msgs = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING
                and "funding unsupported" in r.getMessage()]
        assert any(m.startswith("funding unsupported: arcus") for m in msgs)
        assert sum(1 for m in msgs if ": arcus" in m) == 1
        assert all(m.startswith("funding unsupported: ") for m in msgs)
    finally:
        env["ops_mod"]._make_venue = env["orig"]
        env["storage"].close()

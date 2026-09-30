"""Ledger accounting tests (V2-009): the spec §14.2 synthetic cases A–E
plus FIFO edge cases — exact Decimal expectations, no screenshots.

Run:  python3 -m pytest tests/test_ledger.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from decimal import Decimal  # noqa: E402

from entropy_arb.console.ledger import (Fill, compute_period,  # noqa: E402
                                        dedupe_fills, fifo_lots, unrealized)

D = Decimal
M = {"acct": {"MAKER": D("99"), "HEDGE": D("104")}}   # case A end marks


def F(seq, side, qty, px, fee="0", fill_id=None, instrument="MAKER",
       ts=1.0):
    return Fill(ts=ts, seq=seq, event_id=f"e{seq}", account="acct",
                instrument=instrument, side=side, qty=D(qty), price=D(px),
                fee=D(fee) if fee is not None else None,
                fee_currency="USDC", fill_id=fill_id)


def marks(spec):
    return {("acct", k): D(v) for k, v in spec.items()}


def test_case_a_maker_hedged_not_exited():
    # maker BUY 1 @100 (fee .1), hedge SELL 1 @102 (fee .1); no exit
    fills = [F(1, "buy", "1", "100", fee="0.1"),
             F(2, "sell", "1", "102", fee="0.1", instrument="HEDGE")]
    r = compute_period(
        fills_start=[], fills_period=fills,
        marks_start={}, marks_end=marks({"MAKER": "99", "HEDGE": "104"}),
        funding_net=D("0"), fees_period=None,
        fee_source="venue_fill", mark_source="venue_mark")
    c = r["components"]
    assert D(c["gross_realized"]) == 0
    # unreal_end = (99-100) + (104-102) = -3
    assert D(c["unrealized_end"]) == D("-3")
    assert D(c["unrealized_start"]) == 0
    assert D(c["trading_fees"]) == D("0.2")
    # net = 0 + (-3 - 0) + 0 - 0.2 = -3.2
    assert D(r["net_pnl"]) == D("-3.2")
    # the matched gross edge +2 / fee-adjusted +1.8 is EVIDENCE, shown
    # separately — it must not overwrite net
    assert r["status"] in ("estimated", "reconciled")


def test_case_b_taker_cross_midnight_exit():
    # previous day: long base 1 @100, short hedge 1 @105
    prior = [F(1, "buy", "1", "100", instrument="BASE", ts=0),
             F(2, "sell", "1", "105", instrument="HEDGE", ts=0)]
    # period: close both — base sell 1 @103, hedge buy 1 @102
    period = [F(3, "sell", "1", "103", fee="0.25", instrument="BASE", ts=10),
              F(4, "buy", "1", "102", fee="0.25", instrument="HEDGE",
                ts=11)]
    r = compute_period(
        fills_start=prior, fills_period=period,
        marks_start=marks({"BASE": "101", "HEDGE": "104"}),
        marks_end={},
        funding_net=D("-0.2"), fees_period=None,
        fee_source="venue_fill", funding_source="venue_funding",
        mark_source="venue_mark")
    c = r["components"]
    # gross realized = (103-100) + (105-102) = 6
    assert D(c["gross_realized"]) == 6
    # unreal_start = (101-100) + (105-104) = +2; end flat = 0
    assert D(c["unrealized_start"]) == 2
    assert D(c["unrealized_end"]) == 0
    assert D(c["trading_fees"]) == D("0.5")
    # net = 6 + (0-2) + (-0.2) - 0.5 = 3.3 — NOT 5.3 (the prior day's +2
    # must not be booked again)
    assert D(r["net_pnl"]) == D("3.3")
    assert r["status"] == "reconciled"


def test_case_b_missing_open_history_stays_incomplete():
    # same as B but the prior-day fills are missing: the +2 opening unreal
    # cannot be verified → no reconciled 3.3
    period = [F(3, "sell", "1", "103", fee="0.25", instrument="BASE"),
              F(4, "buy", "1", "102", fee="0.25", instrument="HEDGE")]
    r = compute_period(
        fills_start=[], fills_period=period,
        marks_start=marks({"BASE": "101", "HEDGE": "104"}),
        marks_end={}, funding_net=D("-0.2"), fees_period=None,
        fee_source="venue_fill", mark_source="venue_mark")
    # without the prior fills the "close" legs OPEN opposite lots, so
    # nothing realizes and the end valuation lacks marks for the phantom
    # inventory — the missing open history yields NO number at all
    assert r["net_pnl"] is None
    assert r["status"] == "incomplete"


def test_case_c_rebate_and_duplicate_import():
    fills = [F(1, "buy", "1", "100", fee="-0.1", fill_id="f1",
               instrument="X")]
    r = compute_period(
        fills_start=[], fills_period=fills,
        marks_start={}, marks_end={},
        funding_net=D("-2"), fees_period=None,
        other_costs=D("0"),
        fee_source="venue_fill", funding_source="venue_funding",
        mark_source="venue_mark")
    c = r["components"]
    assert D(c["trading_fees"]) == D("-0.1")   # rebate: negative cost
    assert D(c["funding_net"]) == D("-2")
    # gross 10 comes from a closed round trip; add one via an exit
    fills2 = fills + [F(2, "sell", "1", "110", fee="-0.1", fill_id="f2",
                        instrument="X")]
    r2 = compute_period(
        fills_start=[], fills_period=fills2,
        marks_start={}, marks_end={}, funding_net=D("-2"),
        fees_period=None, other_costs=D("0"),
        fee_source="venue_fill", funding_source="venue_funding",
        mark_source="venue_mark")
    # gross = (110-100) = 10; net = 10 + 0 - 2 - (-0.2) - 0 = 8.2
    assert D(r2["components"]["gross_realized"]) == 10
    assert D(r2["net_pnl"]) == D("8.2")
    assert r2["status"] == "reconciled"

    # duplicate import of the same fill id does not change the result
    dup = fills2 + [F(3, "sell", "1", "110", fee="-0.1", fill_id="f2",
                      instrument="X")]
    deduped = dedupe_fills(dup)
    assert len(deduped) == 2
    r3 = compute_period(
        fills_start=[], fills_period=deduped,
        marks_start={}, marks_end={}, funding_net=D("-2"),
        fees_period=None, other_costs=D("0"),
        fee_source="venue_fill", funding_source="venue_funding",
        mark_source="venue_mark")
    assert r3["net_pnl"] == r2["net_pnl"]


def test_case_d_partial_close_unknown_fee():
    fills_start = [F(1, "buy", "2", "100", instrument="X", ts=0)]
    period = [F(2, "sell", "1", "105", fee=None, fill_id="f9",
                instrument="X")]
    r = compute_period(
        fills_start=fills_start, fills_period=period,
        marks_start=marks({"X": "100"}), marks_end=marks({"X": "101"}),
        funding_net=D("0"), fees_period=None,
        fee_source="venue_fill", mark_source="venue_mark")
    # fee unknown → net null, incomplete; gross realized still exact (5)
    assert D(r["components"]["gross_realized"]) == 5
    assert r["net_pnl"] is None
    assert r["status"] == "incomplete"
    assert any(m["code"] == "fee_missing" for m in r["missing"])
    # remaining 1 lot values at mark 101
    assert D(r["components"]["unrealized_end"]) == 1


def test_case_fifo_partial_and_cross_zero():
    books = fifo_lots([F(1, "buy", "2", "100", instrument="X"),
                       F(2, "buy", "1", "110", instrument="X"),
                       F(3, "sell", "1", "105", instrument="X")])
    lots = books[("acct", "X")]
    # FIFO: the sell closes the FIRST lot partially → remaining long 1@100
    # plus long 1@110
    assert [(l.side, str(l.qty), str(l.px)) for l in lots] == \
        [("long", "1", "100"), ("long", "1", "110")]

    # cross zero: short 1@50 then buy 3@60 → closes the short, opens long 2
    books2 = fifo_lots([F(1, "sell", "1", "50", instrument="Y"),
                        F(2, "buy", "3", "60", instrument="Y")])
    lots2 = books2[("acct", "Y")]
    assert [(l.side, str(l.qty), str(l.px)) for l in lots2] == \
        [("long", "2", "60")]
    # realized on the close = (50-60)*1 = -10 with the split booked
    r = compute_period(
        fills_start=[F(1, "sell", "1", "50", instrument="Y", ts=0)],
        fills_period=[F(2, "buy", "3", "60", fee="0", fill_id="z",
                        instrument="Y")],
        marks_start=marks({"Y": "55"}), marks_end=marks({"Y": "60"}),
        funding_net=D("0"), fees_period=None,
        fee_source="venue_fill", mark_source="venue_mark")
    assert D(r["components"]["gross_realized"]) == D("-10")
    # unreal_start = (50-55) = -5; end = 2*(60-60) = 0
    assert D(r["components"]["unrealized_start"]) == D("-5")
    assert D(r["components"]["unrealized_end"]) == 0
    assert D(r["net_pnl"]) == D("-5")    # -10 + (0 - -5) + 0 - 0 - 0


def test_case_e_shared_account_deduped_elsewhere():
    # the ledger itself is per-strategy; the ACCOUNT dedupe (two workers
    # reporting the same equity = one account) lives in the accounts API —
    # here: two strategies on one account never copy each other's lots
    a = [F(1, "buy", "1", "100", instrument="X")]
    b = [F(1, "buy", "1", "100", instrument="X")]
    r_a = compute_period(fills_start=[], fills_period=a,
                         marks_start={}, marks_end=marks({"X": "101"}),
                         funding_net=D("0"), fees_period=None,
                         fee_source="venue_fill", mark_source="venue_mark")
    r_b = compute_period(fills_start=[], fills_period=b,
                         marks_start={}, marks_end=marks({"X": "101"}),
                         funding_net=D("0"), fees_period=None,
                         fee_source="venue_fill", mark_source="venue_mark")
    # each strategy's sub-ledger shows +1; the ACCOUNT total position is 2
    # (verified in the accounts API against the exchange's actual net
    # position — the same fetch_position() is never copied per strategy)
    assert D(r_a["components"]["unrealized_end"]) == 1
    assert D(r_b["components"]["unrealized_end"]) == 1


def test_missing_mark_or_funding_blocks_net():
    fills = [F(1, "buy", "1", "100", fee="0.1", fill_id="f", instrument="X")]
    r = compute_period(fills_start=[], fills_period=fills,
                       marks_start={}, marks_end={},
                       funding_net=None, fees_period=None,
                       fee_source="venue_fill", mark_source=None)
    assert r["net_pnl"] is None
    assert r["status"] == "incomplete"
    codes = {m["code"] for m in r["missing"]}
    assert {"boundary_valuation_missing", "funding_missing"} <= codes

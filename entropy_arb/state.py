"""Engine state snapshot: one pure function consumed by every UI.

The Rich terminal dashboard and the web console both render from
build_snapshot(eng) -> plain JSON-safe dict. Keeping the field assembly in
one place means the two UIs can never disagree about what the engine is
doing, and the function is trivially unit-testable (no rendering involved).

Snapshot shape (all values JSON-safe; None where unknown):

    ts, uptime_sec, markets_ready, record_only, status, stale_count
    symbol, hedge_name, entropy_dex
    venues.entropy / venues.hedge:
        name, bid, ask, spread_bps, book_age_sec, fresh, position,
        position_usd, volume_usd, equity, free, down, limited
    session:
        pnl_mtm, account_delta, equity_sum, exp_edge, fill_edge, trades,
        hedges, net_delta, net_tolerance_base, consec_errors,
        last_trade_ago_sec, minute_rows
    signal:
        mid_premium_bps, midline_bps, upper_bps, lower_bps,
        band_low_bps, band_high_bps, entropy_fee_bps, hedge_fee_bps,
        directions[]: key, label, exec_prem_bps, hurdle_bps, gap_bps, armed
    recent_trades[]: ts, direction, qty, notional, prem_bps, exp, fill,
        status, ok
    events[]: [level_no, line]        (only when a log buffer is supplied)
    config: thresholds/sizing/inventory/execution echo for display

`status` mirrors the terminal header's precedence: halted > venue_down >
stale > rate_limited > recording/running, plus "starting" before markets
resolve.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


def _venue_state(eng, v, now: float) -> Dict[str, Any]:
    bb, ba, m = v.book.best_bid(), v.book.best_ask(), v.book.mid()
    fresh = v.book.is_fresh(eng.cfg.staleness_sec)
    pos_usd = abs(v.position) * m if (m is not None and v.position) else None
    age = now - v.book.last_update_ts if v.book.ready else None
    return {
        "name": v.name,
        "key": v.key,
        "bid": bb,
        "ask": ba,
        "spread_bps": (ba / bb - 1.0) * 1e4 if (bb and ba) else None,
        "book_age_sec": age,
        "fresh": fresh,
        "position": v.position,
        "position_usd": pos_usd,
        "volume_usd": v.volume_usd,
        "equity": v.equity,
        "free": v.free,
        "down": v.key in eng._venue_down,
        "limited": eng._venue_limited(v),
    }


def _direction_state(eng, buy, sell, dkey: str, base_hurdle_bps: float) \
        -> Dict[str, Any]:
    """Mirror dashboard._dir_row: executable premium vs the full hurdle
    (both taker fees + inventory ladder surcharge included)."""
    ba, sb = buy.book.best_ask(), sell.book.best_bid()
    hurdle = (base_hurdle_bps + buy.fee_bps + sell.fee_bps
              + eng._inv_add_bps(buy, sell))
    if not (ba and sb):
        return {"key": dkey, "buy": buy.name, "sell": sell.name,
                "exec_prem_bps": None, "hurdle_bps": hurdle,
                "gap_bps": None, "armed": eng._armed.get(dkey) is not None}
    prem = (sb / ba - 1.0) * 1e4
    return {"key": dkey, "buy": buy.name, "sell": sell.name,
            "exec_prem_bps": prem, "hurdle_bps": hurdle,
            "gap_bps": prem - hurdle,
            "armed": eng._armed.get(dkey) is not None}


def build_snapshot(eng, log_buffer=None) -> Dict[str, Any]:
    cfg = eng.cfg
    now = time.time()
    snap: Dict[str, Any] = {
        "ts": now,
        "uptime_sec": now - eng.start_ts,
        "record_only": eng.record_only,
        "symbol": cfg.symbol,
        "hedge_name": None,
        "entropy_dex": cfg.entropy.hl_dex,
        "venues": {},
        "session": {},
        "signal": {},
        "recent_trades": [],
        "events": [],
        "config": {},
    }

    if eng.entropy is None or eng.hedge is None or not eng.markets_ready:
        snap["status"] = "starting"
        snap["markets_ready"] = False
        snap["stale_count"] = 0
        if log_buffer is not None:
            snap["events"] = [list(e) for e in log_buffer.lines][-30:]
        return snap

    snap["markets_ready"] = True
    snap["hedge_name"] = eng.hedge.name

    # ---- status (same precedence as the terminal header) -------------------
    stale = sum(1 for v in eng.venues.values()
                if not v.book.is_fresh(cfg.staleness_sec))
    snap["stale_count"] = stale
    if eng.halted:
        snap["status"] = "halted"
    elif eng._venue_down:
        snap["status"] = "venue_down"
    elif stale:
        snap["status"] = "stale"
    elif any(eng._venue_limited(v) for v in eng.venues.values()):
        snap["status"] = "rate_limited"
    elif eng.record_only:
        snap["status"] = "recording"
    else:
        snap["status"] = "running"

    # ---- venues ------------------------------------------------------------
    for key, v in eng.venues.items():
        snap["venues"][key] = _venue_state(eng, v, now)

    # ---- session ------------------------------------------------------------
    eqs = [v.equity for v in eng.venues.values()]
    net = sum(v.position for v in eng.venues.values())
    snap["session"] = {
        "pnl_mtm": eng.session_pnl(),
        "account_delta": eng.account_delta(),
        "equity_sum": (sum(eqs) if all(e is not None for e in eqs) else None),
        "exp_edge": eng.total_exp_edge,
        "fill_edge": eng.total_fill_edge,
        "trades": eng.trades,
        "hedges": eng.hedges,
        "net_delta": net,
        "net_tolerance_base": cfg.net_tolerance_base,
        "consec_errors": eng.consec_errors,
        "last_trade_ago_sec": (now - eng.last_trade_ts
                               if eng.last_trade_ts else None),
        "minute_rows": eng.recorder.rows_written if eng.recorder else 0,
    }

    # ---- signal -------------------------------------------------------------
    prem = eng.premium_bps()
    snap["signal"] = {
        "mid_premium_bps": prem,
        "midline_bps": cfg.midline_bps,
        "upper_bps": cfg.upper_bps,
        "lower_bps": cfg.lower_bps,
        "band_low_bps": cfg.midline_bps - cfg.lower_bps,
        "band_high_bps": cfg.midline_bps + cfg.upper_bps,
        "entropy_fee_bps": eng.entropy.fee_bps,
        "hedge_fee_bps": eng.hedge.fee_bps,
        "directions": [
            _direction_state(eng, eng.hedge, eng.entropy, "sell_entropy",
                             cfg.midline_bps + cfg.upper_bps),
            _direction_state(eng, eng.entropy, eng.hedge, "buy_entropy",
                             cfg.lower_bps - cfg.midline_bps),
        ],
    }

    # ---- recent executions (newest last, as stored) --------------------------
    snap["recent_trades"] = [dict(r) for r in eng.recent_trades]

    # ---- config echo ---------------------------------------------------------
    snap["config"] = {
        "midline_bps": cfg.midline_bps,
        "upper_bps": cfg.upper_bps,
        "lower_bps": cfg.lower_bps,
        "take_fraction": cfg.take_fraction,
        "max_order_notional": cfg.max_order_notional,
        "min_order_notional": cfg.min_order_notional,
        "inventory_scale_bps": cfg.inventory_scale_bps,
        "inventory_floor_frac": cfg.inventory_floor_frac,
        "premium_persist_sec": cfg.premium_persist_sec,
        "cooldown_sec": cfg.cooldown_sec,
        "leg_slippage_bps": cfg.leg_slippage_bps,
        "hedge_slippage_bps": cfg.hedge_slippage_bps,
        "staleness_sec": cfg.staleness_sec,
        "recorder_enabled": cfg.recorder_enabled,
        "entropy_fee_bps": eng.entropy.fee_bps,
        "hedge_fee_bps": eng.hedge.fee_bps,
        "entropy_cap_usd": eng.entropy.cap_usd,
        "hedge_cap_usd": eng.hedge.cap_usd,
    }

    if log_buffer is not None:
        snap["events"] = [list(e) for e in log_buffer.lines][-30:]
    return snap

"""Two-venue arbitrage engine: Entropy vs one hedge venue.

The signal is a fixed band around a configured midline (config.yaml):

    SELL entropy / BUY hedge  when executable premium >= midline + upper (+fees)
    BUY entropy / SELL hedge  when executable premium <= midline - lower (+fees)

Around the signal: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. There is no paper mode: the
bot either trades live or runs --record-only (data collection, no strategy).
Both venues' books are recorded to 1-minute CSV bars throughout.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from collections import deque
from typing import Dict, List, Optional

import aiohttp

from .book import ArbPlan, floor_step, plan_arb
from .config import Config, read_band
from .maker import (FillEvent, MakerQuote, inventory_skew_bps, quote_prices,
                    requote_reason)
from .recorder import MinuteRecorder
from .venue_hl import HLVenue
from .venue_katana import KatanaVenue
from .venue_lighter import LighterVenue

log = logging.getLogger("engine")

CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]
BALANCE_POLL_SEC = 30.0


class Engine:
    def __init__(self, cfg: Config, record_only: bool = False,
                 config_path: str = "") -> None:
        self.cfg = cfg
        self.record_only = record_only
        self.config_path = config_path   # set → band hot-reload enabled
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy = None
        self.hedge = None
        self.venues: Dict[str, object] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self.halted = False
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # escalating pause after margin rejections (collateral exhausted):
        # key -> next pause seconds; reset by a clean two-leg fill
        self._margin_backoff: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        # ---- maker mode (MAKER-DESIGN.md): quoting on the maker venue,
        # hedging fills on the taker hedge venue. roles set in
        # _setup_maker_roles; everything below is state, not config.
        self.maker = None                   # maker-role venue (quotes here)
        self.taker_hedge = None             # taker-role venue (hedges here)
        self._mk_quotes: dict = {}          # side ("bid"/"ask") -> MakerQuote
        self._mk_pending = 0.0              # signed qty to hedge (+buy/−sell)
        self._mk_pending_first_ts = None
        self._mk_hedge_evt = asyncio.Event()
        self._mk_hedge_failures = 0
        self._mk_exposed = False
        self._mk_blocked_reason = None
        self._mk_last_clear_ts = 0.0
        self._mk_fills = 0
        self._mk_hedges = 0
        self._mk_last_skiplog = 0.0
        self._mk_batch_first_fill_ts = None
        self._mk_batch_fills = 0
        self._mk_fills_buy: deque = deque()    # (qty, px) since last hedge
        self._mk_fills_sell: deque = deque()
        self._mk_hedge_sent_ts = None
        # adverse-selection samples: dicts with prem captured at fill, +1s, +10s
        self._mk_selection: list = []

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.time()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.time() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = time.time() + self.cfg.rate_limit_pause_sec

    def _margin_pause(self, key: str) -> float:
        """Escalating pause after a margin rejection: 60s doubling to a 15min
        cap, reset by the next clean two-leg fill. A margin rejection is not
        a 429 — retrying on the same clock just fills the entry leg, gets the
        hedge rejected, and bleeds a forced round trip (anth-rh 2026-09-19:
        8 cycles in 45s)."""
        pause = min(max(self._margin_backoff.get(key, 30.0) * 2.0, 60.0), 900.0)
        self._margin_backoff[key] = pause
        return pause
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.time())

    def request_stop(self) -> None:
        self.stop.set()
        self._update_evt.set()
        self._reconcile_evt.set()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        reload_task = None
        if self.config_path:
            reload_task = asyncio.create_task(self._hot_reload_bands())
        try:
            await self._run_inner()
        finally:
            if reload_task is not None:
                reload_task.cancel()
                try:
                    await reload_task
                except asyncio.CancelledError:
                    pass
            await self.session.close()

    async def _hot_reload_bands(self, poll_sec: float = 60.0) -> None:
        """Adopt midline/upper/lower from the profile file whenever it
        changes — manual edits in the console editor and files rewritten by
        tools/auto_band.py both land here, with no restart and no
        interruption to positions. Everything else in the file still
        requires a restart; the three band fields are the only live values.
        A rejected edit (invalid yaml/schema) keeps the current band."""
        try:
            last = os.stat(self.config_path).st_mtime
        except OSError:
            log.warning("band hot-reload: cannot stat %s — disabled",
                        self.config_path)
            return
        while True:
            await asyncio.sleep(poll_sec)
            try:
                mtime = os.stat(self.config_path).st_mtime
            except OSError:
                continue
            if mtime == last:
                continue
            last = mtime
            try:
                mid, up, lo = read_band(self.config_path)
            except Exception as e:
                log.warning("band hot-reload: %s rejected — keeping current "
                            "band (%s)", self.config_path, e)
                continue
            if (mid, up, lo) == (self.cfg.midline_bps, self.cfg.upper_bps,
                                 self.cfg.lower_bps):
                continue
            self.cfg.midline_bps, self.cfg.upper_bps, self.cfg.lower_bps = \
                mid, up, lo
            log.info("band hot-reloaded from %s: midline=%+.2f "
                     "band=[-%.2f, +%.2f]", self.config_path, mid, lo, up)

    def _make_venue(self, vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, self.session, self.cfg.settle_timeout_sec)
        if vc.kind == "katana":
            return KatanaVenue(vc, self.session, self.cfg.settle_timeout_sec)
        return HLVenue(vc, self.cfg.hl_api_url, self.cfg.hl_ws_url,
                       self.session, self.cfg.settle_timeout_sec)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        self.entropy = self._make_venue(cfg.entropy)
        self.hedge = self._make_venue(cfg.hedge)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
        await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())
        self.markets_ready = True

        live = not self.record_only
        if live:
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
            if self.entropy.kind == "hl" and self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
        if (self.hedge.kind == "hl" and self.entropy.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False  # shared account: count once

        self._step = 10 ** -min(self.entropy.size_decimals,
                                self.hedge.size_decimals)
        self._min_base = max(self.entropy.min_base, self.hedge.min_base,
                             self._step)
        self._min_notional = max(cfg.min_order_notional,
                                 self.entropy.min_quote, self.hedge.min_quote)
        log.info("pair %s(%s)-%s(%s): midline=%+.2fbps band=[-%.2f, +%.2f] "
                 "fees=%.2f+%.2f step=%g min_ntl=$%g",
                 self.entropy.name, self.entropy.conf.symbol, self.hedge.name,
                 self.hedge.conf.symbol, cfg.midline_bps, cfg.lower_bps,
                 cfg.upper_bps, self.entropy.fee_bps, self.hedge.fee_bps,
                 self._step, self._min_notional)

        if self.record_only:
            log.warning("RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
        else:
            log.warning("LIVE — real orders will be sent (use --record-only "
                        "for credential-less data collection)")
            await self._reconcile_positions(hedge=False, strict=True)
            log.info("starting positions: %s (net %+.6g)",
                     " ".join(f"{v.name}={v.position:+.6g}"
                              for v in self.venues.values()),
                     sum(v.position for v in self.venues.values()))

        tasks: List[asyncio.Task] = []
        for v in self.venues.values():
            tasks += v.start_tasks(self.stop, self._update_evt.set, live)
        if cfg.recorder_enabled or self.record_only:
            self.recorder = MinuteRecorder(cfg.recorder_csv, self.entropy.book,
                                           self.hedge.book, cfg.staleness_sec,
                                           max_spread_bps=cfg.recorder_max_spread_bps)
            tasks.append(asyncio.create_task(self.recorder.run(self.stop),
                                             name="recorder"))
        if not self.record_only:
            if cfg.maker.enabled:
                # mutually exclusive with the taker band strategy (design §12)
                self._setup_maker_roles()
                tasks.append(asyncio.create_task(self._maker_loop(),
                                                 name="maker"))
                tasks.append(asyncio.create_task(self._maker_hedge_loop(),
                                                 name="maker-hedge"))
            else:
                tasks.append(asyncio.create_task(self._strategy_loop(),
                                                 name="strategy"))
            tasks.append(asyncio.create_task(self._balance_loop(),
                                             name="balances"))
            tasks.append(asyncio.create_task(self._http_keepalive_loop(),
                                             name="keepalive"))
        tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        if live:
            tasks.append(asyncio.create_task(self._reconcile_loop(),
                                             name="reconcile"))

        await self.stop.wait()
        if self._exec_tasks:  # let in-flight executions settle, never cancel
            log.info("waiting for %d in-flight execution(s) to settle",
                     len(self._exec_tasks))
            await asyncio.wait(self._exec_tasks,
                               timeout=cfg.settle_timeout_sec + 2.0)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in self.venues.values():
            await v.close()
        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)

    # --------------------------------------------------------------- signals

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(abs(v.position) * ref / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the reverse premium must clear lower - midline."""
        if sell.key == "entropy":
            base = self.cfg.midline_bps + self.cfg.upper_bps
        else:
            base = self.cfg.lower_bps - self.cfg.midline_bps
        return base + self._inv_add_bps(buy, sell)

    def _headroom(self, buy, sell, ref_px: float) -> float:
        hb = buy.cap_usd - buy.position * ref_px
        hs = sell.cap_usd + sell.position * ref_px
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float):
        plan, reason = plan_arb(
            buy.book, sell.book,
            threshold_bps=self._eff_threshold(buy, sell),
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.cfg.take_fraction,
            cap_notional=cap_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
        )
        if (plan is not None and self.cfg.max_signal_edge_bps > 0.0
                and plan.marginal_premium_bps > self.cfg.max_signal_edge_bps):
            # a dislocation this far beyond the band is almost always a
            # phantom top-of-book on the thin entropy book: the entry leg
            # never fills while the hedge leg does, and the forced round
            # trip bleeds the spread (sndk-rh 2026-09-21/22: 3/3 losses)
            return None, "phantom_edge"
        return plan, reason

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        while not self.stop.is_set():
            await self._update_evt.wait()
            self._update_evt.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.time()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted:
            return
        now = time.time()
        if now - self.last_trade_ts < cfg.cooldown_sec:
            self._schedule_poke(cfg.cooldown_sec - (now - self.last_trade_ts))
            return
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(self._execute_locked(buy, sell, plan))
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)

    async def _execute_locked(self, buy, sell, plan: ArbPlan) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        unresolved = False
        try:
            unresolved = await self._execute(buy, sell, plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("execute failed")
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if unresolved:
            self._reconcile_evt.set()
        else:
            await self._maybe_hedge()
        self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        best = None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            if not (buy.book.is_fresh(cfg.staleness_sec)
                    and sell.book.is_fresh(cfg.staleness_sec)):
                continue
            if not (buy.ready_to_trade() and sell.ready_to_trade()):
                continue
            if self._venue_down:
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if self._venue_limited(buy) or self._venue_limited(sell):
                continue  # reactive 429 exclusion
            if not (self._venue_rate_ok(buy) and self._venue_rate_ok(sell)):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                continue
            plan, reason = self._plan(buy, sell, cfg.max_order_notional)
            edge_present = reason not in ("no_edge", "empty_book")
            if reason == "phantom_edge":
                self._skiplog("%s deferred: edge beyond phantom cap "
                              "(max_signal_edge_bps)", dkey)
            if not edge_present:
                self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                continue
            headroom = self._headroom(buy, sell, plan.buy_limit)
            if headroom < plan.buy_notional:
                plan, _ = self._plan(buy, sell,
                                     min(cfg.max_order_notional, headroom))
                if plan is None:
                    self._skiplog("%s blocked by position caps (headroom $%.0f)",
                                  dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    # ------------------------------------------------------------- execution

    async def _execute(self, buy, sell, plan: ArbPlan) -> bool:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns True when an outcome is unresolved and the caller
        must escalate to reconcile."""
        if self.halted:
            return False
        cfg = self.cfg
        inv_bps = self._inv_add_bps(buy, sell)
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        self.last_trade_ts = time.time()
        log.info("[ARB] %s: BUY %s %.6g @<=%.6g | SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | prem %.2fbps | exp $%.4f",
                 direction, buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.marginal_premium_bps, plan.exp_edge_usd)
        slip = cfg.leg_slippage_bps / 1e4
        buy_bound = buy.px_round(plan.buy_limit * (1 + slip), round_up=False)
        sell_bound = sell.px_round(plan.sell_limit * (1 - slip), round_up=True)
        self._record_send(buy)
        self._record_send(sell)
        res = await asyncio.gather(
            buy.send_taker(is_buy=True, qty=plan.qty, limit_px=buy_bound),
            sell.send_taker(is_buy=False, qty=plan.qty, limit_px=sell_bound),
            return_exceptions=True)
        binfo, sinfo = (r if isinstance(r, dict) else
                        {"status": "send-failed", "filled_base": 0.0,
                         "avg_px": None, "err": repr(r), "unresolved": False}
                        for r in res)
        for v, info, side in ((buy, binfo, "buy"), (sell, sinfo, "sell")):
            if info.get("err"):
                log.error("[%s] %s leg: %s", v.name, side, info["err"])
        bfill = binfo["filled_base"]
        sfill = sinfo["filled_base"]
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.get("avg_px") or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
        if sfill:
            spx = sinfo.get("avg_px") or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx

        matched = min(bfill, sfill)
        if matched > 0:
            # a clean two-leg fill proves margin is available again
            self._margin_backoff.pop(buy.key, None)
            self._margin_backoff.pop(sell.key, None)
        fill_edge = 0.0
        if matched > 0 and binfo.get("avg_px") and sinfo.get("avg_px"):
            fill_edge = matched * (sinfo["avg_px"] * (1 - plan.sell_fee)
                                   - binfo["avg_px"] * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo["status"], bfill, plan.qty,
                 sell.name, sinfo["status"], sfill, plan.qty, matched, fill_edge)
        buy.last_traded_ts = sell.last_traded_ts = time.time()

        unresolved = binfo.get("unresolved") or sinfo.get("unresolved")
        hard_err = (binfo.get("err") is not None
                    or sinfo.get("err") is not None)
        rate_limited = False
        for v, info in ((buy, binfo), (sell, sinfo)):
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                rate_limited = True
                self._mark_limited(v)
            elif "margin" in str(info.get("status", "")).lower():
                pause = self._margin_pause(v.key)
                self._venue_limited_until[v.key] = time.time() + pause
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue %.0fs (backoff)", v.name, pause)
        sent_ok = not hard_err and not unresolved
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                self.halted = True
                log.critical("HALTED after %d consecutive execution problems "
                             "— flatten manually and restart / 连续执行异常，"
                             "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(direction, plan,
                           None if unresolved else fill_edge,
                           f"{binfo['status']}/{sinfo['status']}", sent_ok)
        self._log_csv(direction, buy, sell, plan, sent_ok, bfill, sfill,
                      binfo["status"], sinfo["status"], fill_edge, inv_bps)
        self.last_trade_ts = time.time()
        return bool(unresolved)

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool) -> None:
        self.recent_trades.append({
            "ts": time.time(), "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.marginal_premium_bps,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok})

    async def _maybe_hedge(self) -> None:
        net = sum(v.position for v in self.venues.values())
        if abs(net) > self.cfg.net_tolerance_base:
            await self._hedge(net)

    async def _hedge(self, net: float) -> None:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down \
                    or not v.book.is_fresh(cfg.staleness_sec):
                continue  # unreachable or blind: cannot hedge here
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            qty = floor_step(min(abs(net), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            if qty * limit < max(cfg.min_order_notional, v.min_quote):
                continue
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            net, "SELL" if is_sell else "BUY", qty, v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                          limit_px=limit, reduce_only=True)
                if info.get("err") or info.get("unresolved"):
                    log.error("[HEDGE] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                else:
                    fill = info["filled_base"]
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.get("avg_px") or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info["status"], fill, qty)
                v.last_traded_ts = time.time()
            finally:
                lk.release()
            return
        log.warning("[HEDGE] net %+.6g below hedgeable minimum — carrying "
                    "(next reconcile retries)", net)

    # ------------------------------------------------------------ maker mode

    # a just-placed quote may not appear in the venue's open-order view until
    # its stream catches up; within this grace we never treat it as vanished
    MAKER_QUOTE_GRACE_SEC = 3.0

    MAKER_TRADES_HEADER = ["ts", "hedge_dir", "hedge_qty", "maker_px",
                           "hedge_px", "gross_edge_bps", "net_edge_bps",
                           "fill_to_hedge_ms", "n_fills", "failures"]
    MAKER_SELECTION_HEADER = ["ts", "side", "qty", "prem_fill_bps",
                              "prem_1s_bps", "prem_10s_bps"]

    def _setup_maker_roles(self) -> None:
        """maker role falls on the hedge venue (--hedge katana → Katana
        quotes); the base leg is the taker hedge. Startup fails loudly when
        the selected venue cannot act as maker — never mid-session."""
        mk, hg = self.hedge, self.entropy
        if not getattr(mk, "maker_capable", False):
            raise RuntimeError(
                f"[{mk.name}] does not implement the maker contract — maker "
                f"mode needs a maker_capable hedge venue (e.g. --hedge "
                f"katana) / maker 模式要求对冲腿支持挂单契约")
        self.maker, self.taker_hedge = mk, hg
        mk.maker_mode = True
        mk.on_fill(self._on_maker_fill)
        log.warning("[MAKER] mode on — quoting %s on %s, hedging fills on %s "
                    "(edge %.2fbps + costs %.2fbps, batch %dms); taker band "
                    "strategy disabled", mk.conf.symbol, mk.name, hg.name,
                    self.cfg.maker.edge_bps, self.cfg.maker.costs_bps,
                    self.cfg.maker.hedge_batch_ms)

    def _maker_safety_block(self):
        """Reason the quote loop must stand down right now, or None.

        Transitions into a blocked state clear every resting quote once; the
        design invariant is that a blind or failing hedge leg never leaves
        our quotes resting on the thin venue."""
        cfg = self.cfg
        mk, hg = self.maker, self.taker_hedge
        if self.halted or self._mk_exposed:
            return "halted"
        if self._venue_down:
            return "venue_down"
        if not (mk.book.is_fresh(cfg.staleness_sec)
                and hg.book.is_fresh(cfg.staleness_sec)):
            return "book_stale"
        if not mk.ready_to_trade():
            return "maker_stream_down"
        if not hg.ready_to_trade():
            return "hedge_not_ready"
        if self._venue_limited(mk) or self._venue_limited(hg):
            return "rate_limited"
        return None

    async def _maker_clear_quotes(self, reason: str) -> None:
        """Make every resting quote disappear.

        Market-wide cancel: one atomic request that does not depend on
        knowing live order ids — exactly what a P0 stand-down needs."""
        self._mk_quotes = {}
        try:
            self._record_send(self.maker)
            r = await self.maker.cancel_orders()
            if r.get("err"):
                log.error("[MAKER] cancel-all failed (%s): %s", reason,
                          r["err"])
            else:
                log.warning("[MAKER] all quotes cleared (%s)", reason)
        except Exception:
            log.exception("[MAKER] cancel-all failed (%s)", reason)

    async def _maker_loop(self) -> None:
        cfg = self.cfg.maker
        sides = ("bid", "ask") if cfg.sides == "both" else (cfg.sides,)
        try:
            while not self.stop.is_set():
                try:
                    await self._maker_tick(sides)
                    self._maker_selection_step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("maker tick failed")
                try:
                    await asyncio.wait_for(self.stop.wait(),
                                           timeout=cfg.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            # never leave resting quotes behind on shutdown
            try:
                if self._mk_quotes:
                    await self._maker_clear_quotes("shutdown")
            except Exception:
                log.exception("[MAKER] shutdown cancel failed")

    async def _maker_tick(self, sides) -> None:
        cfg = self.cfg.maker
        mk, hg = self.maker, self.taker_hedge
        now = time.time()
        block = self._maker_safety_block()
        if block is not None:
            if self._mk_blocked_reason != block:
                log.warning("[MAKER] quoting blocked: %s — clearing quotes",
                            block)
                self._mk_blocked_reason = block
                self._mk_last_clear_ts = now
                async with self._vlock(mk.key):
                    await self._maker_clear_quotes(block)
            elif now - self._mk_last_clear_ts >= 10.0:
                # safety re-assert while blocked: if an earlier clear failed
                # (rate limit, blip) the resting quotes must still not survive
                self._mk_last_clear_ts = now
                async with self._vlock(mk.key):
                    await self._maker_clear_quotes(f"{block} (re-assert)")
            return
        if self._mk_blocked_reason is not None:
            log.info("[MAKER] quoting resumed (was blocked: %s)",
                     self._mk_blocked_reason)
            self._mk_blocked_reason = None
        # anchors: the taker hedge venue's EXECUTABLE prices drive everything
        hbid, hask = hg.book.best_bid(), hg.book.best_ask()
        mid = mk.book.mid()
        if not (hbid and hask and mid):
            return
        skew = inventory_skew_bps(mk.position, mid, mk.cap_usd,
                                  self.cfg.inventory_scale_bps,
                                  self.cfg.inventory_floor_frac)
        bid_px, ask_px = quote_prices(hbid, hask, cfg.costs_bps,
                                      cfg.edge_bps, skew)
        live = mk.open_orders()
        anchors = {"bid": (bid_px, hbid), "ask": (ask_px, hask)}
        for side in sides:
            await self._maker_tick_side(side, anchors[side], live, skew, now)

    async def _maker_tick_side(self, side, anchor_pair, live, skew,
                               now: float) -> None:
        cfg = self.cfg.maker
        mk, hg = self.maker, self.taker_hedge
        px, anchor = anchor_pair
        if not px:
            return
        is_bid = side == "bid"
        # headroom across BOTH venues: a maker fill must stay hedgeable
        # within the taker venue's cap, and the quote within the maker cap
        if is_bid:
            head = self._headroom(mk, hg, px)
        else:
            head = self._headroom(hg, mk, px)
        qty = floor_step(min(cfg.size_base,
                             max(head, 0.0) / px), self._step)
        desired = (head > 0 and qty >= mk.min_base
                   and qty * px >= self._min_notional)
        q = self._mk_quotes.get(side)
        if q is not None and q.order_id in live:
            o = live[q.order_id]
            remaining = max(o.get("qty", 0.0) - o.get("executed", 0.0), 0.0)
            reason = requote_reason(
                remaining=remaining, size=q.qty,
                anchor_now=anchor, anchor_quoted=q.anchor,
                requote_bps=cfg.requote_bps,
                age_sec=now - q.placed_ts, requote_sec=cfg.requote_sec,
                skew_now=skew, skew_quoted=q.skew_bps)
            if reason is None:
                return
            place = desired
            if not desired:
                reason = "no_headroom"
            await self._maker_replace(side, q, reason, px, qty, place,
                                      anchor, skew, now)
            return
        if q is not None:
            if now - q.placed_ts < self.MAKER_QUOTE_GRACE_SEC:
                return      # the private stream may just be behind us
            # vanished without our cancel: fully filled (already hedged via
            # the fill path) or force-canceled by the venue — requote
            log.info("[MAKER] %s quote no longer resting — requoting", side)
            self._mk_quotes.pop(side, None)
        if not desired:
            return
        if not self._venue_rate_ok(mk):
            self._skiplog("[MAKER] %s deferred: order budget exhausted", side)
            return
        await self._place_maker_quote(side, is_bid, px, qty, anchor, skew, now)

    async def _place_maker_quote(self, side, is_bid, px, qty, anchor, skew,
                                 now: float) -> None:
        mk = self.maker
        await self._vlock(mk.key).acquire()
        try:
            self._record_send(mk)
            r = await mk.place_maker(is_buy=is_bid, qty=qty, limit_px=px)
        finally:
            self._vlock(mk.key).release()
        if r.get("err") is not None:
            log.warning("[MAKER] %s place rejected: %s", side, r["err"])
            return
        if r.get("took_liquidity"):
            # post-only must never take; if this fires the fill is also
            # arriving via the private stream — surface it loudly
            log.warning("[MAKER] %s quote reports executed qty at place — "
                        "check the stream", side)
        if r.get("status") == "open" and r.get("order_id"):
            self._mk_quotes[side] = MakerQuote(
                side=side, order_id=r["order_id"], px=px, qty=qty,
                anchor=anchor, placed_ts=now, skew_bps=skew)
            log.info("[MAKER] %s %s %.6g @ %.6g (anchor %.6g, skew %.2fbps)",
                     side, "BUY" if is_bid else "SELL", qty, px, anchor, skew)

    async def _maker_replace(self, side, q: "MakerQuote", reason: str,
                             px: float, qty: float, place: bool,
                             anchor, skew, now: float) -> None:
        """Cancel one resting quote and immediately place its replacement
        (keeps the book continuously two-sided).

        If the cancel fails the old order may still be resting — we drop our
        tracking and let the next tick's open_orders check self-heal instead
        of risking a duplicate quote."""
        mk = self.maker
        self._mk_quotes.pop(side, None)
        if not self._venue_rate_ok(mk):
            self._skiplog("[MAKER] %s requote (%s) deferred: budget exhausted",
                          side, reason)
            return
        await self._vlock(mk.key).acquire()
        try:
            self._record_send(mk)
            r = await mk.cancel_orders(order_ids=[q.order_id])
        finally:
            self._vlock(mk.key).release()
        if r.get("err"):
            log.warning("[MAKER] %s cancel failed (%s): %s", side, reason,
                        r["err"])
            return
        log.info("[MAKER] %s requoted: %s", side, reason)
        if place:
            await self._place_maker_quote(side, is_bid=(side == "bid"),
                                          px=px, qty=qty, anchor=anchor,
                                          skew=skew, now=now)

    # ------------------------------------------------- maker fill → hedge

    def _on_maker_fill(self, ev: FillEvent) -> None:
        """Private-stream fill: update maker accounting, queue the hedge
        delta, record the adverse-selection sample.

        Runs on the feed's task — synchronous and fast on purpose; hedging
        happens in its own task behind the batch window."""
        mk = self.maker
        buy = ev.side == "buy"
        q = max(ev.qty_delta, 0.0)
        if q <= 0:
            return
        fee = mk.fee_bps / 1e4
        mk.position += q if buy else -q
        if buy:
            mk.cash -= q * ev.px * (1.0 + fee)
        else:
            mk.cash += q * ev.px * (1.0 - fee)
        mk.volume_usd += q * ev.px
        mk.last_traded_ts = time.time()
        self._mk_fills += 1
        # the hedge target lives on the taker venue: a maker BUY needs a
        # hedge SELL there, and vice versa
        self._mk_pending += -q if buy else q
        if self._mk_pending_first_ts is None:
            self._mk_pending_first_ts = time.time()
        if self._mk_batch_first_fill_ts is None:
            self._mk_batch_first_fill_ts = ev.ts or time.time()
        (self._mk_fills_buy if buy else self._mk_fills_sell).append((q, ev.px))
        self._mk_hedge_evt.set()
        prem = self.premium_bps()
        if prem is not None:
            self._mk_selection.append(
                {"ts": time.time(), "side": ev.side, "qty": q,
                 "prem_fill_bps": round(prem, 3),
                 "prem_1s_bps": None, "prem_10s_bps": None})

    async def _maker_hedge_loop(self) -> None:
        while not self.stop.is_set():
            await self._mk_hedge_evt.wait()
            self._mk_hedge_evt.clear()
            if self.stop.is_set() or self._mk_exposed or self.halted:
                continue
            try:
                await self._maker_hedge_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maker hedge cycle failed")

    async def _maker_hedge_cycle(self) -> None:
        """One drain of the pending hedge queue: wait out the batch window,
        hedge the net, retry with backoff, halt when failures are exhaustive.
        Exposed as its own step so tests can drive it deterministically."""
        cfg = self.cfg.maker
        first = self._mk_pending_first_ts
        if first is not None:
            delay = cfg.hedge_batch_ms / 1000.0 - (time.time() - first)
            if delay > 0:
                await asyncio.sleep(delay)
        while not self.stop.is_set():
            first = self._mk_pending_first_ts
            if first is None:
                break
            delay = cfg.hedge_batch_ms / 1000.0 - (time.time() - first)
            if delay > 0:
                await asyncio.sleep(delay)
            qty = self._mk_pending
            self._mk_pending = 0.0
            self._mk_pending_first_ts = None
            if abs(qty) <= 1e-12:
                continue
            ok = await self._hedge_delta(qty)
            if ok:
                self._mk_hedge_failures = 0
                continue
            # failure: re-queue and back off; exhaustive failures halt
            self._mk_pending += qty
            self._mk_pending_first_ts = time.time()
            self._mk_hedge_failures += 1
            if self._mk_hedge_failures >= cfg.max_hedge_failures:
                self._mk_exposed = True
                self.halted = True
                log.critical(
                    "[MAKER] EXPOSED — hedging failed %d times, %+.6g "
                    "unhedged; quotes cleared, flatten manually and restart / "
                    "对冲连续失败，已停止报价，请手动处理敞口后重启",
                    self._mk_hedge_failures, self._mk_pending)
                await self._maker_clear_quotes("exposed")
                break
            await asyncio.sleep(cfg.hedge_retry_sec)

    async def _hedge_delta(self, qty: float) -> bool:
        """Hedge a signed base quantity on the taker hedge venue
        (+ = buy, − = sell) with an IOC taker and slippage protection.

        Returns True when this quantity is fully handled (hedged, or parked
        as dust awaiting more fills) and False to signal a retryable
        failure. Accounting mirrors the taker path so MTM/reconcile stay
        honest."""
        hg = self.taker_hedge
        is_buy = qty > 0
        q = floor_step(abs(qty), self._step)
        if q <= 0:
            # below one step: unhedgeable for now — park it so it grows
            # until a real hedge is possible (never silently drop exposure)
            self._mk_pending += qty
            self._mk_pending_first_ts = None
            return True
        ref = hg.book.best_ask() if is_buy else hg.book.best_bid()
        if ref is None:
            return False                      # blind: retry when books return
        # the taker venue's cap binds the hedge — exceeding it is exposure
        head = (hg.cap_usd - hg.position * ref if is_buy
                else hg.cap_usd + hg.position * ref)
        q = min(q, max(head, 0.0) / ref)
        q = floor_step(q, self._step)
        if q <= 0:
            return False                      # no headroom: exposure path
        slip = self.cfg.hedge_slippage_bps / 1e4
        limit = hg.px_round(ref * (1.0 + slip) if is_buy
                            else ref * (1.0 - slip), round_up=is_buy)
        if q * limit < max(self.cfg.min_order_notional, hg.min_quote):
            # dust: park it back — it grows until a real hedge is possible
            self._mk_pending += qty
            self._mk_pending_first_ts = None
            return True
        lk = self._vlock(hg.key)
        await lk.acquire()
        try:
            if self._venue_limited(hg) or hg.key in self._venue_down:
                return False
            self._record_send(hg)
            info = await hg.send_taker(is_buy=is_buy, qty=q, limit_px=limit)
        finally:
            lk.release()
        if info.get("err") or info.get("unresolved"):
            log.error("[MAKER-HEDGE] %s %.6g: %s", "BUY" if is_buy else "SELL",
                      q, info.get("err") or "unresolved")
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                self._mark_limited(hg)
            return False
        fill = info["filled_base"]
        px = info.get("avg_px") or limit
        fee = hg.fee_bps / 1e4
        hg.position += fill if is_buy else -fill
        if is_buy:
            hg.cash -= fill * px * (1.0 + fee)
        else:
            hg.cash += fill * px * (1.0 - fee)
        hg.volume_usd += fill * px
        hg.last_traded_ts = time.time()
        self._mk_hedges += 1
        if fill < q - 1e-12:
            # partial hedge: the remainder stays queued for the next cycle
            self._mk_pending += qty / abs(qty) * (q - fill)
            self._mk_pending_first_ts = time.time()
            self._mk_hedge_evt.set()
        self._maker_log_batch(is_buy, fill, px)
        return True

    def _drain_maker_px(self, is_buy):
        """Qty-weighted average maker fill price for this direction since the
        last hedge of that direction (approximate attribution: maker fills
        and hedge sends interleave). None when no fills are queued."""
        dq = self._mk_fills_buy if is_buy else self._mk_fills_sell
        if not dq:
            return None
        qty = sum(q for q, _ in dq)
        if qty <= 0:
            dq.clear()
            return None
        px = sum(q * p for q, p in dq) / qty
        dq.clear()
        return px

    def _maker_log_batch(self, is_buy: bool, hedge_qty: float,
                         hedge_px: float) -> None:
        now = time.time()
        f2h = (int((now - self._mk_batch_first_fill_ts) * 1000)
               if self._mk_batch_first_fill_ts is not None else None)
        self._mk_batch_first_fill_ts = None
        maker_px = self._drain_maker_px(not is_buy)
        gross = net = None
        if maker_px and hedge_px:
            # hedge SELL  → we had bought the maker venue: hedge_px over maker_px
            # hedge BUY   → we had sold the maker venue: maker_px over hedge_px
            ratio = hedge_px / maker_px if not is_buy else maker_px / hedge_px
            gross = (ratio - 1.0) * 1e4
            net = gross - self.cfg.maker.costs_bps
        self._maker_csv(self.cfg.maker.trades_csv, self.MAKER_TRADES_HEADER,
                        [f"{now:.3f}", "BUY" if is_buy else "SELL",
                         f"{hedge_qty:.8g}",
                         f"{maker_px:.10g}" if maker_px else "",
                         f"{hedge_px:.10g}",
                         f"{gross:.3f}" if gross is not None else "",
                         f"{net:.3f}" if net is not None else "",
                         f"{f2h}" if f2h is not None else "",
                         self._mk_batch_fills, self._mk_hedge_failures])
        self._mk_batch_fills = 0

    def _maker_selection_step(self) -> None:
        """Capture the premium at fill+1s / fill+10s — the direct measure of
        adverse selection (did the market move against us after we got
        filled?). Rows land in the selection CSV once complete."""
        now = time.time()
        keep = []
        for s in self._mk_selection:
            if s["prem_1s_bps"] is None and now - s["ts"] >= 1.0:
                p = self.premium_bps()
                s["prem_1s_bps"] = round(p, 3) if p is not None else ""
            if s["prem_10s_bps"] is None and now - s["ts"] >= 10.0:
                p = self.premium_bps()
                s["prem_10s_bps"] = round(p, 3) if p is not None else ""
            if s["prem_1s_bps"] != "" and s["prem_10s_bps"] != "" and \
                    s["prem_1s_bps"] is not None and \
                    s["prem_10s_bps"] is not None:
                self._maker_csv(self.cfg.maker.selection_csv,
                                self.MAKER_SELECTION_HEADER,
                                [f"{s['ts']:.3f}", s["side"],
                                 f"{s['qty']:.8g}", s["prem_fill_bps"],
                                 s["prem_1s_bps"], s["prem_10s_bps"]])
            else:
                keep.append(s)
        self._mk_selection = keep[-256:]

    def _maker_csv(self, path: str, header: list, row: list) -> None:
        try:
            import csv as _csv
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as fh:
                w = _csv.writer(fh)
                if new:
                    w.writerow(header)
                w.writerow(row)
        except Exception:
            log.exception("maker csv write failed")


    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        now = time.time()
        vs = []
        for v in self.venues.values():
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                continue  # just traded: chain read would be stale
            if v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if not vs:
            return
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        if hedge:
            await self._maybe_hedge()

    async def _reconcile_venue(self, v, strict: bool) -> None:
        async with self._vlock(v.key):
            now = time.time()
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                return  # traded while waiting for the lock
            try:
                r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                 "it recovers", v.name, n,
                                 self.cfg.venue_probe_sec)
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                return
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                            "trading RESUMED", v.name,
                            now - self._venue_down.pop(v.key))
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            total += v.equity - v.start_equity
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m is None:
                return None
            total += v.cash + v.position * m
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        return (em / hm - 1.0) * 1e4

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                + ("" if v.book.is_fresh(cfg.staleness_sec) else " STALE")
                + (" RATE-LTD" if self._venue_limited(v) else "")
                + (" DOWN" if v.key in self._venue_down else "")
                for v in self.venues.values())
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            mk = ""
            if self.maker is not None:
                mk = (f" | mk {len(self.maker.open_orders())}q "
                      f"pend {self._mk_pending:+.6g} "
                      f"fills {self._mk_fills} hedges {self._mk_hedges}"
                      + (" EXPOSED" if self._mk_exposed else "")
                      + (f" blocked:{self._mk_blocked_reason}"
                         if self._mk_blocked_reason else ""))
            log.info("[status] %s | prem %s bps (band %+.2f..%+.2f) | pos %s "
                     "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                     "fillEdge $%.4f%s%s%s",
                     books, prem_s, cfg.midline_bps - cfg.lower_bps,
                     cfg.midline_bps + cfg.upper_bps, pos, net, self.trades,
                     self.hedges,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self.total_exp_edge, self.total_fill_edge, rec,
                     " *** HALTED ***" if self.halted else "", mk)

    def _log_csv(self, direction, buy, sell, plan: ArbPlan, ok: bool, bfill,
                 sfill, bstatus, sstatus, fill_edge, inv_bps) -> None:
        try:
            path = self.cfg.trades_csv
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path):
                with open(path) as fh0:
                    if fh0.readline().strip() != ",".join(CSV_HEADER):
                        os.replace(path, path + ".old")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(CSV_HEADER)
                w.writerow([f"{time.time():.3f}",
                            direction, buy.name, sell.name, f"{plan.qty:.8g}",
                            plan.buy_limit, plan.sell_limit,
                            f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
                            f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
                            f"{plan.marginal_premium_bps:.3f}",
                            f"{self.cfg.midline_bps:.3f}",
                            f"{inv_bps:.3f}", int(ok), f"{bfill:.8g}",
                            f"{sfill:.8g}", bstatus, sstatus, f"{fill_edge:.4f}"])
        except Exception:
            log.exception("csv write failed")

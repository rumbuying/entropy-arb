#!/usr/bin/env python3
"""entropy-arb entry point.

    # collect minute data only — no strategy, no credentials needed
    python3 main.py --record-only --symbol SNDK --hedge lighter-rh

    # LIVE trading: real orders, real money (needs .env credentials)
    python3 main.py --symbol SNDK --hedge lighter-rh

--symbol and --hedge are required on every start: the markets you trade are
an explicit decision, not a config default. Add --cn for a Chinese-language
dashboard. There is no paper mode. Collect data with --record-only, set
your thresholds with tools/analyze.py, then go live with small position
caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from entropy_arb.config import HEDGE_VENUES, ConfigError, load_config
from entropy_arb.engine import Engine

log = logging.getLogger("main")


def setup_logging(level: str, log_file: str = None,
                  extra_handler: logging.Handler = None,
                  stdout: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file)
        h.setFormatter(fmt)
        root.addHandler(h)
    if stdout or not log_file:
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                log_buffer, lang: str, web_on: bool = False,
                web_port: int = 0, config_path: str = "") -> None:
    eng = Engine(cfg, record_only=record_only, config_path=config_path)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    web_srv = None
    if web_on:
        from entropy_arb.web import WebServer
        web_srv = WebServer(eng, cfg.web_host, web_port,
                            log_buffer=log_buffer)
        try:
            port = await web_srv.start()
            log.info("web overview: http://%s:%d", cfg.web_host, port)
        except OSError as e:
            web_srv = None
            log.error("web ui failed to start on %s:%d: %r — continuing "
                      "without it", cfg.web_host, web_port, e)
    if not use_dashboard:
        try:
            await eng.run()
        finally:
            if web_srv:
                await web_srv.stop()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    try:
        await eng.run()
    finally:
        eng.request_stop()
        if web_srv:
            await web_srv.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dash_task, timeout=5)
        if not dash_task.done():
            dash_task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: Entropy vs Lighter mainnet / "
                    "Lighter Robinhood / trade.xyz. Without --record-only, "
                    "real orders are sent.")
    p.add_argument("--symbol", required=True,
                   help="symbol traded on both venues, e.g. SNDK / "
                        "两个交易所共同交易的品种")
    p.add_argument("--hedge", required=True, choices=HEDGE_VENUES,
                   metavar="VENUE",
                   help=f"hedge venue, one of: {', '.join(HEDGE_VENUES)} / "
                        f"对冲腿，三选一")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    web = p.add_mutually_exclusive_group()
    web.add_argument("--web", nargs="?", const="", metavar="PORT",
                     help="serve the read-only web overview "
                          "(default port: web.port from config.yaml, 8787)")
    web.add_argument("--no-web", action="store_true",
                     help="disable the web overview even if web.enabled")
    p.add_argument("--log-stdout", action="store_true",
                   help="also write logs to stdout when a log file is set "
                        "(used by the console supervisor)")
    args = p.parse_args()

    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, hedge_venue=args.hedge)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    web_on = (args.web is not None or cfg.web_enabled) and not args.no_web
    try:
        web_port = int(args.web) if args.web else cfg.web_port
    except ValueError:
        print(f"invalid --web port: {args.web!r}", file=sys.stderr)
        sys.exit(2)

    log_buffer = None
    if use_dashboard or web_on:
        from entropy_arb.logbuf import BufferLogHandler
        log_buffer = BufferLogHandler()
    if log_buffer:
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer, stdout=args.log_stdout)
    else:
        setup_logging(cfg.log_level, stdout=args.log_stdout)

    try:
        asyncio.run(amain(cfg, record_only=args.record_only,
                          use_dashboard=use_dashboard, force_tty=force_tty,
                          log_buffer=log_buffer,
                          lang="zh" if args.cn else "en",
                          web_on=web_on, web_port=web_port,
                          config_path=args.config))
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

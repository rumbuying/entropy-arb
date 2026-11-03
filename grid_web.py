"""Serve the independent grid website on http://127.0.0.1:8789.

Start grid_main.py separately with the same --socket. This entry point has
no legacy console, profile, worker, database, or .env initialization.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aiohttp import web

from entropy_arb.console.grid_web import create_grid_web_app

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--socket", default=str(ROOT / "data/run/grid-runtime.sock"))
    parser.add_argument('--workers-config', help='trusted JSON mapping venue keys to absolute isolated runtime socket paths')
    args = parser.parse_args(argv)
    workers = json.loads(Path(args.workers_config).read_text()) if args.workers_config else None
    app = create_grid_web_app(socket_path=str(Path(args.socket).absolute()), runtime_sockets=workers)
    # Never log URLs or request bodies from the credential configuration UI.
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()

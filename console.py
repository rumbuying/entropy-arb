#!/usr/bin/env python3
"""entropy-arb console: browser control room for engine workers.

    python3 console.py                        # http://127.0.0.1:8788
    python3 console.py --host 0.0.0.0 --port 9000 --token mysecret

The console manages:
  * profiles   — named strategy configs (validated through load_config)
  * secrets    — .env keys (masked, audited, format-checked)
  * workers    — engine subprocesses it starts/stops/restarts
  * analytics  — premium analyzer + band backtest + minute history

Binding notes: 127.0.0.1 needs no token (same trust model as the .env file
itself). Any non-loopback host requires a token — pass --token or one is
generated, printed here and stored in logs/console-token (0600).
"""
import argparse
import asyncio
import logging
import os
import secrets as pysecrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web  # noqa: E402

from entropy_arb.console.profiles import ProfilesManager  # noqa: E402
from entropy_arb.console.secrets import SecretsManager, audit_writer  # noqa: E402
from entropy_arb.console.server import create_app  # noqa: E402
from entropy_arb.console.supervisor import Supervisor  # noqa: E402


def setup_logging(log_file: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    d = os.path.dirname(log_file)
    if d:
        os.makedirs(d, exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def resolve_token(args) -> str:
    if args.host in ("127.0.0.1", "localhost", "::1"):
        return args.token or ""
    if args.token:
        return args.token
    tok_path = os.path.join("logs", "console-token")
    try:
        with open(tok_path) as fh:
            tok = fh.read().strip()
        if tok:
            print(f"console token (existing, {tok_path}): {tok}", flush=True)
            return tok
    except FileNotFoundError:
        pass
    tok = pysecrets.token_urlsafe(24)
    os.makedirs("logs", exist_ok=True)
    with open(tok_path, "w") as fh:
        fh.write(tok)
    os.chmod(tok_path, 0o600)
    print(f"non-loopback host requires a token — generated one, saved to "
          f"{tok_path}: {tok}", flush=True)
    return tok


async def amain(args) -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    audit = audit_writer("logs/console.log")
    supervisor = Supervisor(root, args.profiles_dir,
                            port_range=(args.base_port, args.base_port + 198))
    profiles = ProfilesManager(args.profiles_dir, args.env_file, audit_log=audit)
    secrets = SecretsManager(args.env_file, audit_log=audit)
    token = resolve_token(args)
    app = create_app(supervisor, profiles, secrets, token=token)
    try:
        from entropy_arb.console.analytics import register_analytics
        register_analytics(app, profiles)
    except ImportError:
        logging.getLogger("console").warning("analytics module unavailable")
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    url = f"http://{args.host}:{args.port}"
    if token:
        url += f"/?token={token}"
    print(f"entropy-arb console → {url}", flush=True)
    if token:
        print("(keep the token private — it grants full console access)",
          flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await supervisor.shutdown()
        await runner.cleanup()


def main() -> None:
    p = argparse.ArgumentParser(
        description="entropy-arb web console (profiles / keys / workers)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--profiles-dir", default="profiles")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--base-port", type=int, default=8801,
                   help="first port used for worker state servers")
    p.add_argument("--token", default="",
                   help="API token; required (or auto-generated) for "
                        "non-loopback hosts")
    args = p.parse_args()
    setup_logging("logs/console-server.log")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

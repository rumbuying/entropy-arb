"""WebServer: read-only API, websocket push, no order-path interference.

Run:  python3 -m pytest tests/test_web.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_state import make_engine, make_cfg  # noqa: E402,F401  (reuse stubs)
from test_state import StubVenue  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.web import WebServer  # noqa: E402


async def _session():
    return aiohttp.ClientSession()


def test_web_server_state_ws_health():
    async def run():
        eng = make_engine()
        eng.entropy.set_book(100.14, 100.16)
        eng.hedge.set_book(99.99, 100.01)
        srv = WebServer(eng, "127.0.0.1", 0)   # ephemeral port
        port = await srv.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                        f"http://127.0.0.1:{port}/api/state") as r:
                    assert r.status == 200
                    snap = await r.json()
                assert snap["status"] == "running"
                assert snap["venues"]["entropy"]["bid"] == 100.14
                assert snap["signal"]["band_high_bps"] == 6.0

                async with http.get(
                        f"http://127.0.0.1:{port}/api/health") as r:
                    assert (await r.json())["ok"] is True

                # index page + a static asset are served
                async with http.get(f"http://127.0.0.1:{port}/") as r:
                    assert r.status == 200
                    assert "text/html" in r.headers.get("Content-Type", "")
                static_dir = os.path.join(
                    os.path.dirname(os.path.abspath(
                        __import__("entropy_arb.web", fromlist=["x"]).__file__)),
                    "webui")
                if os.path.isdir(static_dir) and \
                        os.path.isfile(os.path.join(static_dir, "style.css")):
                    async with http.get(
                            f"http://127.0.0.1:{port}/static/style.css") as r:
                        assert r.status == 200

                # websocket pushes at ~4Hz within a second
                async with http.ws_connect(
                        f"http://127.0.0.1:{port}/ws") as ws:
                    msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
                    assert msg.type == aiohttp.WSMsgType.TEXT
                    data = json.loads(msg.data)
                    assert data["venues"]["hedge"]["name"] == "RH"
        finally:
            await srv.stop()
        assert srv._runner is None

    asyncio.run(run())


def test_web_server_serves_starting_state_and_rejects_nothing():
    async def run():
        eng = Engine(make_cfg())   # no venues yet
        srv = WebServer(eng, "127.0.0.1", 0)
        port = await srv.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                        f"http://127.0.0.1:{port}/api/state") as r:
                    snap = await r.json()
                assert snap["status"] == "starting"
                # read-only: mutating methods must not exist as routes
                async with http.post(
                        f"http://127.0.0.1:{port}/api/state",
                        json={}) as r:
                    assert r.status == 405
        finally:
            await srv.stop()

    asyncio.run(run())


if __name__ == "__main__":
    test_web_server_state_ws_health()
    test_web_server_serves_starting_state_and_rejects_nothing()
    print("test_web OK")

"""Console analytics endpoints: /api/analyze, /api/backtest, /api/minutes.

Reads the recorder CSV configured by the selected profile. All handlers run
the (pure-CPU) analysis in the default executor so a huge CSV can never
stall the console's event loop.
"""
from __future__ import annotations

import asyncio
import os

from aiohttp import web

from ..analysis import analyze, load_rows, minutes_series, run_backtest


def _csv_path(profiles, profile: str):
    rel = profiles.recorder_csv(profile)
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(profiles_dir_root(profiles),
                                                       rel)
    return path


def profiles_dir_root(profiles) -> str:
    """CSV paths are engine-relative (engine cwd = project root = the
    console's cwd), so plain os.path works."""
    return os.getcwd()


def register_analytics(app: web.Application, profiles) -> None:
    async def _run(fn, *a, **kw):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    def _params(request):
        q = request.query
        return {
            "profile": q.get("profile", ""),
            "hours": float(q.get("hours", "0") or 0),
            "min_samples": int(q.get("min_samples", "10") or 10),
            "max_points": min(int(q.get("max_points", "3000") or 3000), 20000),
        }

    async def api_analyze(request):
        p = _params(request)
        if not profiles.exists(p["profile"]):
            return web.json_response({"error": "unknown profile"}, status=400)
        fees = request.query.get("fees_bps")
        fees_bps = float(fees) if fees not in (None, "") else \
            (profiles.fees_bps(p["profile"]) or 0.0)
        path = _csv_path(profiles, p["profile"])
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data", "rows": 0},
                                     status=404)
        rows = await _run(load_rows, path, p["hours"], p["min_samples"])
        if len(rows) < 5:
            return web.json_response({"error": "no_data", "rows": len(rows)},
                                     status=404)
        result = await _run(analyze, rows, fees_bps)
        result["recorder_csv"] = path
        return web.json_response(result)

    async def api_backtest(request):
        try:
            b = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        profile = b.get("profile", "")
        if not profiles.exists(profile):
            return web.json_response({"error": "unknown profile"}, status=400)
        path = _csv_path(profiles, profile)
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data"}, status=404)
        rows = await _run(load_rows, path, float(b.get("hours", 0) or 0), 0)
        if len(rows) < 5:
            return web.json_response({"error": "no_data"}, status=404)
        try:
            res = await _run(run_backtest, rows,
                             midline=float(b.get("midline", 0)),
                             upper=float(b.get("upper", 3)),
                             lower=float(b.get("lower", 3)),
                             fees_bps=float(b.get("fees_bps", 0)),
                             cap_usd=float(b.get("cap_usd", 1000)),
                             slice_usd=float(b.get("slice_usd", 500)),
                             edge_mode=str(b.get("edge_mode", "scale")),
                             scale=float(b.get("scale", 0.7)))
        except (TypeError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(res)

    async def api_minutes(request):
        p = _params(request)
        if not profiles.exists(p["profile"]):
            return web.json_response({"error": "unknown profile"}, status=400)
        path = _csv_path(profiles, p["profile"])
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data"}, status=404)
        rows = await _run(load_rows, path, p["hours"], p["min_samples"])
        series = await _run(minutes_series, rows, p["max_points"])
        return web.json_response(series)

    app.router.add_get("/api/analyze", api_analyze)
    app.router.add_post("/api/backtest", api_backtest)
    app.router.add_get("/api/minutes", api_minutes)

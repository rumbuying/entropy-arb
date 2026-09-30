"""Console analytics endpoints: /api/analyze, /api/backtest, /api/minutes.

Reads the recorder CSV configured by the selected profile. All handlers run
the (pure-CPU) analysis in the default executor so a huge CSV can never
stall the console's event loop.

Range selection (spec §10.6): legacy `hours` keeps working unchanged; the
absolute form takes start/end (ISO 8601 UTC instants, or local dates
resolved in the `timezone` param) — hours together with start/end is
rejected as ambiguous, start >= end is rejected, an unknown timezone is
rejected. Filtering happens before any statistic; every response reports
its actual coverage including gaps.
"""
from __future__ import annotations

import asyncio
import os
import time as time_mod
from datetime import date, datetime, timedelta, timezone

from aiohttp import web

from ..analysis import analyze, coverage, load_rows, minutes_series, \
    run_backtest


class RangeError(ValueError):
    pass


def _iso_instant(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise RangeError(f"invalid ISO 8601 instant: {s!r}")
    if dt.tzinfo is None:
        raise RangeError(f"instant without offset/Z: {s!r}")
    return dt.timestamp()


def _local_midnight_ts(d: date, tz) -> float:
    """UTC instant of local midnight — found by probing around noon so DST
    boundaries resolve to the right day start."""
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=tz).timestamp()
    lo = noon - 13 * 3600
    for t in [lo + i * 1800 for i in range(48)]:
        if datetime.fromtimestamp(t, tz).date() == d and \
                datetime.fromtimestamp(t - 1, tz).date() != d:
            return t
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def abs_range(getter, *, default_tz: str = "Asia/Shanghai") -> tuple:
    """(start_ts, end_ts, tzname) from start/end/timezone params, or
    (None, None, None) when the request uses no absolute range."""
    start_s = getter("start")
    end_s = getter("end")
    tzname = getter("timezone")
    if not start_s and not end_s:
        return None, None, None
    if not (start_s and end_s):
        raise RangeError("start and end must be provided together")
    # timezone is mandatory with an absolute range (spec §10.1) — a missing
    # tz must never silently fall back to the server's local midnight
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not tzname:
        raise RangeError("timezone is required with start/end")
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        raise RangeError(f"unknown timezone {tzname!r}")
    if len(start_s) == 10:                       # local date → day bounds
        start_ts = _local_midnight_ts(date.fromisoformat(start_s), tz)
    else:
        start_ts = _iso_instant(start_s)
    if len(end_s) == 10:                         # end day is inclusive
        end_ts = _local_midnight_ts(
            date.fromisoformat(end_s) + timedelta(days=1), tz)
    else:
        end_ts = _iso_instant(end_s)
    if start_ts >= end_ts:
        raise RangeError("start must be before end")
    return start_ts, end_ts, tzname


def profiles_dir_root(profiles) -> str:
    """CSV paths are engine-relative (engine cwd = project root = the
    console's cwd), so plain os.path works."""
    return os.getcwd()


def register_analytics(app: web.Application, profiles) -> None:
    async def _run(fn, *a, **kw):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **kw))

    def _csv_path(profiles, profile: str):
        rel = profiles.recorder_csv(profile)
        if not rel:
            return None
        path = rel if os.path.isabs(rel) else os.path.join(
            profiles_dir_root(profiles), rel)
        return path

    def _params(request):
        q = request.query
        return {
            "profile": q.get("profile", ""),
            "hours": float(q.get("hours", "0") or 0),
            "min_samples": int(q.get("min_samples", "10") or 10),
            "max_points": min(int(q.get("max_points", "3000") or 3000),
                              20000),
        }

    def _resolve_range(request, *, hours: float) -> tuple:
        """Absolute range or hours — both at once is rejected (§10.6)."""
        start_ts, end_ts, tzname = abs_range(request.query.get)
        if start_ts is not None and hours:
            raise RangeError("hours cannot be combined with start/end")
        return start_ts, end_ts, tzname

    def _range_envelope(request, rows, start_ts, end_ts, tzname) -> dict:
        if start_ts is None:
            return {}
        return {"period": {
                    "start": datetime.fromtimestamp(
                        start_ts, timezone.utc).isoformat().replace(
                        "+00:00", "Z"),
                    "end": datetime.fromtimestamp(
                        end_ts, timezone.utc).isoformat().replace(
                        "+00:00", "Z"),
                    "timezone": tzname},
                "as_of": time_mod.time(),
                "coverage": coverage(rows, start_ts, end_ts)}

    async def api_analyze(request):
        p = _params(request)
        if not profiles.exists(p["profile"]):
            return web.json_response({"error": "unknown profile"}, status=400)
        try:
            start_ts, end_ts, tzname = _resolve_range(request, hours=p["hours"])
        except RangeError as e:
            return web.json_response({"error": "invalid_range",
                                      "message": str(e)}, status=400)
        fees = request.query.get("fees_bps")
        fees_bps = float(fees) if fees not in (None, "") else \
            (profiles.fees_bps(p["profile"]) or 0.0)
        path = _csv_path(profiles, p["profile"])
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data", "rows": 0},
                                     status=404)
        rows = await _run(load_rows, path, p["hours"], p["min_samples"],
                          start_ts, end_ts)
        if len(rows) < 5:
            return web.json_response({"error": "no_data", "rows": len(rows)},
                                     status=404)
        result = await _run(analyze, rows, fees_bps)
        result["recorder_csv"] = path
        result.update(_range_envelope(request, rows, start_ts, end_ts,
                                      tzname))
        return web.json_response(result)

    async def api_backtest(request):
        try:
            b = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        profile = b.get("profile", "")
        if not profiles.exists(profile):
            return web.json_response({"error": "unknown profile"}, status=400)

        class _G:
            def __init__(self, d):
                self.d = d

            def __call__(self, k):
                return self.d.get(k)
        try:
            start_ts, end_ts, tzname = abs_range(_G(b))
            hours = float(b.get("hours", 0) or 0)
            if start_ts is not None and hours:
                raise RangeError("hours cannot be combined with start/end")
        except RangeError as e:
            return web.json_response({"error": "invalid_range",
                                      "message": str(e)}, status=400)
        path = _csv_path(profiles, profile)
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data"}, status=404)
        rows = await _run(load_rows, path, hours, 0, start_ts, end_ts)
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
        res.update(_range_envelope(request, rows, start_ts, end_ts, tzname))
        return web.json_response(res)

    async def api_minutes(request):
        p = _params(request)
        if not profiles.exists(p["profile"]):
            return web.json_response({"error": "unknown profile"}, status=400)
        try:
            start_ts, end_ts, tzname = _resolve_range(request, hours=p["hours"])
        except RangeError as e:
            return web.json_response({"error": "invalid_range",
                                      "message": str(e)}, status=400)
        path = _csv_path(profiles, p["profile"])
        if not path or not os.path.exists(path):
            return web.json_response({"error": "no_data"}, status=404)
        rows = await _run(load_rows, path, p["hours"], p["min_samples"],
                          start_ts, end_ts)
        series = await _run(minutes_series, rows, p["max_points"])
        series.update(_range_envelope(request, rows, start_ts, end_ts,
                                      tzname))
        return web.json_response(series)

    app.router.add_get("/api/analyze", api_analyze)
    app.router.add_post("/api/backtest", api_backtest)
    app.router.add_get("/api/minutes", api_minutes)

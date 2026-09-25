"""Venue-dimension aggregation for the console's Venues tab.

Pure functions: the server handler feeds one record per RUNNING worker
(its status dict, its /api/state snapshot, today's realized edge) and
gets back the structure the webui renders as three tables — per-exchange
totals, per-exchange position detail, and per-strategy P&L.

Equity needs deduplication: every engine touching the same account
reports its own (slightly stale) copy of the same number, so the group
takes the max instead of a sum. Positions, in contrast, are per-strategy
facts and do sum.
"""
from __future__ import annotations

import csv
import os
import time


def exchange_of(venue_name: str) -> str:
    """Normalize a venue display name to an exchange group label."""
    n = (venue_name or "").upper()
    if "ENTROPY" in n or n == "HL":
        return "HL(io)"
    if n == "RH" or "LIGHTER-RH" in n:
        return "Lighter-RH"
    if "KATANA" in n:
        return "Katana"
    if "LIGHTER" in n:
        return "Lighter"
    return venue_name or "?"


def _local_midnight() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        0, 0, 0, 0, 0, -1))


def realized_today(root: str, status: dict, profile_yaml: dict) -> "float | None":
    """Today's locked edge from the strategy's trades CSV, or None.

    Taker engines write logs/trades-{SYM}-{hedge}.csv (sum fill_edge_usd
    over fully-filled entries); maker engines write the profile's
    maker.trades_csv (sum net_edge_bps × qty × px). No file or no fills
    today → None so the UI can show a dash instead of a fake zero."""
    midnight = _local_midnight()
    if (profile_yaml or {}).get("maker", {}).get("enabled"):
        path = profile_yaml["maker"].get("trades_csv") or ""
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        if not os.path.exists(path):
            return None
        total = 0.0
        seen = False
        with open(path, newline="", errors="replace") as fh:
            for r in csv.DictReader(fh):
                try:
                    if float(r["ts"]) < midnight or not r.get("hedge_qty"):
                        continue
                    total += (float(r["net_edge_bps"]) / 1e4
                              * float(r["hedge_qty"]) * float(r["hedge_px"]))
                    seen = True
                except (KeyError, ValueError, TypeError):
                    continue
        return total if seen else None
    path = os.path.join(root, "logs",
                        f"trades-{status.get('symbol')}-{status.get('hedge')}.csv")
    if not os.path.exists(path):
        return None
    total = 0.0
    seen = False
    with open(path, newline="", errors="replace") as fh:
        for r in csv.DictReader(fh):
            try:
                if float(r["ts"]) < midnight:
                    continue
                if r.get("buy_status") == "filled" and \
                        r.get("sell_status") == "filled":
                    total += float(r["fill_edge_usd"])
                    seen = True
            except (KeyError, ValueError, TypeError):
                continue
    return total if seen else None


def _notional(v: dict) -> "float | None":
    """Signed notional — the engine's position_usd is unsigned, so the
    sign comes from the position itself."""
    pos = float(v.get("position") or 0.0)
    if not pos:
        return None
    if v.get("position_usd") is not None:
        return abs(float(v["position_usd"])) * (1.0 if pos > 0 else -1.0)
    bid, ask = v.get("bid"), v.get("ask")
    if bid and ask:
        return pos * (bid + ask) / 2.0
    return None


def aggregate(records: list) -> dict:
    """Build the Venues-tab payload from per-worker records."""
    groups: dict = {}
    strategies = []
    total_pnl = 0.0
    for rec in records:
        st = rec["status"]
        snap = rec.get("snap")
        if st.get("state") != "running" or not snap:
            continue
        pnl = (snap.get("session") or {}).get("pnl_mtm")
        total_pnl += pnl or 0.0
        strategies.append({
            "worker": st["id"], "profile": st["profile"],
            "symbol": st["symbol"], "hedge": st["hedge"],
            "mode": st["mode"], "state": st["state"],
            "maker": bool(rec.get("maker")),
            "pnl_mtm": pnl, "realized_today": rec.get("realized"),
        })
        for v in (snap.get("venues") or {}).values():
            ex = exchange_of(v.get("name"))
            g = groups.setdefault(ex, {"positions": [], "equities": [],
                                       "frees": [], "engines": set()})
            g["engines"].add(st["id"])
            if v.get("equity") is not None:
                g["equities"].append(float(v["equity"]))
            if v.get("free") is not None:
                g["frees"].append(float(v["free"]))
            pos = float(v.get("position") or 0.0)
            if pos == 0.0:
                continue
            notional = _notional(v)
            g["positions"].append({
                "exchange": ex, "worker": st["id"],
                "profile": st["profile"], "symbol": st["symbol"],
                "leg": v.get("key"), "venue": v.get("name"),
                "side": "long" if pos > 0 else "short",
                "size": pos, "notional_usd": notional,
                "fresh": v.get("fresh"), "down": v.get("down"),
            })
    exchanges = []
    for name in sorted(groups):
        g = groups[name]
        gross = sum(abs(p["notional_usd"]) for p in g["positions"]
                    if p["notional_usd"] is not None)
        net = sum(p["notional_usd"] for p in g["positions"]
                  if p["notional_usd"] is not None)
        exchanges.append({
            "exchange": name,
            "equity": max(g["equities"]) if g["equities"] else None,
            "free": max(g["frees"]) if g["frees"] else None,
            "gross_usd": gross, "net_usd": net,
            "engines": len(g["engines"]),
            "positions": g["positions"],
        })
    return {"exchanges": exchanges, "strategies": strategies,
            "total_pnl_mtm": total_pnl,
            "asof": time.time()}


def load_profile_yaml(profiles_dir: str, name: str) -> dict:
    """Parsed profile yaml, or {} — never raises (dashboards must not)."""
    try:
        import yaml
        with open(os.path.join(profiles_dir, f"{name}.yaml")) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}

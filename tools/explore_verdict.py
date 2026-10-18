#!/usr/bin/env python3
"""One-shot verdict over the discovery matrix: which observed pairs are worth
going live on. Ranks by conservative executable metrics, not raw p95 bps."""
import json
import sys
from pathlib import Path

ROOT = Path("/root/code/entropy")
matrix = json.loads((ROOT / "logs/discovery/matrix.json").read_text())
promos = json.loads((ROOT / "logs/discovery/promotions.json").read_text())

FEES = {"hl": 4.5, "hl:io": 0.9, "hl:xyz": 1.0, "lighter": 0.0, "lighter-rh": 0.0,
        "backpack": 2.5, "bulk": 3.5, "ondo": 2.5, "arcus": 2.25, "perpl": 3.45}

rows = []
for sym, blk in matrix["symbols"].items():
    for p in blk["pairs"]:
        if p["a"] >= p["b"]:
            continue  # keep one orientation; edges mirror
        promo = promos.get(f"{sym}|{p['a']}|{p['b']}") or promos.get(f"{sym}|{p['b']}|{p['a']}")
        rows.append({
            "symbol": sym, "pair": f"{p['a']}↔{p['b']}",
            "hours": round(p["hours"], 1), "n": p["n"],
            "pot": p["roundtrip_potential_bps"],
            "buy": p["net_buy_p95"], "sell": p["net_sell_p95"],
            "harvest": p["harvest_bps"],
            "cap": p["capacity_usd"], "hits": round(p["hits_per_day"], 1),
            "hl_min": p.get("half_life_min"), "drift": p.get("drift_bps_day"),
            "sd": p["premium_sd"], "state": p["state"],
            "anchor": p.get("anchor"), "fees": p["fees_bps"],
            "promoted": bool(promo),
        })

def score(r):
    """Conservative go-live score: require real executable harvest, size,
    frequency, and persistence; penalize unstable anchors."""
    if r["harvest"] is None:
        return -999
    s = 0.0
    s += min(r["harvest"], 10) * 3.0          # executable oscillation edge
    s += min(r["pot"], 20) * 1.0              # directional p95 potential
    s += min(r["hits"], 200) / 20.0           # frequency (capped: more isn't linearly better)
    s += min(r["cap"], 5000) / 500.0          # capacity up to $5k
    s += min(r["hours"], 72) / 12.0           # evidence age up to stable window
    if r["anchor"] != "anchored":
        s -= 8
    if r["hl_min"] is not None and r["hl_min"] > 120:
        s -= 4                                 # slow mean reversion is worse
    if r["pot"] < 6 or r["hits"] < 3 or r["cap"] < 200:
        s -= 20                                # below the watchlist's own gates
    return round(s, 2)

for r in rows:
    r["score"] = score(r)

rows.sort(key=lambda r: -r["score"])
cands = [r for r in rows if r["state"].endswith("candidate")]
print(f"total pairs={len(rows)}  candidate-grade={len(cands)}\n")

hdr = f"{'score':>6} {'symbol':7} {'pair':24} {'hrs':>6} {'pot':>6} {'harvest':>7} {'cap$':>7} {'hits/d':>7} {'hl_m':>5} {'drift':>7} {'state':22} prom"
print(hdr); print("-" * len(hdr))
for r in rows[:28]:
    print(f"{r['score']:>6} {r['symbol']:7} {r['pair']:24} {r['hours']:>6} {r['pot']:>6} "
          f"{r['harvest']:>7} {r['cap']:>7.0f} {r['hits']:>7} {str(r['hl_min']):>5} "
          f"{r['drift']:>7.1f} {r['state']:22} {'Y' if r['promoted'] else '-'}")

print("\n--- dead-but-loud (high pot, failed gates) ---")
bad = [r for r in rows if r["pot"] >= 8 and (r["harvest"] or 0) <= 0][:8]
for r in bad:
    print(f"{r['symbol']:7} {r['pair']:24} pot={r['pot']} harvest={r['harvest']} "
          f"cap={r['cap']:.0f} hits={r['hits']} fees={r['fees']}")

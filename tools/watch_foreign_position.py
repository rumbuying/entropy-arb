#!/usr/bin/env python3
"""Tripwire for foreign fills on the shared ANTH accounts.

Background: on 2026-10-16 the chain position of ANTH was found to be
ENT -0.736 / RH +0.737 while this box's engine ledger expected ~±0.2 —
most likely a second entropy-arb instance (dev machine) trading the same
Lighter account 27904 / HL io account with the same .env credentials.

Logic: sample /api/state on both live engines, take the chain position of
each venue, subtract what OUR engine's trades CSV says it did since the
last sample — any residual is foreign flow. Logs one line per sample;
prints ALERT lines when |residual| >= threshold.

    python3 tools/watch_foreign_position.py            # one shot
    watch=1 python3 tools/watch_foreign_position.py    # loop every 60s
"""
import csv, json, os, sys, time, urllib.request

STATE = {("ANTH", "entropy"): "http://127.0.0.1:8803/api/state",
         ("ANTH", "hedge"): "http://127.0.0.1:8803/api/state",
         ("SNDK", "entropy"): "http://127.0.0.1:8806/api/state",
         ("SNDK", "hedge"): "http://127.0.0.1:8806/api/state"}
TRADES = {"ANTH": "logs/trades-ANTH-lighter-rh.csv",
          "SNDK": "logs/trades-SNDK-lighter-rh.csv"}
STATE_FILE = "/tmp/watch_foreign_position.json"
THRESHOLD = 0.01          # qty units — engine min order is ~0.005
LOG = "logs/foreign-position-watch.log"


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def engine_flow(sym, since_ts):
    """(entropy_delta, hedge_delta) our engine executed since since_ts."""
    de = dh = 0.0
    path = TRADES[sym]
    if not os.path.exists(path):
        return 0.0, 0.0
    with open(path) as fh:
        for r in csv.DictReader(fh):
            ts = float(r["ts"])
            if ts <= since_ts:
                continue
            q = float(r["buy_fill"]) or 0.0
            if r["direction"] == "buy_entropy":
                de += q
                dh -= q
            else:
                de -= q
                dh += q
    return de, dh


def sample(prev):
    now = time.time()
    out = {}
    seen_syms = {}
    for (sym, venue), url in STATE.items():
        try:
            d = get(url)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} WARN {sym} {venue}: state unavailable: {e}")
            return None
        pos = d["venues"][venue]["position"]
        out[(sym, venue)] = pos
        seen_syms[sym] = d["session"]  # any live session counter works
    lines = []
    alerted = False
    for sym in ("ANTH", "SNDK"):
        de, dh = engine_flow(sym, prev.get("ts", 0) if prev else 0)
        for venue, our_d in (("entropy", de), ("hedge", dh)):
            cur = out[(sym, venue)]
            base = prev.get(f"{sym}:{venue}") if prev else None
            if base is None:
                lines.append(f"{sym} {venue} chain={cur:+.5f}")
                continue
            chain_d = cur - base
            residual = chain_d - our_d
            flag = ""
            if abs(residual) >= THRESHOLD:
                flag = f"  *** ALERT foreign={residual:+.4f} (chain {chain_d:+.4f}, ours {our_d:+.4f})"
                alerted = True
            lines.append(f"{sym} {venue} chain={cur:+.5f} Δ={chain_d:+.4f} ours={our_d:+.4f}{flag}")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    report = stamp + "  " + " | ".join(lines)
    print(report)
    with open(LOG, "a") as fh:
        fh.write(report + "\n")
    if alerted:
        with open(LOG, "a") as fh:
            fh.write(stamp + "  *** FOREIGN FLOW DETECTED — a second instance is likely trading\n")
    return {"ts": now, **{f"{k[0]}:{k[1]}": v for k, v in out.items()}}


def main():
    prev = None
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE))
        except Exception:
            prev = None
    prev = sample(prev)
    if prev:
        json.dump(prev, open(STATE_FILE, "w"))
    if os.environ.get("watch"):
        interval = int(os.environ.get("interval", "60"))
        while True:
            time.sleep(interval)
            try:
                prev = sample(prev)
                if prev:
                    json.dump(prev, open(STATE_FILE, "w"))
            except KeyboardInterrupt:
                break


if __name__ == "__main__":
    main()

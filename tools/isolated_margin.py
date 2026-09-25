#!/usr/bin/env python3
"""Add or remove isolated margin on a Hyperliquid (incl. HIP-3) position.

Dry-run by default; pass --go to sign and post to /exchange. Reuses the exact
asset-id resolution the live engine uses, and verifies the result against
clearinghouseState afterwards.

    # 把 spot 里全部可用 USDC 划入 io:ANTH 的逐仓保证金
    python3 tools/isolated_margin.py --dex io --coin io:ANTH --all

    # 指定金额；负数 = 取回保证金（不能低于维持保证金）
    python3 tools/isolated_margin.py --dex io --coin io:ANTH --amount -100 --go

Why a script: an isolated position's liq price is only as far away as the
margin posted to it, and the engine cap usually stops the bot from adding —
this is the manual lever for that (2026-09-25: +$347.48 moved liq from
$1948.67 / -6.9% to $1157.29 / -44.7%).
"""
import json
import math
import os
import sys
import time
import urllib.request

HL = "https://api.hyperliquid.xyz/info"
HL_EX = "https://api.hyperliquid.xyz/exchange"
ROOT = "/root/code/entropy"
DEX, COIN = "io", "io:ANTH"   # defaults; override with --dex/--coin


def post(url, payload, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def env(key):
    with open(os.path.join(ROOT, ".env")) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"{key} not in .env")


def asset_id():
    names = [(d or {}).get("name", "") for d in post(HL, {"type": "perpDexs"})]
    di = names.index(DEX)
    universe = post(HL, {"type": "meta", "dex": DEX})["universe"]
    for i, a in enumerate(universe):
        if a["name"] == COIN:
            aid = 110000 + (di - 1) * 10000 + i
            return aid, a
    raise SystemExit(f"{COIN} not found on dex {DEX}")


def state(addr):
    spot = post(HL, {"type": "spotClearinghouseState", "user": addr})
    usdc = next(b for b in spot["balances"] if b["coin"] == "USDC")
    total, hold = float(usdc["total"]), float(usdc["hold"])
    st = post(HL, {"type": "clearinghouseState", "user": addr, "dex": DEX})
    pos = (st.get("assetPositions") or [{}])[0].get("position") or {}
    return {"total": total, "hold": hold, "free": total - hold,
            "margin": float(st["marginSummary"]["totalMarginUsed"]),
            "szi": float(pos.get("szi") or 0),
            "liq": float(pos.get("liquidationPx") or 0),
            "upnl": float(pos.get("unrealizedPnl") or 0),
            "mark": float(pos.get("positionValue") or 0) / float(pos.get("szi") or 1)}


def main():
    global DEX, COIN
    argv = sys.argv[1:]
    go = "--go" in argv
    if "--dex" in argv:
        DEX = argv[argv.index("--dex") + 1]
    if "--coin" in argv:
        COIN = argv[argv.index("--coin") + 1]
    addr = env("HL_ACCOUNT_ADDRESS").lower()
    aid, meta = asset_id()
    before = state(addr)

    if "--amount" in argv:
        amount = float(argv[argv.index("--amount") + 1])
    else:
        # default: all free spot USDC, floored to cents
        amount = math.floor(before["free"] * 100) / 100.0
    ntli = int(round(amount * 1e6))

    print(f"asset          : {COIN}  asset_id={aid}  onlyIsolated={meta.get('onlyIsolated')} "
          f"maxLev={meta.get('maxLeverage')}x")
    print(f"position       : szi={before['szi']:+.4f}  mark={before['mark']:.2f}  "
          f"uPnL={before['upnl']:+.2f}")
    print(f"io margin      : ${before['margin']:.2f}   liq=${before['liq']:.2f}   "
          f"dist={(before['mark']-before['liq'])/before['mark']*100:.1f}%")
    print(f"spot USDC      : total=${before['total']:.6f} hold=${before['hold']:.6f} "
          f"free=${before['free']:.6f}")
    action = {"type": "updateIsolatedMargin", "asset": aid, "isBuy": True,
              "ntli": ntli}
    print(f"\naction         : {json.dumps(action)}")
    print(f"amount         : +${amount:.6f}  (ntli={ntli} = USD*1e6)")

    if not go:
        print("\n[dry-run] nothing sent. re-run with --go to execute.")
        return 0

    from eth_account import Account
    from hyperliquid.utils import signing as s

    wallet = Account.from_key(env("HL_PRIVATE_KEY"))
    print(f"\nsigner         : {wallet.address}  account={addr}"
          + ("  (agent mode)" if wallet.address.lower() != addr else ""))
    nonce = int(time.time() * 1000)
    for attempt in range(4):
        sig = s.sign_l1_action(wallet, action, None, nonce, None, True)
        payload = {"action": action, "nonce": nonce, "signature": sig,
                   "vaultAddress": None, "expiresAfter": None}
        body = post(HL_EX, payload, timeout=20)
        print(f"exchange       : nonce={nonce} -> {json.dumps(body)}")
        if body.get("status") == "ok":
            break
        msg = json.dumps(body).lower()
        if "nonce" in msg and attempt < 3:
            nonce = int(time.time() * 1000) + 1000
            print("               : nonce rejected, retrying with a newer one")
            continue
        return 1

    time.sleep(2)
    after = state(addr)
    print(f"\nafter          : io margin ${after['margin']:.2f}  liq=${after['liq']:.2f}  "
          f"dist={(after['mark']-after['liq'])/after['mark']*100:.1f}%  "
          f"spot free=${after['free']:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

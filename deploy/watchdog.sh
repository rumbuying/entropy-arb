#!/usr/bin/env bash
# entropy-arb 看门狗：单用户健康检查 + 告警
# 由 entropy-watchdog.timer 每 2 分钟触发一次（Type=oneshot）。
#
# 监测：
#   1. 控制台进程存活   （http://127.0.0.1:8788/api/meta 无响应）
#   2. worker 崩溃退出   （/api/workers 里 state=errored；手动停止是 stopped，不告警）
#   3. 引擎 HALT         （快照 status=halted：连续执行错误停机，需人工看日志后重启）
#   4. 交易所断连        （status=venue_down：已暂停交易，恢复后自动继续，恢复时会通知）
#   5. 逼近强平          （距清算价 < LIQ_WARN_BPS，默认 1000 bps = 10%）
#   6. 保证金用满        （margin_frac >= MARGIN_WARN_FRAC，默认 0.9）
#
# 告警通道：
#   - 在 /etc/default/entropy-watchdog 填 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#     后自动推送 Telegram（服务器需能访问 api.telegram.org）；
#   - 不配置也能用：所有告警/恢复都写入 logs/watchdog.log。
# 去重：同一告警只在出现时发一次，恢复时发一条恢复通知（状态存 logs/.watchdog-active）。
set -u
ROOT=/root/code/entropy
CONSOLE=http://127.0.0.1:8788
STATE=$ROOT/logs/.watchdog-active
LOGF=$ROOT/logs/watchdog.log
[ -f /etc/default/entropy-watchdog ] && . /etc/default/entropy-watchdog
# 控制台若启用了 token（systemd unit 的 --token），探测请求必须带上：
# 否则 /api/* 一律 401，watchdog 会永久误报 console-down 并漏掉真实告警。
# 解析顺序（unit 用 EnvironmentFile 注入时，grep '--token' 只能抓到字面量
# ${ENTROPY_CONSOLE_TOKEN}，那不是真 token —— 2026-09-20 → 09-24 就是这样
# 卡在 console-down，期间所有 HALT/断连/崩溃都不告警）：
#   1. 环境变量 ENTROPY_CONSOLE_TOKEN（/etc/default/entropy-watchdog）
#   2. unit 的 EnvironmentFile 里的 ENTROPY_CONSOLE_TOKEN
#   3. unit 命令行里写死的 --token（旧写法）
TOKEN="${ENTROPY_CONSOLE_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  ENVF=$(grep -oP '(?<=^EnvironmentFile=).*' \
           /etc/systemd/system/entropy-console.service 2>/dev/null | head -1)
  if [ -n "${ENVF:-}" ] && [ -f "$ENVF" ]; then
    TOKEN=$(grep -oP '(?<=^ENTROPY_CONSOLE_TOKEN=).*' "$ENVF" | head -1)
  fi
fi
if [ -z "$TOKEN" ]; then
  TOKEN=$(grep -oP '(?<=--token )\S+' \
            /etc/systemd/system/entropy-console.service 2>/dev/null || true)
  case "$TOKEN" in *'${'*) TOKEN="";; esac   # 变量引用不是 token
fi
[ -n "$TOKEN" ] || echo "watchdog: no console token resolved — /api/* will 401" >&2
mkdir -p "$ROOT/logs"
touch "$STATE"

log() { echo "$(date '+%F %T') $*" >> "$LOGF"; }

notify() {
  log "$*"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -fsS -m 10 -o /dev/null \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=[entropy-arb] $*" \
      || log "(telegram 发送失败)"
  fi
}

ALERTS=$(python3 - "$CONSOLE" "${TOKEN:-}" "${LIQ_WARN_BPS:-1000}" \
                       "${MARGIN_WARN_FRAC:-0.9}" <<'PY'
import json, sys, urllib.request

base, token = sys.argv[1], sys.argv[2]
liq_warn_bps = float(sys.argv[3])
margin_warn = float(sys.argv[4])

def get(path):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)

def pct(bps):
    return "%.1f%%" % (bps / 100.0)

try:
    get("/api/meta")
except Exception:
    print("console-down\t控制台无响应！请检查：systemctl status entropy-console")
    raise SystemExit(0)
try:
    workers = get("/api/workers")
except Exception:
    raise SystemExit(0)
for w in workers:
    wid, st = w.get("id", "?"), w.get("state")
    label = "%s %s/%s" % (w.get("profile", "?"),
                          w.get("symbol", "?"), w.get("hedge", "?"))
    if st == "errored":
        print("worker-crashed:%s\tworker 崩溃退出：%s (exit=%s)"
              " — 控制台 Runs 页可重启" % (wid, label, w.get("exit_code")))
        continue
    if st != "running":
        continue
    try:
        snap = get("/api/workers/%s/state" % wid)
    except Exception:
        continue
    status = snap.get("status")
    if status == "halted":
        print("halted:%s\t引擎 HALT：%s — 连续执行错误已停机，"
              "看日志后手动重启" % (wid, label))
    elif status == "venue_down":
        print("venue-down:%s\t交易所断连：%s — 已暂停交易，恢复后自动继续"
              % (wid, label))

    # 风险：距强平太近 / 保证金用满。两个都只报警不动作。
    for key, v in (snap.get("venues") or {}).items():
        d = v.get("liq_dist_bps")
        if d is not None and d < liq_warn_bps:
            print("liq-near:%s:%s\t逼近强平：%s %s 现价距清算价仅 %s"
                  "（清算 $%s，仓位 %s）— 考虑减仓或补保证金"
                  % (wid, key, label, v.get("name"), pct(d),
                     v.get("liq_px"), v.get("position")))
        mf = v.get("margin_frac")
        if mf is not None and mf >= margin_warn:
            print("margin-high:%s:%s\t保证金已用 %.0f%%：%s %s"
                  "（已用 $%.2f / 权益 $%.2f）— 无余量再加仓"
                  % (wid, key, mf * 100, label, v.get("name"),
                     v.get("margin_used") or 0.0,
                     v.get("margin_collateral") or 0.0))
PY
)

# 交易所侧风险检查：不依赖引擎（引擎可能没重启、甚至挂了，而清算只认交易所）。
# 直接读 .env 里的公开账户标识，查每个 dex/市场的清算价与保证金占用。
RISK=$(python3 - "$ROOT" "${LIQ_WARN_BPS:-1000}" "${MARGIN_WARN_FRAC:-0.9}" <<'PY'
import json, os, sys, urllib.request

root, liq_warn, margin_warn = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
HL = "https://api.hyperliquid.xyz/info"
RH = "https://api.rh.lighter.xyz/api/v1/account"
TIMEOUT = 8
out = []


def env(key):
    try:
        with open(os.path.join(root, ".env")) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def get(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.load(r)


def dist_bps(mark, liq, signed_size):
    """Positive = room left before liquidation, in bps of mark."""
    d = (mark - liq) / mark * 1e4
    return -d if signed_size < 0 else d


def report(tag, text):
    out.append((tag, text))


addr = env("HL_ACCOUNT_ADDRESS")
if addr:
    try:
        dexes = [""] + [d.get("name") for d in post(HL, {"type": "perpDexs"})
                        if isinstance(d, dict) and d.get("name")]
    except Exception:
        dexes = [""]
    for dex in dexes:
        try:
            st = post(HL, {"type": "clearinghouseState", "user": addr, "dex": dex})
        except Exception:
            continue
        tag_dex = dex or "core"
        ms = st.get("marginSummary") or {}
        try:
            av = float(ms.get("accountValue") or 0)
        except (TypeError, ValueError):
            av = 0.0
        maxlev = {}
        try:
            for a in post(HL, {"type": "meta", "dex": dex}).get("universe") or []:
                maxlev[a["name"]] = float(a.get("maxLeverage") or 0)
        except Exception:
            pass
        for ap in st.get("assetPositions") or []:
            p = ap.get("position") or {}
            try:
                szi = float(p.get("szi") or 0)
                liq = float(p.get("liquidationPx") or 0)
                mark = abs(float(p.get("positionValue") or 0) / szi)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if not szi or mark <= 0:
                continue
            # 逐仓的 marginUsed/accountValue 恒等于 1，所以用「杠杆 vs 上限」
            ml = maxlev.get(p.get("coin") or "", 0)
            if liq > 0:
                d = dist_bps(mark, liq, szi)
                if d < liq_warn:
                    report("hl-liq:%s:%s" % (tag_dex, p.get("coin")),
                           "HL/%s %s 距强平仅 %.1f%%（现价 %.2f，清算 %.2f，持仓 %+.4f）"
                           "— 考虑减仓或补保证金"
                           % (tag_dex, p.get("coin"), d / 100, mark, liq, szi))
            if av > 0 and ml > 0 and (mark * abs(szi) / ml) / av >= margin_warn:
                report("hl-margin:%s:%s" % (tag_dex, p.get("coin")),
                       "HL/%s %s 杠杆已到上限附近：%.1fx / 上限 %.0fx（占用 $%.2f / 权益 $%.2f）"
                       "— 无余量再加仓"
                       % (tag_dex, p.get("coin"), mark * abs(szi) / av, ml,
                          mark * abs(szi) / ml, av))

idx = env("LIGHTER_ACCOUNT_INDEX")
if idx:
    try:
        acct = (get("%s?by=index&value=%s" % (RH, idx)).get("accounts") or [{}])[0]
    except Exception:
        acct = {}
    try:
        coll = float(acct.get("collateral") or 0)
        avail = float(acct.get("available_balance") or 0)
    except (TypeError, ValueError):
        coll = avail = 0.0
    if coll > 0 and (coll - avail) / coll >= margin_warn:
        report("lighter-margin:%s" % idx,
               "Lighter/%s 保证金已用 %.0f%%（$%.2f / $%.2f）"
               % (idx, (coll - avail) / coll * 100, coll - avail, coll))
    for p in acct.get("positions") or []:
        try:
            qty = float(p.get("position") or 0)
            sign = float(p.get("sign") or 1)
            liq = float(p.get("liquidation_price") or 0)
            mark = abs(float(p.get("position_value") or 0) / qty)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not qty or liq <= 0 or mark <= 0:
            continue
        d = dist_bps(mark, liq, sign)
        if d < liq_warn:
            report("lighter-liq:%s" % p.get("symbol"),
                   "Lighter/%s 距强平仅 %.1f%%（现价 %.2f，清算 %.2f）"
                   "— 考虑减仓或补保证金"
                   % (p.get("symbol"), d / 100, mark, liq))

for tag, text in out:
    print("%s\t%s" % (tag, text))
PY
)

ALERTS="$ALERTS
$RISK"
NEW=""
while IFS=$'\t' read -r key msg; do
  [ -n "${key:-}" ] || continue
  NEW="$NEW$key"$'\n'
  grep -qxF "$key" "$STATE" || notify "ALERT $msg"
done <<< "$ALERTS"

while IFS= read -r k; do
  [ -n "$k" ] || continue
  grep -qxF "$k" <<< "$NEW" || notify "RECOVERED $k"
done < "$STATE"

printf '%s' "$NEW" > "$STATE"

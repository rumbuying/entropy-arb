#!/usr/bin/env bash
# entropy-arb 看门狗：单用户健康检查 + 告警
# 由 entropy-watchdog.timer 每 2 分钟触发一次（Type=oneshot）。
#
# 监测：
#   1. 控制台进程存活   （http://127.0.0.1:8788/api/meta 无响应）
#   2. worker 崩溃退出   （/api/workers 里 state=errored；手动停止是 stopped，不告警）
#   3. 引擎 HALT         （快照 status=halted：连续执行错误停机，需人工看日志后重启）
#   4. 交易所断连        （status=venue_down：已暂停交易，恢复后自动继续，恢复时会通知）
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

ALERTS=$(python3 - "$CONSOLE" "${TOKEN:-}" <<'PY'
import json, sys, urllib.request

base, token = sys.argv[1], sys.argv[2]

def get(path):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)

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
PY
)

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

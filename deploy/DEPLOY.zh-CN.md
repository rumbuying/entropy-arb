# 部署清单（单用户服务器） / DEPLOY

> 目标场景：一台服务器跑 entropy-arb，只有你一个人用浏览器远程管理。
> 本文按本机（OpenCloudOS 9.4）实际核查结果编写，并已反映 2026-09-16 的
> 安装进度：第 2/3/4 节及看门狗、备份**均已装好并验证**。

## 0. 已经就绪、不用再做的

| 项 | 状态 |
|---|---|
| Python 3.11.6 + 全部依赖（含实盘 SDK） | ✅ 系统级已装 |
| `pytest tests/` | ✅ 42 passed |
| `config.yaml` 阈值（midline -4.2 / upper 6.5 / lower 5.5，28.5h 实测） | ✅ |
| **控制台 systemd 常驻**（`entropy-console.service`，开机自启+崩溃拉起） | ✅ 已装，active |
| **日志轮转**（`/etc/logrotate.d/entropy`，log 周轮/CVS 月轮，copytruncate） | ✅ 已装，dry-run 验证 |
| **看门狗**（`entropy-watchdog.timer`，每 2 分钟：控制台存活/worker 崩溃/HALT/交易所断连，恢复自动通知） | ✅ 已装，告警→恢复闭环实测通过 |
| **每周备份**（`entropy-backup.timer`，周日 03:00，config.yaml+profiles+.env → /root/backups，留 8 份，0600） | ✅ 已装，手动跑通 |
| 机器时钟 | ✅ chrony NTP 已同步 |
| 端口 8788 / 8787 / 8801+ | ✅ 无冲突 |
| 优雅停机（SIGTERM → 结算在途订单 + 对账） | ✅ |

## 1. 剩下必做：填密钥 `.env`（当前缺失，`/api/meta` 显示 `env_exists: false`）

没有 `.env` 只能 RECORD-ONLY。控制台"API Keys"页签直接填（推荐，自动
0600+校验+审计），或手工：

```bash
cp .env.example .env && chmod 600 .env && vim .env
```

要点：`HL_PRIVATE_KEY` 填 **agent** 钱包私钥；`LIGHTER_*` 三项必须与
启动 `--hedge` 同一部署（主网和 Robinhood 链是两套账户）。

## 2. 访问方式：域名 + HTTPS + token（✅ 已上线）

控制台只绑 127.0.0.1:8788，由 nginx(443) 反代 `taoli.coinfetcher.xyz`；
token 即访问密码，存于 gitignored 的 `deploy/console-token.env`
（`ENTROPY_CONSOLE_TOKEN`，由 systemd unit 的 EnvironmentFile 注入），不入版本库。

```bash
# 正式地址（收藏这条）：
#   https://taoli.coinfetcher.xyz/?token=<见 deploy/console-token.env>
# 重新接入/换域名（幂等）：
./deploy/setup-domain.sh <新域名>
```

- 证书自动续期：`certbot-renew.timer` 每天两次，续期后自动 reload nginx
- 安全矩阵（已实测）：无/错 token 的所有 /api/* 一律 401；页面壳无敏感数据
- token 存 sessionStorage：换浏览器/标签需重带 `?token=` 的完整链接

## 3. 日常运维速查

```bash
systemctl status entropy-console          # 控制台状态
journalctl -u entropy-console -f          # 控制台输出（含 worker 启停）
tail -f logs/watchdog.log                 # 告警历史
systemctl restart entropy-console         # 重启（会优雅停掉所有 worker）
./deploy/entropy-backup.sh                # 手动备份
```

看门狗规则：worker 崩溃 / 引擎 HALT / 交易所断连 / 控制台无响应 → 告警
一次，恢复时通知。**Telegram 推送**：编辑 `/etc/default/entropy-watchdog`
填 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（去掉行首 #），立即生效，
无需重启任何服务。

## 4. 剩下必做：上线前人工验证（需要你的浏览器）

1. 隧道连上控制台，六个页签全部过一遍（Overview / Runs / Strategy
   Config / API Keys / Analyzer / History）。
2. 填好密钥后先 RECORD-ONLY 启动一个 profile 确认采集正常。
3. 小额 `max_position_usd` 试 LIVE，观察 Overview 卡片和 trades。

## 5. 已知行为（设计如此，不是故障）

- **worker 崩溃不会自动重启**（supervisor 只提供手动 restart；看门狗会
  告警）。引擎连续 3 次执行错误会主动 HALT——同样需要人工确认后重启。
- 重启控制台/服务器时，worker 收到 SIGTERM 会先结算在途订单再退出
  （0.1–0.3s），unit 的 TimeoutStopSec=45 已留足余量。
- 服务器上还有 nginx(80/443/8443)、docker(8080)、python(8000) 等业务，
  动防火墙时注意别误伤。

## 6. 可选硬化（非必需）

- 专用低权用户运行（当前 root；需迁移 .env/logs 属主，收益一般）。
- `requirements-live.txt` 把 lighter-sdk 固定到已知 commit（当前 1.1.2
  可用；升级前先跑测试）。
- 更新代码：`git pull` 后 `systemctl restart entropy-console`（worker 需
  在控制台 Runs 页逐个 restart 才会用新代码）。

# 运营手册：从密钥到策略启动 / OPERATIONS

> 控制台地址：`https://taoli.coinfetcher.xyz/?token=<token>`（token 见 gitignored 的 deploy/console-token.env）
> 适用：首次跑通 + 日常例行操作。服务器侧（常驻/告警/备份）见 DEPLOY.zh-CN.md。

## 阶段 0：交易所侧准备（在各交易所网页上做，与服务器无关）

| 交易所 | 要做的事 | 得到什么 |
|---|---|---|
| **Hyperliquid**（Entropy 腿） | 1. 充值 USDC 到 Hyperliquid；2. 在 app.hyperliquid.xyz/API 创建 API(agent) 钱包；3. 把资金划转到 **io dex**（Entropy 所在的 dex clearinghouse） | agent 私钥 `0x`+64位十六进制；主账户地址 `0x`+40位 |
| **Lighter Robinhood 链**（对冲腿 `lighter-rh`） | 注册 API key 并充值 **USDG**（该链以 USDG 计价）；参考 lighter-python 仓库的 key 生成方法 | `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`（整数）、`LIGHTER_API_PRIVATE_KEY` |

⚠️ 三个易错点：
1. agent 私钥 ≠ 主钱包私钥，填错等于把主钱包权限交出去；
2. Lighter **主网**和 **Robinhood 链**是两套独立账户/密钥，必须与启动时的
   `--hedge` 一致（跑 lighter-rh 就用 RH 链的 key）；
3. 两个交易所都要有钱，一腿没钱下单即失败。

资金参考：首次实盘建议两边持仓上限各 $200–300，两所合计预算 ≥ 持仓上限
的 30–50% 作保证金 + 留手续费/波动余量（以各所保证金页面为准）。

## 阶段 1：控制台填密钥（API Keys 页签）

依次填 5 项（有即时格式校验，填错会当场报错）：

| 字段 | 值 | 校验规则 |
|---|---|---|
| `HL_PRIVATE_KEY` | agent 私钥 | `0x` + 64 hex |
| `HL_ACCOUNT_ADDRESS` | 主账户地址 | `0x` + 40 hex |
| `LIGHTER_ACCOUNT_INDEX` | 整数 | 纯数字 |
| `LIGHTER_API_KEY_INDEX` | 整数 | 纯数字 |
| `LIGHTER_API_PRIVATE_KEY` | RH 链 API 私钥 | `0x` + 64 hex |

保存后写入 `.env`（0600 权限、审计日志），**永不回显**——只显示"已设置 +
尾 4 位"。填完这一步，`/api/meta` 的 `env_exists` 变 true。

## 阶段 2：建策略 profile（Strategy Config 页签）

1. 新建 profile，建议名 `sndk-rh`；Symbol 填 `SNDK`，hedge 选 `lighter-rh`。
2. 编辑器里有默认模板，**必须改的**（其余可先不动）：

```yaml
thresholds:
  midline_bps: -4.2     # 28.5h 实测中位数，Analyzer 会持续校准
  upper_bps: 6.5
  lower_bps: 5.5
entropy:
  max_position_usd: 250 # 首跑从 250 起步（模板默认 500）
  leg slippage: execution.leg_slippage_bps: 15   # 模板 50 太松，沿用调优值
hedge:
  max_position_usd: 250
sizing:
  max_order_notional_usd: 125   # 与持仓上限成比例，别低于 min 10
```

3. 保存。保存走真实 `load_config()` 校验——**能保存的配置，启动就一定通过**。

## 阶段 3：先 RECORD-ONLY 跑（Runs 页签）

1. Runs → Start：选 `sndk-rh`、Symbol `SNDK`、hedge `lighter-rh`、模式
   **RECORD-ONLY** → 启动。
2. Overview 页确认卡片出现、两侧盘口 age 正常刷新、状态 `recording`。
3. 让它跑至少数小时（隔一天更好）——数据写
   `logs/minutes-SNDK-lighter-rh.csv`。此期间关页面、关电脑都无所谓。

## 阶段 4：分析校准（Analyzer 页签）

1. 选 `sndk-rh` 跑分析：看溢价分布、当前 band 的触发频率、**建议阈值**。
2. 跑回测看往返利润与未平仓敞口。
3. 判定：建议 midline 与 -4.2 偏差 >1–2bps → regime 已变，点"应用建议
   阈值"→ 回编辑器 → 保存。偏差小则不改。

## 阶段 5：LIVE 启动（真实下单）

1. Runs → Start：同上，模式选 **LIVE**。
2. 强制二次确认：手动输入品种名 `SNDK`（输错/不输都不能启动）；密钥
   不完备会直接拒绝。
3. 启动后盯着 Overview 30–60 分钟：状态 `starting → running`、PnL、
   持仓、最近成交。之后关页面，看门狗接管（HALT/断连/崩溃会告警）。
4. 成交流水：`logs/trades-SNDK-lighter-rh.csv`。

## 阶段 6：日常例行

- 每周：Analyzer 复测 `hours=24`（阈值漂移是最大风险）；
- 告警：`/etc/default/entropy-watchdog` 填 Telegram 两项即推送；
- HALT 后：先看 Runs 页日志尾部找原因，再手动 restart；
- 服务器重启后：Runs 页手动重启 worker（console 会自起，worker 不会）；
- 想加仓：profile 里改 `max_position_usd` → 保存 → Runs 页 restart 生效。

## 快速故障对照

| 现象 | 先查 |
|---|---|
| LIVE 按钮拒绝 | API Keys 页对应 venue 三项是否齐（venus 要求见页内提示） |
| 启动即 errored | Runs 页日志尾部；多为密钥部署不匹配/市场不存在/网络 |
| 卡在 starting | 两所 ws 是否连上；服务器时间是否同步（chrony 已配好） |
| 长时间无成交 | Analyzer 看 band 是否过宽、fire 表是否为 0（正常，等溢价到位） |

## 阶段 7：分时段 band 自动校准（已上线）

系统按美东时段（盘前/盘中/盘后/休市）自动维护 band，无需人工调参：

- `entropy-autoband.timer` 每 5 分钟运行 `tools/auto_band.py`
- midline = 当前时段过去 7 天的溢价中位数；宽度 = max(2.5×时段std, 2×实测滑点, 5bps)
- 实测滑点自动从 trades CSV 统计——SNDK 实测 ~11bps，所以 band 自动放宽到 ±23bps
- 写回 profile 后引擎 **60 秒内热加载**，不重启、不打断持仓
- 数据不足 240 分钟的 profile 自动跳过（NBIS/ANTH 攒够后自动启用）
- 手动改 band 也会被引擎热加载，但 5 分钟内会被调度器按数据纠回——
  想固定参数就在 profile 里删掉 `auto_band.enabled: true`

紧急停用：`systemctl disable --now entropy-autoband.timer`

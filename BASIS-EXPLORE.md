# 跨所基差探索：Katana / Lighter / Hyperliquid

> 起点：`KATANA-2D-REVIEW.md` 的结论是 BTC(KAT-vs-HL) 往返成本 ≈10bp，
> 是基差摆动的 4–5 倍 → 套不动。于是并行探索两条路：**换标的** 与 **换对冲腿**。
> 数据窗口：2026-09-22 16:30 CST 起持续采集。
>
> **§1 的三所基差表是 2026-09-22 的 35 分钟快照**；41.5 小时全窗口复跑
> （含数据质量问题、漂移与实盘阈值结论）见 **§6**。

---

## 0. 结论（先把话说清楚）

1. **换标的不是杠杆，换对冲腿才是。** 标的间差异只有 2×；
   把对冲腿从 HL(4.5bp 吃单) 换成 **Lighter(0bp)**，往返成本降 **10×**（9.95 → 0.95bp）。
   用同一把尺子量（35 分钟窗口，`sd / 往返成本`）：

   | pair | 往返成本 | sd/toll | 备注 |
   |---|---|---|---|
   | **Lighter / Katana** | **0.95 bp** | **0.83 – 1.70** | 唯一 sd 与 toll 同量级的一对 |
   | HL / Katana | 9.95 bp | 0.14 – 0.31 | 当前 MAKER-DESIGN 的结构 |
   | HL / Lighter | 9.0 bp | 0.10 – 0.39 | 引擎今天就能跑，但 toll 太贵 |

2. **KAT 在所有 6 个标的上都是最便宜腿**（比 HL 低 6.1–13.8bp），是场所级现象，不是 BTC 特例。
3. **资金费不解释这个价差**（§3）：HL 与 KAT 的资金费几乎相同（≈3bp/日），
   而"更贵的那条腿"资金费反而更高——方向与"补偿说"相反。
4. **ETH 确实没有空间**（HL/KAT 仅 +8.0，Lighter/KAT 仅 +2.3，摆动也最小）；
   **SOL/HYPE 的错价在 HL 一侧**（HL 比 KAT 与 Lighter 同时高 11.6/13.8bp），
   而地 KAT-vs-Lighter 本身≈0 → 用 Lighter 对冲抓不到，用 HL 对冲成本又太贵。
5. **真正可做的组合是 `KAT 挂单 maker + Lighter 吃单对冲`**，
   而且**引擎今天表达不了**：基准腿被硬编码为 HL（`config.py` ~line 447）。
   这是把结论变成可执行策略的唯一阻碍。

---

## 1. 数据（累积中）

### 1.1 三所基差（分钟中位，bps；正 = 前者贵）

| 标的 | HL/KAT | Lighter/KAT | HL/Lighter（推导） | KAT OI(USD) | n |
|---|---|---|---|---|---|
| BTC | **+9.79**(2天3111min) | +4.30 | +8.12 | ~$268k | 35 |
| ETH | +8.04 | +2.31 | +5.84 | ~$503k | 35 |
| SOL | +11.57 | +1.02 | +10.49 | ~$59k | 35 |
| HYPE | +13.77 | +0.01 | +14.08 | ~$40k | 29 |
| ZEC | +6.10 | **+8.34** | −0.82 | ~$16k | 29 |
| DOGE | +11.12 | **+10.95** | +0.23 | ~$2k | 28 |

三种形态（不是一种）：

- **三方均分**（BTC）：KAT −12.2 / Lighter −7.7 / HL 0 → KAT↔Lighter 与 Lighter↔HL 各有 ~4.5bp。
- **HL 独高**（SOL、HYPE）：KAT≈Lighter（差 0.0–1.0bp），HL 比两者高 11.6–14.1bp。
- **KAT 独低**（ZEC、DOGE）：Lighter≈HL，KAT 比两者低 8.3–11.0bp。

### 1.2 摆动 vs 成本（同窗口，35min）

| 标的 | pair | median | sd | range | sd/toll | range/toll |
|---|---|---|---|---|---|---|
| BTC | lighter/katana | +4.30 | 0.79 | 3.22 | 0.83 | 3.38 |
| ETH | lighter/katana | +2.31 | 0.94 | 3.87 | 0.99 | 4.07 |
| SOL | lighter/katana | +1.02 | 1.29 | 5.16 | 1.35 | 5.43 |
| HYPE | lighter/katana | +0.01 | 1.43 | 7.55 | 1.51 | 7.95 |
| ZEC | lighter/katana | +8.34 | 1.11 | 3.86 | 1.17 | 4.06 |
| DOGE | lighter/katana | +10.95 | 1.62 | 7.52 | **1.70** | **7.92** |
| — | *对比* BTC hl/katana | +9.79 | 2.60 | 24.81 | 0.26 | 2.49 |

> `sd/toll` 才是"能不能做"的尺子，**不是基差水平**（水平不可套，见 REVIEW §3.3）。
> KAT↔Lighter 的 `sd/toll` 是 HL↔KAT 的 **4–11 倍**。

### 1.3 一个时刻的三所对账（2026-09-22 08:55 UTC，避免推导误差）

| 标的 | HL | Katana | Δ vs HL | Lighter | Δ vs HL |
|---|---|---|---|---|---|
| BTC | 86329.5 | 86224.5 | −12.16 | 86263.45 | −7.65 |
| ETH | 2753.05 | 2750.70 | −8.54 | 2751.13 | −6.99 |
| SOL | 117.855 | 117.710 | −12.30 | 117.712 | −12.18 |
| HYPE | 95.2935 | 95.1550 | −14.53 | 95.1599 | −14.03 |
| ZEC | 1498.30 | 1497.07 | −8.21 | 1498.16 | −0.94 |
| DOGE | 0.09972 | 0.09960 | −11.79 | 0.09972 | +0.50 |

三所独立印证：**Katana 恒为最便宜腿**。

---

## 2. 成本栈（实测费率）

| venue | maker | taker | 来源 |
|---|---|---|---|
| **Lighter**(mainnet) | **0 bp** | **0 bp** | `/api/v1/orderBooks` → `maker_fee/taker_fee`，6 个市场全 0 |
| **Katana** | **0.475 bp** | **1.9 bp** | `GET /v1/markets`，9 个市场一致 |
| **Hyperliquid**(主 dex) | 1.5 bp | **4.5 bp** | `POST /info {userFees}`：`userCrossRate=0.00045`，本账户 tier 0，无折扣 |

**完整往返（两条腿各过两次，不含滑点）：**

| 结构 | 往返成本 | 能否用现有引擎表达 |
|---|---|---|
| **Katana maker + Lighter taker** | **0.95 bp** | ❌ 需要基准腿可配（当前硬编码 HL） |
| HL maker + Lighter taker | 3.0 bp | ❌ HL 无 maker 契约（v1 非目标） |
| HL taker + Lighter taker | 9.0 bp | ✅ `--hedge lighter` |
| Katana maker + HL taker | 9.95 bp | ✅ 当前 MAKER-DESIGN |

---

## 3. 资金费不解释价差（实测）

| 标的 | HL 资金费(bp/日) | Katana(bp/日) | 差(HL−KAT) |
|---|---|---|---|
| BTC | +4.25 | +2.99 | +1.26 |
| ETH / SOL / HYPE / ZEC / DOGE | +3.00 | +2.99 | +0.01 |
| XRP | +5.86 | +2.99 | +2.87 |
| TAO | +19.89 | +3.41 | +16.48 |

- 若"HL 贵"是因为 HL 多头付更多资金费，那是**反向补偿**：贵的腿资金费更高，
  应该更便宜才对。实测与补偿方向相反 → **价差没有资金费基础**。
- 唯一有大 carry 的是 TAO（+16.5bp/日），但 KAT 的 TAO OI 仅 ≈$1.3k，不可交易。
- Lighter 的资金费在 `orderBooks` 里没有字段，待补（需要另找端点）。

---

## 4. 新增工具与采集

| 进程 | 标的 | 两条腿 | 产出 |
|---|---|---|---|
| console `w1` | BTC | HL vs Katana | `logs/minutes-BTC-katana.csv`（2 天+） |
| console `w6/w7/w8/w9/w10` | ETH/SOL/ZEC/HYPE/DOGE | HL vs Katana | `logs/minutes-<SYM>-katana.csv` |
| systemd `entropy-probe` | BTC/ETH/SOL/ZEC/HYPE/DOGE | **Lighter** vs Katana | `logs/minutes-<SYM>-lighter-vs-katana.csv` |

- 全部 `--record-only`，零订单。
- `tools/basis_probe.py`：任意两所分钟盘口采集（引擎表达不了 base=Lighter）。
- `tools/basis_matrix.py`：汇成三所矩阵 + 推导 HL/Lighter。
- `deploy/entropy-probe.service`：常驻采集（`Restart=always`）。
- Katana REST 快照并发 resync 会 429，feeds 内部 2s 间隔重试，1 分钟内恢复 60/60。

---

## 5. 下一步

1. **累积 24h 后重跑**（`sd/toll` 需要更长窗口才有统计意义）：
   ```bash
   python3 tools/basis_matrix.py --minutes 1440
   python3 tools/analyze.py --csv logs/minutes-BTC-lighter-vs-katana.csv --fees-bps 0.95
   ```
   ✅ 已于 2026-09-24 10:00 CST（41.5h）执行，结果见 §6。
2. **引擎解耦基准腿**（最大单点收益）：让 `entropy` 可选 lighter，
   才能交易 `Katana maker + Lighter taker`（0.95bp 门费）。
   ✅ 已实现（`--base lighter` + `profiles/lighter-btc-katana.yaml`，当前 record-only）。
3. 候选优先级（level × swing/toll × KAT 流动性）：
   **BTC**（流动性最好，sd/toll 0.83）、**HYPE/SOL**（swing/toll 1.4–1.5，KAT OI 偏小）、
   **ZEC**（level 最高 8.3bp，OI 仅 $16k）、DOGE（指标最好但 KAT OI ≈$2k，基本不可交易）。
   → 41.5h 复跑后排序不变，但 **ETH/SOL 的 35 分钟摆动实际大于 BTC**（§6.3）。
4. 给 recorder 加档位量，才能把"容量"从一次性快照变成可监控指标。

---

## 6. 41.5h 复跑（2026-09-22 16:36 → 2026-09-24 10:00 CST）

### 6.1 采集运行状况

| 进程 | 标的/腿 | 起止 | 结果 |
|---|---|---|---|
| systemd `entropy-probe` | BTC/ETH/SOL/ZEC/HYPE/DOGE **lighter/katana** | 09-22 16:36 → | 连续 41.5h 无重启；覆盖率 BTC/ETH/HYPE **100%**，SOL/ZEC/DOGE 98.7% |
| console `w1→w6` | BTC HL/KAT | 09-20 → | 100%，5565 行 |
| console `w6–w10` | ETH/SOL/ZEC/HYPE/DOGE HL/KAT | 09-22 16:30 → **09-23 10:46 手动停止** | 各 1097 行（≈18h），HL 腿对照现在只剩 BTC |
| console `w4→w5` | BTC **lighter**/katana（`--base lighter`） | 09-23 11:05 → | 95.4%（启动即晚） |

Feed 健康：41.5h 内每标的 Lighter 重连 4–6 次、Katana 1–2 次，21 条 ws error、启动时 4 次 429。

### 6.2 昨天（09-23 CST 全天）基差

premium = 前腿/后腿 − 1，已剔除单边挂单的坏分钟：

| 标的 | 腿 | 覆盖 | 中位 | sd | p05 | p95 |
|---|---|---|---|---|---|---|
| BTC | lighter/katana | 100% | **+2.65** | 1.19 | +0.76 | +4.57 |
| ETH | lighter/katana | 99.9% | +1.96 | 1.38 | −0.40 | +4.05 |
| SOL | lighter/katana | 98.7% | +0.55 | 1.45 | −1.39 | +3.14 |
| HYPE | lighter/katana | 98.5% | +1.22 | 8.69\* | −7.87 | +5.48 |
| ZEC | lighter/katana | 98.5% | +3.96 | 4.28 | −3.12 | +10.79 |
| DOGE | lighter/katana | 98.5% | +9.71 | 3.31 | +4.34 | +14.74 |
| BTC | hl/katana | 100% | **+10.37** | 2.08 | +6.10 | +13.09 |

\*HYPE 的 sd 被坏数据灌高，干净口径约 3。

### 6.3 摆动 vs 成本（往返 0.95bp = KAT maker×2 + Lighter taker 0）

| 标的 | 35min Δ sd | p95\|Δ\| | sd/toll | p95\|Δ\|/toll | \|Δ\|≥toll 的窗口 | 数据干净度 |
|---|---|---|---|---|---|---|
| BTC | 1.09 | 2.11 | **1.14** | 2.22 | 37% | 2491/2491 |
| ETH | 1.58 | 3.14 | 1.67 | 3.31 | 54% | 剔 1 分钟 |
| SOL | 1.80 | 3.60 | 1.89 | 3.79 | 57% | 全干净 |
| HYPE | — | — | ~3.7 | ~7.3 | — | 剔 46 分钟，上界 |
| DOGE | 3.58 | 6.93 | 3.77 | 7.29 | 78% | KAT OI ≈$2k，不可交易 |
| ZEC | 5.40 | 11.28 | 5.61 | 11.68 | 84% | KAT OI ≈$16k |

**水平会漂移，不是常数**（6 小时块中位，bps）：

| 标的 | 22/08Z | 22/14Z | 22/20Z | 23/02Z | 23/08Z | 23/14Z | 23/20Z |
|---|---|---|---|---|---|---|---|
| BTC | +4.57 | +2.32 | +1.63 | +3.51 | +2.99 | +3.26 | +3.23 |
| ETH | +3.44 | +1.87 | +1.43 | +2.57 | +2.20 | +2.23 | +1.78 |
| SOL | +2.26 | +0.51 | +0.13 | +0.38 | +1.24 | +1.76 | +0.26 |
| ZEC | +9.72 | +4.96 | +4.39 | +6.14 | +0.88 | +3.78 | +2.64 |
| DOGE | +10.85 | +8.33 | +7.12 | +9.70 | +12.39 | +7.39 | +5.78 |
| HYPE | +2.37 | +0.74 | −1.63 | +2.18 | +2.53 | +0.61 | +1.01 |

→ 只能靠 `auto_band` 按时段重算 midline，静态阈值最多撑几天。

### 6.4 数据质量问题（三类，必须先过滤）

1. **Katana 单边挂单**：薄盘偶尔只留一张远离的 ask，mid 溢价瞬间 ±200~2000bp。
   实测 HYPE 46 分钟、ZEC 3、ETH 1、DOGE 2 分钟被污染；顶档价差 79–4027bp，
   而正常盘口 p99 < 20bp。例：ZEC 09-23 00:20Z `bid 1612.5 / ask 1070` → +2030bp。
   影响：HYPE raw sd 19.7 → 3.2，ZEC 63 → 4.7（不修就没法标定阈值）。
2. **Katana 报价冻结**：HYPE 09-23 15:28–15:33Z bid/ask 6 分钟钉在 94.9/94.91，
   Lighter 从 94.30 跌到 93.77 → 记录出 `buy_edge_max = 121bp` 的"僵尸报价"信号。
3. **Katana 侧 20 分钟空档**：09-23 11:30–11:50 CST，DOGE/SOL/ZEC 同时缺行，
   与 HYPE 异常同时段，是 Katana 盘口的一次事件（probe 无报错，属静默 staleness）。

**修复**：`MinuteRecorder` 增加 `max_spread_bps`（默认 50，profile 的
`recorder.max_spread_bps` 可覆盖；`tools/basis_probe.py --max-spread-bps` 同）：
顶档价差超限的 1s 采样直接丢弃，整分钟都超限就不写行。probe 与 BTC 采集器已重启应用。

### 6.5 校准结论

- `tools/analyze.py --fees-bps 0.95`（≈10% 分钟触发）：**BTC midline 3.1 / upper 1.5 / lower 1.5**。
- 线上 profile 原来是 `midline 3.65 / ±5`（`auto_band.min_width_bps: 5.0` 把宽度钉死），
  实测每边一天只开火 ~0.6 次。**已把 `min_width_bps` 降到 2.0**，运行期 auto_band
  现给出 `midline +3.62 / ±2.00`（off 时段 σ≈0.7，宽度由 floor 主导），每边约 20–50 次/天。
- HL 腿对照再次确认不可做：BTC `hl/katana` 中位 +10.4bp vs 往返 9.95bp；
  推导 `hl/lighter` 中位 +5.7bp。
- 运维顺带修复：`deploy/watchdog.sh` 自 09-20 起卡在 `console-down`（unit 改用
  `EnvironmentFile` 后 grep 到的是字面量 `${ENTROPY_CONSOLE_TOKEN}`，Bearer 401），
  已改为按 env → EnvironmentFile → 字面量 顺序解析 token 并验证恢复告警。


# 自动套利标的探测（Discovery）规划

> 目标一句话：**给出一个币种，系统自动发现在哪些交易所可交易、持续采集全部
> 交易所的盘口、自动算出任意两个交易所之间的套利空间，并把达标配对推进到
> 观察策略** —— 人只做最后一步"是否上实盘"的决策。

---

## 1. 现状与问题

当前探测一个新标的的流程，每一步都靠人：

```
人脑选定 (symbol, venueA, venueB)
  → 手写 tools/basis_probe.py --a A --b B --symbols S   （或建 profile 跑 record-only）
  → 跑几天
  → 人跑 tools/basis_matrix.py / tools/analyze.py 看数字
  → 人手写 profile + thresholds
  → 人从 console 起 worker
```

痛点：

1. **配对是人选的**。6 个 base 场所（`hl / lighter / lighter-rh / katana /
   backpack / bulk`，`tradexyz` 复用 HL 通道）意味着每个币种理论上有
   **15 个无序配对（30 个有向）**，靠人猜哪一对有边际，覆盖不全且带偏见。
2. **采集拓扑浪费**。`basis_probe.py` 按配对建流：每对 2 条 ws。若真要覆盖
   15 对，一个币种要 30 条连接，每个场所的同一本账被重复订阅 5 次——而
   Katana REST 快照本身有 429 限速（`entropy-probe.service` 注释里已踩过坑）。
3. **分析与晋级脱节**。数据落成 CSV 之后，"哪对值得做"没有机器结论，更没有
   自动接到已有的 profile / experiment / supervisor 体系上。

已有资产（本规划全部复用，不重造）：

| 资产 | 位置 | 在本方案中的角色 |
|---|---|---|
| 每所市场目录解析（含费用/tick/minNotional） | `tools/basis_probe.py: resolve()` | 抽成共享库，作为发现层 |
| 六所盘口 feed | `entropy_arb/feeds.py` | 星型采集直接用 |
| 分钟条聚合（含 wide-spread 防幻影过滤） | `entropy_arb/recorder.py` | 派生出单所版本 |
| 配对溢价/可执行价差统计 | `entropy_arb/analysis.py`、`tools/basis_matrix.py` | 全配对评分的核心公式 |
| 会话感知 band 自动标定 | `entropy_arb/autoband.py` | candidate 的阈值提案 |
| 策略生命周期（draft→…→observing）与审批门 | `entropy_arb/console/experiments.py` | 晋升管道的承接端 |
| worker 生成/看护、profile 管理 | `entropy_arb/console/supervisor.py` / `profiles.py` | 观察任务的执行端 |
| 可解释推荐规则 | `entropy_arb/console/recommendations.py` | 探测结论的出口之一 |

---

## 2. 核心设计决策

### 2.1 探测层与执行层分离

`engine.py` 一进程跑一个 (base, hedge) 配对，是**执行层**，不动它。本方案在
其上加一个**探测层**：无凭证、只读公共行情、常驻。探测层的产品是"哪个配对
值得建策略"的结论，执行层消费这个结论。两层之间通过 profile + console 的
既有机制衔接。

### 2.2 星型采集，不是两两配对（本方案的关键）

对一个币种 S，**每个上市场所只建 1 条 feed**，落盘"单所分钟条"；所有
N×(N−1) 个配对溢价在分析期由单所条**推导**：

```
                    ┌─ minutes-S-@hl.csv
   S @ 6 venues ──► ├─ minutes-S-@lighter.csv        分析期两两 join：
   6 条连接         ├─ minutes-S-@katana.csv          premium(a,b) = mid_a/mid_b − 1
                    └─ ...                            sell_edge = bid_a/ask_b − 1 ...
```

对比两两配对：

| | 两两配对 probe | 星型采集 |
|---|---|---|
| 连接数 / 币种 | 30（15 对 × 2） | ≤ 6 |
| 新接入第 7 所 | 手工补 6 对 probe | 自动多 1 条 feed，6 个新配对**回溯免费出现** |
| 数据一致性 | 两个进程各写一份，可能交错 | 同一分钟桶对齐，同源推导 |
| Katana 429 限速 | 5 份重复快照压力 | 1 份 |

`basis_matrix.py` 已经在用同样的思想（HL/Lighter = (HL/KAT)/(Lighter/KAT)
的派生腿），本方案把它从"事后补算一条腿"升级为"默认拓扑"。

### 2.3 自动化的边界：探测全自动，实盘仍须人批

延续仓库哲学（no paper mode、experiment apply 有 409 版本门）：
**发现 → 采集 → 评分 → 生成 candidate（含阈值提案）全自动；从 candidate
到 live 的那一步，永远是人点确认。**

---

## 3. 架构：四层

```
┌─────────────────────────────────────────────────────────────────┐
│ L0 市场目录层  entropy_arb/discovery.py                          │
│   list_markets(venue) / resolve(venue, symbol) → 目录缓存        │
│   输入: symbol 或 universe → 输出: 该币种的活跃上市清单+元数据    │
├─────────────────────────────────────────────────────────────────┤
│ L1 星型采集层  tools/star_probe.py（常驻服务）                    │
│   watchlist 文件/console API 驱动；每 (venue,symbol) 1 feed      │
│   落盘单所分钟条 minutes-<SYM>-@<venue>.csv（含盘口深度快照）     │
├─────────────────────────────────────────────────────────────────┤
│ L2 全配对分析层  entropy_arb/pair_matrix.py                      │
│   join 单所条 → 每个有向配对的 premium/edge/费用后净边际/命中率   │
│   → 评分排名 → 状态机 dead/watch/candidate + sqlite 历史         │
├─────────────────────────────────────────────────────────────────┤
│ L3 晋升管道层  console Discovery tab + /api/discovery/*          │
│   candidate → 自动建观察(record-only worker)或仅出推荐           │
│   → autoband 阈值提案 → experiment(draft→ready→…) → 人批 → live │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. 各层详细设计

### L0 市场目录层 — `entropy_arb/discovery.py`

把 `basis_probe.py` 里的 `resolve()` / `_CACHE` / 各所市场列表逻辑原样抽到
包内（`basis_probe.py` 改为薄包装调用它，行为不变），并补一个面向"发现"的
接口：

```python
async def list_markets(session, venue) -> list[MarketListing]
# MarketListing: symbol, status(active/TRADING/Open), market(本地名),
#                taker_fee_bps, maker_fee_bps, tick, step, min_notional,
#                quote_asset(USDC/USDG/USD)

async def universe(session, symbol: str,
                   venues=ALL_VENUES) -> UniverseReport
# 对每个场所解析一次；产出:
#   listings: [MarketListing...]        # 哪些所可交易
#   missing:   {venue: 原因}            # 未上市/停牌/状态异常
#   pairs:     [(a,b) ...]              # 可行配对（含 quote 资产提示）
# CLI: python3 -m entropy_arb.discovery --symbol DOGE
```

要点：

- HL 的 perp 清单走 `info {type: "meta"}`；tradexyz 视作 `hl` 的一个 dex
  变体（沿用 `entropy.dex` 惯例），目录里标记为同通道不同市场。
- 名字映射沿用 probe 的 `A:B` 惯例（如 `SNDK.US:SNDK`）。
- 目录结果带 TTL 缓存（默认 10 min），落 sqlite（console storage 新表
  `market_listings`），console 的 Venues tab 顺便受益。

### L1 星型采集层 — `tools/star_probe.py`

新常驻进程（替代两个现有 probe systemd unit 的角色，旧 unit 保留兼容、
逐步合并）：

```
python3 tools/star_probe.py --watchlist discovery-watchlist.yaml
# watchlist 条目: symbols: [DOGE, WIF, ...], venues: all(默认)/子集,
#                 depth_levels: 3, max_spread_bps: 50
```

- 复用 `feeds.py` 的六个 BookFeed；每 (venue, symbol) 一条 feed、一个
  `OrderBook`。
- **单所分钟条**（新 `VenueMinuteRecorder`，从 `MinuteRecorder` 抽公共逻辑）：

  ```
  minutes-<SYM>-@<venue>.csv
  minute_ts, time_utc, bid, ask, bid_sz1..3, ask_sz1..3,
  mid, spread_bps, samples
  ```

  文件名里的 `@` 与现有两类命名（engine 的 `minutes-SYM-venue.csv`、probe
  的 `minutes-SYM-a-vs-b.csv`）明确区分，`basis_matrix.py` 的 glob 不会误读。
- 记 top-3 档量，是为了 L2 能回答"过费 hurd­le 的价差**吃不吃得到
  minNotional×k**"——只记 top-of-book 会把容量问题留到实盘才暴露。
- wide-spread 过滤沿用 recorder 的做法与默认值（防幻影溢价）。
- 崩溃自愈：feed 级 watchdog（某条流 stale > N 秒即重建，Katana 限速退避
  沿用 feeds 内部 2 s 节流）。
- systemd：`deploy/entropy-star-probe.service`，watchlist 热加载
  （文件 mtime 变化即增删 feed，不重启进程、不动已写 CSV）。

### L2 全配对分析层 — `entropy_arb/pair_matrix.py`

纯 CPU over 单所分钟条（与 `analysis.py` 同风格，JSON-safe 输出）：

```
python3 -m entropy_arb.pair_matrix --symbols DOGE --window 72h
python3 -m entropy_arb.pair_matrix --json            # console/机器人消费
```

对每个有向配对 (a,b)，在共同新鲜的分钟桶上计算：

| 指标 | 定义 |
|---|---|
| premium 中位/sd/p05/p95 | `(mid_a/mid_b − 1)` 分布 |
| 净 sell edge | `bid_a/ask_b − 1 − fee_a(taker) − fee_b(taker)`，取 p95 与超阈命中率 |
| 净 buy edge | `bid_b/ask_a − 1 − 双边费`，同上 |
| 往返潜力 | 两方向净 hurdle 之和（对应 config 的 upper+lower 逻辑） |
| 容量可行性 | 深度折算的可成交名义 vs 两边 minNotional、tick/step 可撮性 |
| 会话切分 | 复用 `autoband.session_of`，per-session 中位（美股盘前/盘中对股票类标的很重要） |
| 数据新鲜度 | 沿用 basis_matrix 的 age 报告，陈旧配对不得伪装成当前 |

**评分与状态机**（阈值进 watchlist 配置，落 sqlite 表 `pair_scores`，每次
评分存一行历史）：

```
dead      净 edge p95 < 双边费 × 1.2，或样本/新鲜度不足
watch     有信号但命中率/容量不足，继续采
candidate 净往返潜力 ≥ 阈值 且 命中率 ≥ 阈值 且 容量可行 且 数据满最小窗口
```

**配对 CSV 合成**（兼容层）：对 candidate 配对，按 recorder 的 16 列 schema
现合成 `minutes-<SYM>-<a>-vs-<b>.csv`（由两份单所条 join 而来）——于是
`tools/analyze.py`、`autoband`、console Analyzer tab **零修改**可直接用。

### L3 晋升管道层 — console 集成

- 新 API（沿用现有 auth/audit 中间件）：
  - `GET/PUT /api/discovery/watchlist` — 币种清单管理
  - `GET /api/discovery/universe?symbol=X` — L0 结果（上市所+费用+min）
  - `GET /api/discovery/matrix?symbol=X` — L2 配对矩阵与状态
  - `POST /api/discovery/pairs/<a>/<b>/promote` — 生成 candidate 工件
- promote 做三件事（全部幂等、有审计）——**已确认：全自动，不需要人工再确认这一步**：
  1. 由模板生成观察 profile `<sym>-<a>-vs-<b>-obs.yaml`（record-only、
     小仓位上限、阈值来自 autoband 提案），过**真实 `load_config` 校验**；
  2. 自动起 record-only worker（supervisor 现有机制，无凭证）；
  3. 建 experiment（draft，附 autoband 阈值提案与评分证据快照）。

### Discovery tab — 探索过程 / 进度 / 结论可视化（已确认需求）

UI 回答三个问题，对应三个区块：

1. **过程（Process）**——"系统现在在探索什么"：
   - watchlist 币种卡：每币种显示 L0 发现结果（各所上市 ✓/✗ + 原因）、
     活跃 feed 数、连接健康（stale 计数）、当前采集时长；
   - scanner 心跳：star_probe 的状态快照（每 feed 的 last sample 年龄、
     重建次数、wide-spread 过滤计数）。
2. **进度（Progress）**——"离结论还有多远"：
   - 每币种 × 每配对的评分进度条：已积累样本 / 最小窗口（24h 初评、72h
     稳定评）、新鲜度 age、当前状态徽章（dead/watch/candidate，(candidate)
     额外标注连续达标窗口数）；
   - 15 对矩阵热力图视图（按净往返潜力着色），点开看单配对明细。
3. **结论（Verdicts）**——"系统认为哪里有钱赚"：
   - 结论表：配对、净 edge p95、命中率、容量判定、证据时间窗、**缺失项
     （missing）**——沿用 recommendations 的"说不出就不说"原则，数据不够
     的配对只报进度不报结论；
   - 晋升动作留痕：candidate → 自动建的 obs worker / experiment 链接、
     评分证据快照（哪份 CSV、哪个窗口、什么阈值提案），可回溯。
   - 实时性：配对矩阵用轮询（60s）；feed 心跳用 console 已有的 ws 桥接
     模式（复用 worker_ws 的 pump 思路，或降级为 10s 轮询）。
- console 新 **Discovery tab**：输入币种 → 上市矩阵 → 配对评分表（可按净
  edge/命中率排序）→ 状态流转按钮。`recommendations.py` 增加发现类规则
  （如 `discovery-candidate-ready`：连续 3 个评分窗口为 candidate 且数据
  无缺口 → 建议激活，missing 字段列明还缺什么）。

---

## 5. 生命周期与调度

```
人: 报一个币种 (console / watchlist 文件 / CLI)
 └► L0 立即: 哪些所有、费用/精度/minNotional、可行配对数
 └► L1 分钟级: 星型采集开始（≤6 条连接）
 └► L2 小时级: 全配对评分入 sqlite；首次满 24h 出初步结论，72h 出稳定结论
 └► L3 天级: candidate 自动 promote → record-only worker + experiment(draft)
             人批 → live；连续 N 窗口 dead 的配对自动降级停采（省连接）
 新所接入: 实现 feed + discovery.list_markets → 所有历史币种的配对自动出现
```

评分调度放 console 进程内（asyncio 周期任务，与 `_valuation_loop` 同风格），
无 console 时由 systemd timer 跑 `python3 -m entropy_arb.pair_matrix`。

---

## 6. 安全边界

1. L0–L2 **零凭证**：只碰公共 REST/WS。
2. promote 产生的 worker 一律 record-only，同样无凭证。
3. live 门槛不放松：experiment apply 的版本门（409）与人审不变；
   recommendations 保持"只解释、不执行"。
4. 资源上限：watchlist 币种数 × 场所数受连接预算约束（配置项
   `max_feeds`，默认 24），超限时按优先级裁剪。

---

## 7. 里程碑

| # | 内容 | 交付物 | 验收 | 状态 |
|---|---|---|---|---|
| M1 | L0 市场目录 | `entropy_arb/discovery.py`、CLI | `--symbol DOGE` 打印全部上市所+元数据 | ✅ 已实测（DOGE→4所6对；ANTH 走 `hl:io` 别名→2所） |
| M2 | L1 星型采集 | `tools/star_probe.py`、`entropy_arb/venue_bars.py`、systemd unit、watchlist 热加载 | 单进程采集一个币种多所；与旧 probe 并跑统计一致 | ✅ 代码+测试 |
| M3 | L2 全配对评分 | `entropy_arb/pair_matrix.py`、配对 CSV 合成、matrix.json/history | 对采集币种输出配对排名；合成 CSV 过 `tools/analyze.py` | ✅ 代码+测试 |
| M4 | L3 console 集成 | `/api/discovery/*`、Discovery tab、自动 promote 管道 | 输入币种→candidate→自动 obs worker+experiment 草案全链路 | ✅ 端到端测试通过 |
| M5 | 运维闭环 | console 定时评分循环、dead 连续 3 轮自动停 obs worker、`basis_probe` 换用共享目录（延后） | 断电重启自动恢复；持续产出 candidate | ✅ 循环+降级已实现 |

实施注记（与原规划的偏差）：

- `basis_probe.py` 保持原样未改薄（两个生产 probe 正在跑，避免重启风险）；
  M5 后期在 star_probe 稳定并跑一周后做切换下线。
- engine 表达力约束进入了实现：`hl↔hl:io` 这类 hl 系内部配对 engine 无形状，
  promote 返回 422 `engine_gap`，UI 明示"只测量不晋升"。
- recommendation 规则（`discovery-candidate-ready`）延后：Discovery tab 的
  结论区已直接承载同一信息，规则等 tab 跑一段时间后按真实误报率再定。

---

## 8. 决策点（已确认）

1. **candidate 的自动化程度** → **A：自动建 record-only worker +
   experiment(draft)，人只批 live**（无凭证、零资金风险，闭环完整）。
2. **采集深度** → **top-3 档**（容量评估必需）。
3. **评分窗口** → 默认 **72h 滚动 + 会话切分**；24h 出初评（UI 标记
   provisional）；股票类标的（.US 后缀）建议 5 个交易日。
4. **watchlist 来源** → 先纯手工（console/文件），M5 后再评估按成交量
   top-N 自动补位。

# Maker 腿扩展设计方案（v1）

> 状态：**已评审定稿**（§12 五项决策已确认）· 2026-09-20
> 前置依赖：Katana 适配器已合并（`venue_katana.py`，EIP-712 签名经 ethers 交叉验证）
> 数据依据：BTC(katana) 采集 profile，首日 ~1.5h 分钟数据（`logs/minutes-BTC-katana.csv`）

---

## 0. 目标与非目标

**目标**：让引擎支持「薄腿挂单（maker）+ 深腿吃单对冲（taker）」的模式——在 Katana
以 post-only 方式挂单吸收散户流，成交瞬间在 HL 主网吃单对冲，锁定结构性溢价减去
成本后的净边际；库存通过对称报价自然了结。

**非目标（v1 明确不做）**：
- 不做双所同时挂单的双边做市（只做单边 maker + 对冲）
- 不做 HL 侧 maker（HL 的 post-only 与用户事件流留给 v2）
- 不做与现有 taker-taker band 策略同时运行（**已决策：互斥**，见 §6/§12）
- 不做自动资金调拨、不做组合保证金优化

**范围说明（扩展性原则）**：v1 的 maker 能力只在 Katana 适配器上落地，但引擎侧
（`_maker_loop`、报价模型、状态机、安全梯、配置面、观测指标）**不含任何交易所
知识**——交易所差异全部封装在 §5 的 maker 契约后面。后续接入新交易所（如
Backpack）= 实现契约 + 通过合规测试套件，引擎与安全设计零改动（详见 §5.3）。

---

## 1. 事实依据

### 1.1 市场数据（实测，2026-09-20 05:14–06:42 UTC）

| 指标 | 实测值 | 含义 |
|---|---|---|
| 溢价中枢（HL/KAT 中间价） | **+7.34 bp**，std 0.68 bp | 结构性、极稳、缓慢衰减 |
| 卖向可成交 edge 峰值 | 100% 分钟越 6.4bp 费用线，净均值 +1.8bp | taker-taker 开仓向"常在" |
| 买向（平仓向）edge | 全程为负（max −3.5bp） | taker-taker 无法盈利了结 → 不可交易 |
| Katana 盘口 | 常态 1 档/边，±25bp 深度中位 **0.10 BTC ≈ $8k** | 容量天花板：单笔几十美元~百美元级 |
| Katana OI / 日量 | 2.3 BTC / $2.4M（HL：41k BTC / $1.44B） | 我们自己就是市场，仓位上限必须小 |
| 两所资金费 | 完全相同（均 +0.01%/8h，贴利率下限） | 排除资金费解释；溢价=做市费用均衡 |

**经济性**（taker-taker vs maker-taker 成本栈）：

```
taker-taker:  KAT taker 1.9 + HL taker 4.5 + 双边滑点 ≈ 6.9bp+   → 费后为负
maker-taker:  KAT maker 0.475 + HL taker 4.5 + 单边滑点~0.5 ≈ 5.5bp
              且 maker 赚价差而非付价差 → 每成交单位净锁 ≈ 7.4 − 5.5 ≈ +1.9bp
容量估计:     吃下 Katana 日流 10–20%（$240–480k/日）× ~2bp ≈ $45–90/日
```

### 1.2 机制事实（全部已验证：API 实测 / 官方文档 / SDK 源码）

| 能力 | 依据 |
|---|---|
| post-only 挂单 | GTX time-in-force；若会交叉则整单被拒（error `TIME_IN_FORCE`），语义即"绝不吃单" |
| 挂单/撤单 | `POST /v1/orders`（同步响应 orderId+status）；`DELETE /v1/orders` 支持**按 wallet / wallet+market 原子批量撤单，仅消耗 1 次速率配额** |
| 成交实时回报 | 鉴权 WS `orders` 订阅：任何状态变化实时推送，消息自带累计成交量 `z`（天然幂等）、`F` 明细数组（逐笔价/量/费/方向） |
| WS 鉴权 | `GET /v1/wsToken`（单次有效 token，User Data scope）→ subscribe 请求带 `token` 字段 |
| 签名复用 | 挂单与 taker 同一条 EIP-712 路径（仅 `timeInForce` 枚举 2→1(gtc)/1(gtx) 差异）；撤单走 HMAC+钱包签名（与下单同构） |
| 免费测试环境 | sandbox（api-perps-sandbox / Bokuto 测试网，解锁钱包自动发测试 vbUSDC） |
| 数值规整 | 8 位定宽小数字符串；stepSize/tickSize 由 markets 端点下发（BTC: 0.0001 / $1） |

**结论：不需要任何交易所侧的新能力，全部是已有 API 的组合。可行性无外部依赖。**

---

## 2. 总体架构

### 2.1 角色模型

```
┌────────────────────────────────────────────────────────────────┐
│ Engine (现有 engine.py 扩展，不新建进程)                          │
│                                                                │
│  maker venue (Katana)                hedge venue (HL main)      │
│  ┌──────────────────────┐            ┌──────────────────┐      │
│  │ KatanaBookFeed (有)   │            │ HLBookFeed (有)   │      │
│  │ KatanaOrdersFeed (新) │──fill事件──▶│                  │      │
│  │  place_maker (新)     │            │  send_taker (有)  │      │
│  │  cancel_all   (新)    │            │  IOC+滑点保护     │      │
│  └──────────────────────┘            └──────────────────┘      │
│         ▲ 锚定报价                              ▲ 对冲          │
│         │                                       │              │
│  ┌──────┴───────────────────────────────────────┴──────────┐   │
│  │ _maker_loop (新任务)                                     │   │
│  │  报价计算 → 挂单 → [成交事件 → 对冲批次] → 重报价循环        │   │
│  └──────────────────────────────────────────────────────────┘  │
│  复用: venue locks / 限频预算 / 断连暂停 / reconcile /          │
│        MTM / recorder / status / dashboard / web               │
└────────────────────────────────────────────────────────────────┘
```

- **maker venue** = 溢价发生器（薄、贵、被锚定）；**hedge venue** = 流动性来源（深、便宜、被吃单）
- **角色映射：maker 角色固定落在 hedge venue 上**（`--hedge katana` 时 Katana 是
  maker，基准腿 HL 是 taker 对冲腿）。未来 `--hedge backpack` 即 Backpack 担任
  maker，CLI 与配置面不变
- 适配器通过类属性 `maker_capable = True` 声明能力；引擎在 `maker.enabled` 启动时
  校验角色分配——不具备契约的所**启动即报错**，而不是运行中才失败
- v1 角色由配置固定。接口按角色定义而非按交易所，后续任何满足 §5.3 准入清单的
  交易所都能担任 maker

### 2.2 模式开关

`maker.enabled: true` 时：
- `_strategy_loop`（taker band 扫描）**不启动** —— 两策略争同一预算/仓位，v1 互斥
- `_maker_loop` 启动；其余任务（recorder/status/reconcile/balance/keepalive）不变

---

## 3. 报价模型（核心公式）

### 3.1 锚定原则

**锚定对冲腿的可成交价，而非中间价。** 我们锁的是"如果现在成交，对冲能拿到什么价"，
中间价只是近似。锚自适应当前盘口 → 溢价中枢漂移被自动吸收（无需静态 midline）。

### 3.2 双边报价公式

设 `C` = 总成本(bp) = maker_fee + hedge_taker_fee + hedge_slippage_est
（当前实测 C ≈ 0.475 + 4.5 + 0.5 = **5.5bp**，配置化），
`E` = 目标净锁利 `maker.edge_bps`（默认 2.0bp），
`K` = 加仓侧库存加价（§3.3）。

**挂买单**（吸收卖流 → 买 KAT，卖 HL 对冲；对冲可得价为 `hedge_bid`）：

```
locked_edge = hedge_bid / P_bid − 1 − C          （要求 ≥ E + K）
P_bid = hedge_bid / (1 + (C + E + K)/1e4)        即比 HL 买一低 7.5bp+K
```

**挂卖单**（卖 KAT，买 HL 对冲；对冲成本价为 `hedge_ask`）：

```
locked_edge = P_ask / hedge_ask − 1 − C          （要求 ≥ E + K）
P_ask = hedge_ask × (1 + (C + E + K)/1e4)        即比 HL 卖一高 7.5bp+K
```

以当前数据代入（E=2, K=0）：P_bid ≈ HL买一 × (1−7.5bp)。实测 Katana 自身买一约在
HL 买一低 ~6.5bp 处 → **我们的买单比盘口更激进、仍在公允便宜侧**，会优先成交；
这就是 +7.4bp 结构性溢价转化为挂单优势的机制。

数量：`qty = maker.size_base`（如 0.005 BTC ≈ $400），受 `max_order_notional_usd`
与双边仓位上限（`cap_usd`）约束（沿用 `_headroom` 语义）。

### 3.3 库存偏移（复用 inventory ladder 语义）

沿用现有 `inventory.scale_bps / floor_frac` 参数，但作用于**报价偏移**而非入场阈值：

```
u = |position| × mid / cap_usd
ramp(u) = 0                                   u ≤ floor_frac
        = scale_bps × (u−floor)/(1−floor)     u > floor_frac（线性到满仓）
K_加仓侧 = ramp(u)；K_减仓侧 = 0（v1 不做减仓优惠，保守起步）
```

库存超 `floor_frac` 后加仓侧报价自动更贪，天然减速单边堆积——与 taker 模式的
库存阶梯完全同构，参数可共用。

### 3.4 重报价策略（事件驱动 + 心跳）

| 触发 | 条件 | 动作 |
|---|---|---|
| 锚移动 | `|anchor_new − anchor_quoted| > requote_bps`（默认 1.0bp） | cancel+replace |
| 心跳 | 挂单年龄 > `requote_sec`（默认 30s） | cancel+replace |
| 成交 | fill 后剩余量 < 50% 目标量 | replace 补量 |
| 库存 | K 值变化 > 0.5bp | replace |

**预算核算**（实测：BTC tick $1 ≈ 0.12bp，HL 中间价分钟级波动 ±2–4bp → 1bp 锚移动
约 10–60s 一次）：心跳 30s + 事件触发 → 每边 ≤2–4 次/分钟 cancel/replace。
Katana 批量撤单按 1 次配额计，`max_orders_per_min: 30` 下占用 ~8–10，给对冲与
重试留 2 倍余量。**预算不足时优先降心跳频率，绝不跳过对冲。**

---

## 4. 状态机

### 4.1 单边（bid/ask 各一）

```
IDLE ──place ok──▶ QUOTING ──┬─ 锚移动/心跳/补量 ─▶ CANCELLING ─▶ QUOTING
                             ├─ GTX 被拒(会交叉) ──▶ 等锚回移后重试（正常现象，非错误）
                             ├─ 交易所强制撤单(ec) ─▶ 记日志，按新锚重挂
                             └─ FILL 事件 ─────────▶ FILLED_DELTA 累积
FILLED_DELTA ──hedge_batch_ms 窗口──▶ HEDGING ──ok──▶ QUOTING(补量)
                                        └─失败─▶ RETRY(退避,≤3) ─▶ EXPOSED(告警)
```

### 4.2 全局安全状态

| 状态 | 触发 | 动作 |
|---|---|---|
| HEDGE_BLIND | hedge 盘口 stale（复用 `staleness_sec`） | **立即撤全部 maker 单**，HOLD |
| HEDGE_DOWN | hedge venue 不可达（复用 `_venue_down`） | 同上 |
| MAKER_WS_DOWN | orders WS 断连 | 撤全部单（撤单走 REST 不受影响），HOLD 至 WS 恢复 |
| EXPOSED | 对冲重试穷尽 | 停新报价；沿用现有 `_maybe_hedge`/reconcile 兜底；持续告警 |
| RECONCILE_GAP | 链上/本地仓位偏差超容忍 | 暂停报价 + 采用链上（现有逻辑） |

**核心不变式：任何时刻只要对冲腿失明/失效，maker 单必须在数百毫秒内消失。**
宁可错过成交，不可裸露敞口。

---

## 5. Maker 契约（venue-generic 接口层）

### 5.1 新增方法（仅 maker 角色需要实现）

```python
async def place_maker(self, *, is_buy: bool, qty: float, limit_px: float) -> dict:
    """post-only (GTX) limit；同步响应。返回 {order_id, status, err}。
    status ∈ {open, canceled(GTX被拒→err='TIME_IN_FORCE'), ...}"""

async def cancel_orders(self, order_ids: list[str] | None = None) -> dict:
    """order_ids=None 且 market 给定时 → 按 wallet+market 原子批量撤单（1 次配额）。
    返回 {ok, canceled: n, err}"""

def on_fill(self, cb) -> None:
    """注册成交回调；事件 shape：
    FillEvent{order_id, client_order_id, side, qty_delta, px, fee, ts,
              status, update, error_code}"""
```

`ready_to_trade()`（maker venue）升级为：签名就绪 **且** orders WS 已连接
（对齐 LighterVenue 的 `AccountOrdersFeed.ready` 模式）。

### 5.2 Katana 实现要点

- `place_maker`：复用 `send_taker` 的 HMAC+EIP-712 管线，仅三处差异——
  `timeInForce: "gtx"`（签名枚举 1）、无 `reduce_only`、响应解析保留 orderId
- `cancel_orders`：`DELETE /v1/orders`，body `{parameters:{nonce,wallet,
  (orderIds)|(market)}, signature}`；批量形态优先
- **KatanaOrdersFeed**（新，模板=Lighter 的 `AccountOrdersFeed`）：
  1. `GET /v1/wsToken`（HMAC）取单次 token
  2. 连 `wss://websocket-perps.katana.network/v1`，subscribe `{method:"subscribe",
     subscriptions:[{name:"orders"}], token}`
  3. 消息→FillEvent 映射（幂等靠累计量 z）：

| WS 字段 | FillEvent | 说明 |
|---|---|---|
| `i` / `c` | order_id / client_order_id | |
| `s` / `X` / `x` | side / status / update | |
| `z` | （引擎按 order_id 记 z_prev） | `qty_delta = max(0, z − z_prev)`，天然幂等 |
| `F[].p/q/f` | px / qty / fee | 无 F 时退用 `v`(avgExecutionPrice) |
| `ec/em` | error_code | 强制撤单原因（GTX 交叉=TIME_IN_FORCE 等） |

  4. token 过期/断连 → 重连重取 token（退避复用现有 feed 模式）

### 5.3 新交易所接入指引（扩展性设计）

**设计保证：引擎侧零交易所知识。** `_maker_loop`、报价数学、对冲路径、安全梯、
配置面、观测指标全部只依赖 §5.1 的三个方法 + FillEvent 形状。接入一个新 maker
交易所的全部工作 = 写一个适配器（现有标准 venue 接口 + §5.1 契约）+ 通过合规
测试套件（§10）。`engine.py` 不改一行。

**能力准入清单**——候选交易所必须逐项验证（以 Backpack 为例的初始评估状态；
Backpack 项尚未核实，接入时按同一清单逐项打勾）：

| # | 能力 | 为什么必须 | Katana | Backpack |
|---|---|---|---|---|
| 1 | post-only / GTX（或等价"挂单时刻绝不成交"语义） | maker 经济学的前提，杜绝意外吃单 | ✅ GTX，会交叉整单拒 | ⏳ 待验证 |
| 2 | 原子批量撤单（按账户/市场，低速率成本） | P0 安全不变式的执行手段 | ✅ `DELETE /v1/orders` 仅耗 1 配额 | ⏳ 待验证 |
| 3 | 实时成交回报（鉴权 WS），带幂等键 | fill→对冲的延迟、正确性与去重 | ✅ `z` 累计量 + `F[]` 明细 | ⏳ 待验证 |
| 4 | maker 费率显著低于 taker | 成本栈成立的经济前提 | ✅ 0.475 vs 1.9 bp | ⏳ 待验证 |
| 5 | 挂单状态 REST 查询 | reconcile 与断连恢复兜底 | ✅ `GET /v1/orders` | ⏳ 待验证 |
| 6 | 测试环境（testnet/sandbox） | E2E 验收的前置条件 | ✅ Bokuto sandbox 免费测试金 | ⏳ 待验证 |
| 7 | 足够的标的重叠（与对冲腿同 underlying） | 配对存在的前提 | ✅ BTC/ETH/SOL/XRP/DOGE | ⏳ 待验证 |

**适配器需要封装的交易所差异**（对引擎完全透明）：

- 鉴权方案：Katana = HMAC + EIP-712 钱包签名；Backpack 预计为 API key + Ed25519
  （接入时验证）——全部隔离在 `init_signer`/请求签名内部
- 数值规整：pip 定宽字符串（Katana）vs tick/lot 取整规则（其它所）
- 订单/成交枚举值、WS 消息形状（长/短字段名）、撤单语义（按 id vs 批量）
- 限频形状（按请求计 vs 按权重计）与时钟要求

**接入流程（不变式：全程不改 engine.py）**：

```
实现标准 venue 接口 + §5.1 maker 契约（含 maker_capable 声明）
  → 注册 HEDGE_VENUES + .env 密钥 + console/secrets 校验
  → 通过 §10 的 Mock 合规测试套件（与 Katana 同一套，参数化）
  → 测试环境 E2E 验收（§10.3 同一标准）
  → 小额试点（§10.4 同一协议）
```

---

## 6. 引擎集成（选型论证）

**方案：扩展现有 Engine（Option B-lite），不新建 MakerEngine 进程。**

理由：安全机制几乎全部可复用且必须复用——

| 复用（零改动或调用点平移） | 说明 |
|---|---|
| venue locks | 对冲/撤单与 reconcile 互斥（`_vlock`） |
| 限频预算 + `_venue_limited` | maker 撤挂与 taker 共享 `orders_per_min` 滑窗 |
| `_venue_down` / 探活 | 触发 CANCEL_ALL 的信号源 |
| `_reconcile_positions` / grace | 挂单成交后与链上对账，兜住 WS 丢事件 |
| `_hedge`(reduce-only) / `_maybe_hedge` | EXPOSED 与净敞口的自愈路径 |
| cash/position/volume/last_traded_ts 记账点 | dashboard/web/MTM 语义不变 |
| recorder / status / balance / keepalive | 不感知模式差异 |

改动面：`_run_inner` 按模式装配任务；新增 `_maker_loop` + `_on_fill` 处理器 +
maker 专用 trades CSV；`_scan/_evaluate/_execute` 代码路径**不动**（taker 模式回归
零风险）。测试上两种模式各自覆盖，共享 venue mock。

---

## 7. 配置面（严格校验，进 `_SCHEMA`）— 已按此实现

```yaml
maker:
  enabled: false          # true 时 taker band 策略停用（§12 决策 2）
  edge_bps: 2.0           # 每笔成交的目标净锁利 E
  requote_bps: 1.0        # 锚移动重报价阈值
  requote_sec: 30         # 心跳重报价上限
  size_base: 0.005        # 每边挂单量（base 单位）
  sides: both             # both | bid | ask
  hedge_batch_ms: 250     # 成交→对冲的合并窗口（§12 决策 4）
  costs_bps: 5.5          # C：maker费+hedge吃单费+滑点估计（也可自动推导）
  max_hedge_failures: 3   # 对冲连续失败 → EXPOSED 停机
  hedge_retry_sec: 0.5    # 对冲重试退避
  interval_sec: 0.5       # 报价循环周期
  trades_csv: logs/maker-trades.csv
  selection_csv: logs/maker-selection.csv
```

复用现有：`inventory.*`（库存偏移）、`hedge.taker_fee_bps`、`hedge_slippage_bps`、
`max_position_usd`、`max_orders_per_min`、`staleness_sec`、`reconcile_sec`。

---

## 8. 风险与安全设计（分级）

| 级别 | 风险 | 缓解 |
|---|---|---|
| **P0** | 成交后对冲失败 → 薄所裸敞口 | 撤挂即熔断 + 重试窗口 + `_hedge` 兜底 + `cap_usd` 限制最坏情形 |
| **P0** | 对冲腿行情失明时被成交 | stale→毫秒级撤光所有单（§4.2 不变式） |
| P1 | 逆向选择（被成交=对方知情） | `edge_bps` 保底 + 库存偏移 + 成交后溢价偏移监控指标（§9） |
| P1 | WS 丢成交事件 | `z` 累计量幂等 + reconcile 周期对账采用链上 |
| P1 | 限频耗尽 → 撤单失败 | 预算核算（§3.4）+ 批量撤单 1 配额 + 预算不足降心跳 |
| P2 | Katana ADL 强平我们的对冲腿 | 薄所仓位小 + adlQuintile 可观测；接受残余风险 |
| P2 | 溢价中枢突变（>10bp 快速移动） | 锚定 executable 价天然跟随；`requote_bps` 收紧即自保护 |
| P2 | 时钟漂移 | 启动检查（文档要求 ±几秒） |

---

## 9. 观测与评估指标（判断策略是否成立的仪表）

`logs/maker-trades-<pair>.csv`，每个对冲批次一行：

| 字段 | 用途 |
|---|---|
| `exp_edge_bps` vs `realized_edge_bps` | 期望 vs 实锁（差=对冲滑点+延迟） |
| `fill_to_hedge_ms` | 成交→对冲发出的延迟分布 |
| `quote_age_sec`, `anchor_move_bp` | 重报价策略效率 |
| `prem_at_fill`, `prem_at_1s`, `prem_at_10s` | **逆向选择直接计量**（成交后溢价向不利方向漂移的幅度） |
| `fills_per_quote`, `cancel_reason` | 挂单质量拆解 |

**试点成败判据**（48h 小额试点后评估）：
- `realized_edge_bps` 均值 > 0.5bp（覆盖滑点后仍有边际）
- `prem_at_10s − prem_at_fill` 平均不利漂移 < edge_bps 的 50%
- 对冲成功率 > 99%，无 EXPOSED 持续 > 60s 的事件

---

## 10. 测试与验证计划

1. **单元**（离线）：报价公式（含偏移/取整到 tick）、重报价触发条件、fill-delta 幂等
   （重复/乱序 WS 消息）、GTX 被拒处理、预算耗尽时行为
2. **Mock 集成**：`MockMakerVenue`（脚本化：部分成交×2→全成；对冲失败路径；
   WS 断连路径；锚快速移动路径）跑完整 `_maker_loop` 状态机
3. **Maker 契约合规测试套件**：针对 §5.1 契约的**参数化**测试（挂单/撤单/成交
   回报/幂等/强撤/断连语义），用例与具体交易所无关——Katana 与未来的 Backpack
   等适配器必须通过**同一套**测试。这是 §5.3 扩展性承诺的强制执行机制，随 M1
   一并交付
4. **Sandbox E2E**（免费测试金）验收标准：
   - 挂单 → orders WS 推送与 REST 状态一致
   - 用 sandbox 网页端手动吃我们的单 → 引擎 ≤1s 发出对冲 IOC → 记账与 reconcile 一致
   - 批量撤单 RTT < 500ms；拔 WS 后 ≤5s 全部撤单
   - 连跑 30 分钟无 429
5. **实盘试点**：`size_base` 最小档（0.0005 BTC ≈ $40）、`cap_usd: 200`、
   `edge_bps: 4`（保守）→ 按 §9 判据评估 48h

---

## 11. 里程碑与工作量

| 阶段 | 内容 | 估时 |
|---|---|---|
| M1 ✅ **已完成 2026-09-20** | KatanaVenue maker 方法 + wsToken + KatanaOrdersFeed + **契约合规测试套件**（§10.3） | — |
| M2 ✅ **已完成 2026-09-20** | `_maker_loop`/`_maker_hedge_cycle` 状态机 + 记账/CSV + 17 项 Mock 集成测试 | — |
| M3 ✅ **已完成 2026-09-20** | config schema + 示例配置 + 文档（sandbox E2E 验收待密钥，见 §10.4） | — |
| M4 | 实盘试点 + 指标看板（日志级即可） | 0.5 天 + 48h 观察 |

**实现地图**（2026-09-20，99 项测试全绿）：

| 文件 | 内容 |
|---|---|
| `entropy_arb/maker.py` | 契约类型（FillEvent/MakerQuote/MakerParams）+ 报价数学（quote_prices/inventory_skew_bps/requote_reason，纯函数） |
| `entropy_arb/venue_katana.py` | place_maker(GTX)/cancel_orders(3 种签名结构)/on_fill + KatanaOrdersFeed（wsToken 鉴权、fill 幂等双层去重、open_orders 视图） |
| `entropy_arb/engine.py` | `_setup_maker_roles`、`_maker_loop`/`_maker_tick(_side)`、`_on_maker_fill`、`_maker_hedge_loop`/`_maker_hedge_cycle`、`_hedge_delta`、安全梯（blocked 状态 + 10s 重申清单）、EXPOSED 停机、trades/selection CSV、状态行扩展 |
| `tests/maker_contract.py` | venue-agnostic 合规套件（7 项检查，Backpack 复用同一套） |
| `tests/test_katana_maker.py` | Katana shim + GTX/撤单 EIP-712 签名验证 + wsToken HMAC 验证 |
| `tests/test_maker_engine.py` | 状态机集成测试（报价锚定、安全梯、批量对冲、失败停机、部分成交、灰尘聚合、cap 约束、关机清单） |

关键路径无外部依赖；M1 可立即开工（签名管线已就绪且经交叉验证）。

---

## 12. 决策记录（已确认，2026-09-20）

| # | 决策项 | 结论 | 说明 |
|---|---|---|---|
| 1 | v1 maker 交易所 | **固定 Katana** | 引擎侧按通用契约实现；后续 Backpack 等按 §5.3 准入清单接入，引擎零改动 |
| 2 | 与 taker 模式关系 | **互斥** | `maker.enabled: true` 时 `_strategy_loop`（band 扫描）不启动 |
| 3 | 库存了结方式 | **对称报价自然了结 + 手动 flatten** | 撤单 + reduce-only 平仓走控制台流程；自动 taker 强平留 v2 |
| 4 | `hedge_batch_ms` | **默认 250ms** | 微聚合成交、延迟风险可控 |
| 5 | 试点规模 | **$200 cap / $40 每边** | 配 `edge_bps: 4` 保守起步，按 §9 判据评估 48h |

---

## 附录：已验证 Katana API 速查（maker 相关）

| 用途 | 端点/机制 | 关键事实 |
|---|---|---|
| post-only 挂单 | `POST /v1/orders`, `timeInForce:"gtx"`（EIP-712 枚举 1） | 会交叉→整单拒，errorCode `TIME_IN_FORCE` |
| 批量撤单 | `DELETE /v1/orders` `{wallet(,+market)}` | 原子、**仅 1 次速率配额**；HMAC+钱包签名 |
| 单笔撤单 | 同上，带 `orderIds` | 受二级限频，尽量不用 |
| 成交回报 | WS `orders` 订阅（鉴权） | `z` 累计量幂等；`F[]` 逐笔明细；`ec/em` 强撤原因 |
| WS token | `GET /v1/wsToken` | 单次有效，断连重取 |
| 数值格式 | 8 位定宽字符串 | qty 按 `stepSize`(1e-4)、px 按 `tickSize`($1) 取整 |
| 挂单上限 | 100 单/市场/边，1000 单/钱包 | 我们最多 2 单/边，无虞 |
| 费率 | maker 0.475bp / taker 1.9bp（市场级） | 三层取最低，promo 可降 |

# Backpack Exchange 接入规划

> 写作时间：2026-09-28 · 状态：**P0 已实现**（见文末"实现状态"）
> 相关文档：[MAKER-DESIGN.md](MAKER-DESIGN.md)（maker 契约与安全设计）、
> [HANDOVER-LIGHTER-KATANA.md](HANDOVER-LIGHTER-KATANA.md)（Katana 线的现状与教训）、
> [BASIS-EXPLORE.md](BASIS-EXPLORE.md)（基差方法论）

---

## 0. 目标：角色 × 模式 2×2 全覆盖

引擎的角色模型（`--base` 基准腿 / `--hedge` 对冲腿；maker 模式下挂单角色固定落在
**对冲腿**、吃单对冲落在**基准腿**，见 `engine._setup_maker_roles`）。Backpack 要
四个格子全部能站：

| 启动方式 | Backpack 的角色 | 另一腿 | 说明 |
|---|---|---|---|
| `--base hl --hedge backpack`（taker band） | 对冲腿（taker 吃单） | Entropy/HL | 经典双吃单套利 |
| `--base backpack --hedge lighter-rh`（taker band） | 基准腿（taker 吃单） | Lighter rh | premium = backpack/rh − 1 |
| `--base lighter-rh --hedge backpack` + `maker.enabled` | **下单腿**（post-only 挂单） | Lighter rh 吃单对冲 | 对标现在的 rh↔katana 线 |
| `--base backpack --hedge katana` + `maker.enabled` | **对冲腿**（吃单对冲） | Katana 挂单 | Backpack 深度做对冲容量 |

架构上这几乎是"免费"的：`--base/--hedge` 已解耦（`af9a26c`），maker 契约
（`maker.py` + `tests/maker_contract.py`）本来就是按"任意满足 §5.3 清单的交易所
可换入"设计的——契约测试的 docstring 里甚至已经写着 `BackpackMakerCase` 的示例。
**引擎与安全梯零改动**，工作量集中在 venue 适配层。

---

## 1. 已核实的 API 事实

来源：官方文档站 <https://docs.backpack.exchange>（TOC）、官方 asyncapi 规范镜像
<https://raw.githubusercontent.com/api-evangelist/backpack/refs/heads/main/asyncapi/backpack-asyncapi.yml>、
第三方 SDK 源码 <https://github.com/solomeowl/backpack_exchange_sdk>（含一份完整
openapi.json，实现时可当字段参考）。

### 1.1 端点与认证

| 项 | 值 |
|---|---|
| REST | `https://api.backpack.exchange`（路径形如 `api/v1/...`，**无前导斜杠**拼在 base 后） |
| WS | `wss://ws.backpack.exchange`（**与 REST 不同域名**） |
| 认证 | Ed25519；API key = base64 公钥，secret = base64 私钥种子（32 字节） |
| 请求头 | `X-API-Key` / `X-Signature`（base64 签名）/ `X-Timestamp`（ms）/ `X-Window`（默认 5000ms） |
| 签名串 | `instruction=<action>` + 按 key 字典序的 `&k=v`（bool 小写）+ `&timestamp=..&window=..`；GET 签 query 参数，POST/DELETE 签 body 参数 |
| 时钟 | X-Timestamp 与服务器偏差须在 window 内 → 启动时用 `GET /api/v1/system/time` 校准并记偏移 |

### 1.2 订单（instruction 名用于签名）

| 操作 | 端点 | instruction | 关键参数 |
|---|---|---|---|
| 下单 | POST `api/v1/order` | `orderExecute` | orderType Limit/Market、side **Bid/Ask**、price/quantity（字符串）、timeInForce GTC/IOC/FOK、**postOnly**（bool，与 IOC 互斥）、clientId（**u64 整数**）、selfTradePrevention、reduceOnly（futures） |
| 撤单 | DELETE `api/v1/order` | `orderCancel` | symbol + (orderId 或 clientId) |
| 全撤 | DELETE `api/v1/orders` | `orderCancelAll` | symbol（可按 orderType 过滤） |
| 批量下单 | POST `api/v1/orders` | 每单 `instruction=orderExecute&...` 拼接签名 | v2 可选，暂不做 |
| 挂单查询 | GET `api/v1/orders` | `orderQueryAll` | symbol / marketType 过滤 |

映射到统一契约：

- **taker 腿** `send_taker()`：`Limit + IOC` + 滑点保护价 → 同步响应结算，
  对齐 Katana 的 IOC 模型（avg px / filled / unresolved escalate）。
- **maker 腿** `place_maker()`：`Limit + GTC + postOnly=true` → GTX 等价语义。
- **安全撤单** `cancel_orders(None)`：`orderCancelAll` 一个请求（正好是 maker
  安全路径要求的"market-wide atomic cancel"）；按 id 撤 `cancel_orders(ids)`：
  无批量端点，**循环单撤**（带 in-flight 上限，避免烧限频）。
- `reduce_only` 直接支持 → 对冲腿 `_hedge()`/`_hedge_delta()` 的 reduce-only
  语义无需特殊处理（**组合行为需实测**，见 §7）。

### 1.3 行情与私有流（wss://ws.backpack.exchange）

- 订阅帧：`{"method":"SUBSCRIBE","params":["depth.SOL_USDC_PERP",...]}`；
  私有流前缀 `account.`，需带 `signature:[verifyingKey, sig, ts, window]`，
  签名串 `instruction=subscribe&timestamp=<ts>&window=<window>`。
- 信封：`{"stream":"<name>","data":{...}}`。
- **`depth.<SYMBOL>`**：增量、档位**绝对量**、qty=0 删档；`U`/`u` 序号，
  连续性要求 `U == prev.u + 1`；需 REST `GET /api/v1/depth` 快照播种。
  —— 与 `KatanaBookFeed` 的 snapshot+seq 模型**一比一对应**，算法可直接复用
  （订阅先于快照、diff 缓冲重放、gap 重快照、单飞+限频保护）。
  另有聚合档 `depth.200ms/600ms/1000ms.<SYM>`（省限频用，暂不需要）。
- **`account.orderUpdate[.SYM]`**：事件 `orderAccepted/orderFill/orderCancelled/
  orderExpired/orderModified/...`；fill 事件带 `i`(orderId)、`c`(clientId)、
  `t`(trade id)、`l`(本次成交量)、`z`(累计成交量)、`L`(成交价)、`n/N`(费率)、
  `m`(我方是否 maker)、`X`(订单状态)。—— 去重方案照抄 `KatanaOrdersFeed`：
  fill id（`t`）为主、累计量 `z` 差分为兜底，重连不重放已对冲数量。
- **`account.positionUpdate[.SYM]`**：订阅即推当前仓快照（`e` 缺省），
  之后 `positionOpened/Adjusted/Closed`；`q` 为**带符号净量**（正=多）。
  —— 可作为 `fetch_position` 的加速器（v1 先用 REST，v2 再说）。
- 心跳：服务器每 60s 发 ws ping，120s 内必须 pong（`websockets` 库自动应答，
  维持现有 ping_interval/ping_timeout 参数即可）；服务器停机发 Close 1001 +
  30s 宽限 → 重连退避里特殊处理。
- 时间戳一律 **微秒**（kline 的 t/T、RFQ 除外）。

### 1.4 市场/符号/账户

- 永续符号：`SOL_USDC_PERP`（现货为 `SOL_USDC`）。`--symbol SOL` → 自动补
  `_USDC_PERP`（`load_market` 里做候选匹配，`hedge.symbol`/`entropy.symbol`
  可覆盖，同 Katana 的 `-USD` 候选逻辑）。
- `GET /api/v1/markets`：市场元数据（tick/step/最小单等，字段名以 openapi.json
  为准，实测确认）；顺带把返回的费率字段打进日志（学 Katana 打 takerFeeRate）。
- 持仓：GET `api/v1/position`（净量带符号）→ `fetch_position`。
- 权益：GET `api/v1/capital/collateral` / `balances` → `fetch_equity`。

### 1.5 费率（影响策略可行性，必须实测）

- EU 实体 perp tier1：maker 0.020% / taker 0.050%（tier/VIP 随 30 天量+BP
  stake+MadLad 下调至 maker 0）；主站费率表是图片，文本里没有数字。
- **结论先按"主站 taker ≈ 2.5–5bp、maker ≈ 0–2bp"两种情形各自预演**（§6），
  实盘前用 markets/账户接口核实实际档位并写进配置。
- 配置纪律不变：`taker_fee_bps` 显式写在 profile（引擎的门槛是显式数字），
  接口值只做日志对照。

---

## 2. 改动清单（按文件）

### 新增

| 文件 | 内容 | 预估 |
|---|---|---|
| `entropy_arb/venue_backpack.py` | `BackpackSigner`（Ed25519 签名、时钟偏移）、`BackpackVenue`（kind=`"backpack"`，`maker_capable=True`）、`BackpackOrdersFeed`（私有订单流，fill 去重） | ~700 行，结构照 `venue_katana.py` |
| `entropy_arb/feeds.py` 追加 `BackpackBookFeed` | snapshot+U/u diff、缓冲重放、gap 重快照 | ~150 行，算法照 `KatanaBookFeed` |
| `tests/test_backpack.py` | 签名向量（固定 key 的离线 golden test）、下单参数形状（IOC/postOnly）、响应解析（filled/partial/canceled/would_cross/unresolved/429） | ~250 行 |
| `tests/test_backpack_maker.py` | `BackpackMakerCase` → `run_contract()`：契约套件已预留此用法 | ~120 行 |
| `profiles/` 示例 | `lighter-rh-hype-backpack.yaml`（Backpack 挂单 + rh 对冲）、`backpack-hype-katana.yaml`（katana 挂单 + Backpack 对冲） | 小 |

### 修改（每处都是小改）

| 文件 | 改动 |
|---|---|
| `config.py` | `HEDGE_VENUES`/`BASE_VENUES` 加 `"backpack"`；`MAKER_VENUES` 加 `"backpack"`；`BackpackCreds(api_key, api_secret)` + `VenueConf.backpack_creds` + `creds_complete` 分支；base/hedge 两个构造分支（默认 fee_bps 待核实值、orders_per_min=120）；`.env` 读 `BACKPACK_API_KEY/BACKPACK_API_SECRET` |
| `engine.py` | `_make_venue` 加一个 `if vc.kind == "backpack"` 分支（3 行） |
| `main.py` | 无需改（choices 来自 config 常量，help 文案顺手更新） |
| `requirements-live.txt` | 加 `cryptography`（Ed25519 签名；aiohttp/websockets 已在基础依赖） |
| `.env.example` / `config.example.yaml` | Backpack 凭证段（注明 key=base64 公钥、secret=base64 种子、只开 Trade 权限）与 venue 注释行 |
| `console/secrets.py` | `BACKPACK_API_KEY/SECRET` 的格式校验（base64）+ venue→所需键映射（学 Katana 的教训：不做字母表校验，只查空白/长度） |
| `console/server.py` / `console/venues.py` / `profiles.py` | venue 下拉、`exchange_of("BACKPACK")→"Backpack"`、maker-capable 提示文案 |
| `tools/basis_probe.py` | venue 表加 backpack（采集线，照 katana 分支写） |
| `README.md` / `README.zh-CN.md` | venue 表、凭证段、maker 段（`--hedge backpack`） |

引擎的 `premium_bps`、inventory、reconcile、dashboard、web 全部不动——它们只见
`VenueConf` 统一接口。

---

## 3. 适配器关键设计决策

1. **数值格式化**：Backpack 用十进制 tick/step（非 Katana 的 8 位 pip 字符串），
   保留 `_pips`-式的 floor/ceil 取整到 tick 网格，输出纯十进制字符串
   （`f"{v:.10f}".rstrip("0")` 风格，避免科学计数法）。
2. **clientId 是 u64**：不能用 uuid hex。用进程内单调计数器
   （`time_ms << 20 | seq` 之类可读性更好的组合），撤单可按 clientId 兜底。
3. **post-only 拒绝的形状**：postOnly 交叉时整个订单被拒（HTTP 4xx + error code）。
   错误码/响应体形状**实测确认**后映射为 `status="canceled", reason="would_cross",
   err=None`——契约明确要求这不算错误（快市场里正常事件，重试即可，绝不能烧
   consec_errors）。这是 Katana 上线首日烧预算的同一个坑（§5.2）。
4. **未决结果**：超时/5xx/非 JSON → `unresolved=True`，交给引擎 reconcile
   （orderQuery/orderQueryAll 做 escalate 的查询端点）。
5. **限频**：429 → `RATE_LIMITED:` 前缀（引擎现成的 reactive pause）。
   具体配额（下单/撤单/订阅分别多少）实测后写进 venue 文档字符串。
6. **私有流是报价前提**：`maker_mode=True` 时 `ready_to_trade()` 依赖
   `BackpackOrdersFeed.ready` —— 与 Katana 相同的安全门（盲腿不报价）。
7. **`warm_http`**：`GET /api/v1/system/ping`（保持订单路径 TLS 热）。
8. **demo/沙盒**：未见文档化的 demo API（主站有 Demo Trading 功能，API key 是否
   分离待确认）。若有 → 学 `KATANA_SANDBOX` 加 `BACKPACK_DEMO=1` 路由，maker
   链路先在 demo 全链路演练（MAKER-DESIGN §10.4 的方法论）。

---

## 4. 测试与验证阶梯

0. **离线测试**：§2 的新增测试 + 全量回归（当前 137 passed 必须保持绿）。
   签名 golden test 用固定 key/参数断言签名串与签名输出，防回归。
1. **只读体检**（不花钱）：`/tmp/backpack_check.py` —— 时间偏移、balances/
   position 可读、签名链路 OK（对应 Katana 的 `katana_check.py`）。
2. **订单链路体检**（远端盘口 post-only 挂单+撤单，最小名义）：
   `would_cross` 映射、orderCancelAll、clientId 撤单、私有流 fill 事件形状
   （对应 `katana_order_path.py`）。
3. **采集**：`--record-only` 跑 `backpack↔hl`、`backpack↔lighter-rh`（probe
   systemd 单元照抄 `entropy-probe` 模式），≥1–2 天分钟数据。
4. **分析**：`tools/analyze.py --fees-bps <实测>` + `basis_matrix.py` → 决定
   哪条线、哪个方向、taker 还是 maker（§6 的两套预演谁成立）。
5. **最小实盘**：maker 试点纪律照抄 rh↔katana（cap $100–200、size 最小档、
   `max_signal_edge_bps` 保留、观察 `maker-selection-*.csv` 逆向选择指标）。

---

## 5. Katana 的教训逐条对照（HANDOVER §9 → Backpack 方案）

| 教训 | Backpack 侧 |
|---|---|
| 报价锚定对冲腿、基差大时 GTX 连续被拒烧预算 | 引擎已有 `clamp_to_maker_book`（ venue 无关，自动生效）；post-only 拒绝仍按 would_cross 静默映射 |
| 重启留孤儿单 | 待办同一条：maker 启动先 cancel-all（这是引擎层待办，Backpack 的 orderCancelAll 已具备原子性） |
| 私有流是报价前提 | 同设计，`ready_to_trade` 门 |
| session key 30 天过期 | Backpack 的 API key 长期有效、可开"仅交易"scope、可绑定 IP —— 更省心；文档写明权限最小化 |
| `position` 无符号 | Backpack `positionUpdate.q` / REST 均为**带符号净量** —— 坑不存在，但 reconcile 首次运行时仍打印方向核对（教训 §5 的操作纪律保留） |
| 薄盘假行 | Backpack 深度远好于 Katana（这是本次换所的主要动机），`max_signal_edge_bps` 仍保留 |
| nonce 冲突 | 无 nonce 概念（Ed25519 每请求独立签名）—— Lighter 的 P0 问题在 Backpack 不存在 |
| 保证金率按标的而异 | 首仓前用 positionUpdate 快照/`position` 接口读 `initial margin fraction`，cap 按实际档位折算 |

---

## 6. 经济性预演（在费率核实前只做框架，不结论）

两条候选结构（对照 rh↔katana 线的 maker 成本 ≈0.95bp + rh 点差）：

- **A：Backpack 挂单 + rh 吃单对冲**（`--base lighter-rh --hedge backpack`）
  成本 = bp maker + 0(rh taker) + rh 点差。若主站 maker=0 → 成本≈rh 点差，
  优于 katana 线；若 maker=2bp → 与 katana 相当，但**容量/深度远大**
  （katana 的 OI 天花板是这条线的根本限制：ETH $473k OI）。
- **B：Katana 挂单 + Backpack 吃单对冲**（`--base backpack --hedge katana`）
  成本 = 0.475 + bp taker(2.5–5) + bp 点差(小)。若 bp taker 5bp → 总 ~8bp，
  只有 sd 极大的标的（ZEC 类）才养得活；若 2.5bp → ~5.5bp，与 HL 对冲腿相当
  但深度更好。
- taker-taker（`--base hl --hedge backpack`）：HL 4.5 + bp 2.5–5 ≈ 7–9.5bp
  成本 → 与 katana 线同样"必须走 maker"的结论概率大；等采集数据说话。

**先采集、先核实费率，再选线**——顺序与 HANDOVER §8 的恢复纪律一致。

---

## 7. 待确认清单（P0 实测，编码前/中完成）

- [ ] 主站实际 maker/taker 费率（markets/账户接口 + 自己账户档位）
- [ ] `GET /api/v1/depth` 快照响应是否带 update id（决定快照/diff 对齐细节；
      asyncapi 只说"需快照播种+U 连续"）
- [ ] markets 元数据的字段名（priceTick/quantityTick/minOrderQuantity?）
- [ ] post-only 拒绝的 HTTP 状态与 error code（would_cross 映射）
- [ ] IOC + reduceOnly 组合的实际行为（对冲腿场景）
- [ ] 下单/撤单/订阅限频配额；`max_orders_per_min` 保守起步（120）
- [ ] Demo trading 是否有独立 API（`BACKPACK_DEMO` 开关可行性）
- [ ] 服务器时钟与本地偏差（X-Window 5000ms 是否够，不够则启动校准）

## 8. 排期建议

| 阶段 | 内容 | 规模 |
|---|---|---|
| P0 | §7 实测确认 + venue_backpack/feeds/config/tests + 只读体检 | 1–1.5 天编码 + 半天实测 |
| P0.5 | 订单链路体检（挂撤单）+ 回归全绿 | 半天 |
| P1 | probe 采集两条线 + analyze → 定线定参数 | 跑 1–2 天数据 |
| P1.5 | 最小 cap maker 试点（单标的） + 逆向选择观察 | 半天上线 + 观察 |
| P2 | `account.positionUpdate` 做持仓加速、批量下单端点、demo 演练（若存在） | 按需 |

---

## 9. 实现状态（2026-09-28 P0 完成）

| 项 | 状态 |
|---|---|
| `entropy_arb/venue_backpack.py`（Signer/Venue/OrdersFeed） | ✅ |
| `feeds.py` BackpackBookFeed（快照+U/u 重放） | ✅ 实测：SOL 同步 760 bids/882 asks |
| config/engine/main/console 接线（base/hedge/maker 三组枚举） | ✅ |
| 契约测试 `BackpackMakerCase`（maker_contract 套件） | ✅ |
| 单测 test_backpack.py（签名 golden 向量/订单形状/解析/两条 ws 流） | ✅ |
| `tools/basis_probe.py` backpack 分支 | ✅ 实测解析 SOL/HYPE |
| `tools/backpack_check.py`（只读体检 + `--order-path`） | ✅ 待真实密钥跑一次 |
| profiles：`lighter-rh-hype-backpack`（BP 挂单）、`backpack-hype-katana`（BP 对冲） | ✅ 过严格校验 |
| README/README.zh-CN/.env.example/config.example.yaml | ✅ |

**实现期已当场核实**（公开端点实测，原 §7 的部分待确认项已消）：

- `GET /api/v1/markets`：`filters.price.tickSize` / `quantity.stepSize` /
  `minQuantity` / `orderBookState=="Open"` / `imfFunction.base`（保证金）✅
- `GET /api/v1/depth` 快照**带 `lastUpdateId`** ✅（快照/diff 对齐成立）
- `GET /api/v1/time` 返回 epoch 毫秒 ✅（本机偏差 ~10ms ≪ window 5000ms）
- `GET /api/v1/ping` → `pong` ✅（warm_http）
- instruction 名：`positionQuery` / `collateralQuery`（openapi 描述原文）✅
- `clientId` 是 **uint32**（openapi schema）✅ → 进程内计数器
- 下单响应：`status`(New/Filled/PartiallyFilled/Cancelled/Expired) +
  `executedQuantity` + `executedQuoteQuantity`（均价=quote/base）✅；
  post-only 交叉 = `Expired + expiryReason=PostOnlyTaker`（另防御 4xx 文本）
- 权益：`capital/collateral → netEquity / netEquityAvailable` ✅

**仍未核实（需要 API 密钥/实盘，上线前必做）**：

- [ ] `tools/backpack_check.py --order-path`：签名链路、would-cross 的实际
      响应形状（200+Expired 还是 400）、orderCancelAll 返回结构
- [ ] 账户实际费率档位（两个 profile 的 `costs_bps`/`taker_fee_bps` 随之修正）
- [ ] 下单/撤单限频实测
- [ ] IOC+reduceOnly 组合行为
- [ ] demo 环境是否存在（`BACKPACK_API_URL` 覆盖开关已预留）

**附带修复**：`tests/test_maker_engine.py` 的 make_engine 现在把
`recorder.csv` 指到临时目录——此前 `_seed_prem_history` 会读到工作区里真实
的 `logs/minutes.csv`，vol_widen 被意外打开导致 4 个测试在部署机上必红
（HEAD 纯净检出 + 拷入真实 CSV 已复现）。

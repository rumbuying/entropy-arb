# 新接入一家交易所（ADD-A-VENUE）

> 目标：接入一家新交易所 = **写 1 个 `venue_<x>.py` + 在 `venue_registry.py` 加 1 条声明 + 测试**。
> engine、console、前端、CLI、secrets 校验、discovery、探测工具全部自动获得新所，不需要人肉对齐。
> 如果哪一步发现还要去别的文件"补一条 if"，那是回归——请回来修派生，而不是补分支。

---

## 0. 前置调研（写代码前必须回答的三问）

这三条来自 Ondo Perps / Arcus 调研和 bulk 私有流在途的教训，**每一家都要先查清**：

1. **断线撤单语义**：交易所断连后挂单是否存活？
   - 有 cancel-on-disconnect → 常规接入；
   - 没有（如 Arcus/dYdX 系）→ 必须实现"死手开关"常驻续期任务（如 `scheduleCancel`，
     注意每日触发/布防配额），因为引擎的 `cancel_orders(None)` 市场级全撤兜底在断线时**不可达**。
2. **私有流认证形态**：REST 认证 ≠ WS 认证。
   - Ondo：REST 是 HMAC API key（可完全无头），私有 WS 却要 SIWE 会话 JWT（需 EOA 私钥
     本地签 ERC-4361 换会话；API key 能否直接换 WS 会话要实测）；
   - Backpack/Arcus：订阅帧带签名，注意签名是否随时间老化（Backpack 8h 强制重连刷新）。
3. **私有 WS fill 判别**：文档通常不写全事件形态（bulk 至今挂着"实盘核对"）。上线前用真实
   账户小额成交核对 `on_fill` 的去重键（trade id + 累计量的 fallback），**引擎按你告诉它的
   fill 精确对冲——重放的 fill 就是重复对冲**。

另查：手续费梯队（默认费率写进 spec，注释标明来源与档位）、数量/价格精度格式、429 语义、
测试网有无。若是永续以外的产品线（现货股票代币等），先停：引擎假设多空对称永续，需要另立议题。

### 第四问（Perpl 教训）：订单生命周期与订单模型

4. **订单有没有交易所侧的 TTL / 特殊订单模型**——文档要读到 WS 帧级：
   - Perpl：**所有订单（含 GTC）最长活 `order_ttl_blocks`（20 块 ≈ 6 秒）**，挂单自动
     过期——maker 模式=持续重挂（秒级 requote 节奏）；下单走认证 WS 帧（mt:22）而非
     REST，`rq` 严格递增做幂等键（从钱包快照的 `lfr` 播种）；订单按**仓位方向**建模
     （OpenLong/OpenShort/CloseLong/CloseShort），对侧加仓必须先平，不存在净化；
   - 私有对象的 wire 字段文档没写全时（Perpl 的 Order/Position/Wallet）：宽容解析
     （多字段候选 + 未知打日志），代码里打 VERIFY 标记，实盘前用真实账户核对；
   - fills 可能没有唯一 id（Perpl/bulk 都是）：用稳定元组去重，重连后用 REST 回填缺口。

## 1. 写 `entropy_arb/venue_<name>.py`

实现一个 `<Name>Venue` 类（鸭子类型，合同见 `maker.py` 头注释与 `tests/maker_contract.py`）：

- 属性：`book, position, equity, free, name, key, kind, conf, fee_bps, cap_usd,
  size_decimals, min_base, min_quote, tick_size, step_size, margin_used,
  margin_collateral, max_leverage`
- 生命周期：`load_market()` / `init_signer()` / `start_tasks(stop, notify, live)` /
  `ready_to_trade()` / `warm_http()` / `close()`
- 交易：`send_taker(is_buy, qty, limit_px, reduce_only) -> {status, filled_base, avg_px,
  err, unresolved}`、`px_round(px, round_up)`
- maker 合同（若支持）：`maker_capable = True` + `place_maker / cancel_orders /
  on_fill / open_orders`
- 查询：`fetch_position() / fetch_equity() -> (eq, free)`、可选 `fetch_funding(market)`
  （配合 spec 里 `funding_supported=True`）

**尽量用 `venues_common`**，不要复制旧 adapter 的样板：

- `fnum`（解析）、`step_decimals / round_grid / grid_str / px_round_grid`（精度与取整）、
  `classify_http`（429→RATE_LIMITED、4xx 拒绝、5xx 未决——下单通道是 REST 还是 WS post
  帧都映射到同一三元组合同）
- `OrdersFeedBase`：私有流骨架（重连退避、ready 信号、成交去重、open_orders）——你只需实现
  `_subscribe_frame()`（签名放这里，每连新建）和 `_handle_envelope()`（wire → fill/order）；
  签名会老化的所覆写 `_should_reconnect()`
- `SeqBookFeedBase`：快照+序列号的 L2 纪律（先订阅后快照、竞速事件缓冲、缺口即重同步）——
  实现 `_fetch_snapshot()` / `_snapshot_seq()` / `_on_connected()` / `_on_message()`，
  每个解析出的 book 事件走 `offer(seq_lo, seq_hi, bids, asks)`

模块级三个钩子（registry 用 importlib 惰性解析，**不要在模块顶层 import 签名 SDK**）：

```python
def make_venue(vc, session, settle_timeout): ...          # VenueConf -> 客户端
def make_public_feed(listing, book, notify, session=None): ...   # discovery 用
async def list_markets_catalog(session, venue, dex=""): ...      # 公共 REST 市场目录
```

## 2. 在 `entropy_arb/venue_registry.py` 注册

按注释顺序在 `VENUES` 里追加一条 `VenueSpec`（**加在 `_ORDER` 末尾**，顺序即所有派生列表
的展示顺序）：

- `key`（CLI 名）/ `kind`（adapter 类，可复用现有所——`lighter-rh` 就是零 adapter 接入）
- `label` / `label_hedge`（对冲腿显示名不同才填）/ `display`
- `base` / `hedge` / `maker_capable` / `funding_supported` / `in_discovery`
- `leg_fee_bps`（yaml 可覆盖的默认费率，注释写清档位依据）、`opm_base` / `opm_hedge`
- `discovery_fee_bps`（打分兜底费率）、`fee_variants`（仅 HL 类多 dex）
- `creds_dataclass`（在 `config.py` 定义新 Creds dataclass，带 `.complete` 属性）+
  `creds_group`（secrets 页组名）+ `creds`（`CredField` 列表：字段、env 回退链、格式校验
  id、是否必需）+ `requirements`（最小可交易 env 键）
- `module="entropy_arb.venue_<name>"`

若凭证展示/诊断需要特例，再补 `UI_CARDS[key]`（凭证卡片）与 `NEED_GROUP[key]`
（runs 页按腿查完整性；只有 lighter 式 per-leg 覆盖才需要）。

## 3. 测试

- `tests/test_<name>.py`：签名/错误分类/fill 去重/mock WS + REST（参照 test_bulk.py）；
- maker 所：`tests/test_<name>_maker.py` 写一个 MakerCase shim（合同套件是
  `tests/maker_contract.py`，不用重写）；
- `tests/test_venue_registry.py` 会自动核对你的钩子/secrets/UI 声明——跑它；
- `tests/test_golden_legconf.py` 是存量行为快照，**新增 venue 不应改变它**（新 key 不在
  旧矩阵里）；若它红了，说明你改了老所的行为——停下检查。

## 4. 样板文件（仍需手动的部分）

| 文件 | 内容 |
|---|---|
| `.env.example` | 凭证段（键名与 spec 的 env 链一致——test_venue_registry 会查） |
| `requirements-live.txt` | 签名 SDK 块（标注仅 live 需要） |
| `config.example.yaml` / `README.md` / `README.zh-CN.md` | venue 表加一行（费率/角色/备注） |
| `discovery-watchlist.yaml` | 若要参与扫描，`venues:` 加名 |
| 试点 profile ×2 | 观察线跑起来 |

## 5. 自动获得（零接线，勿再手改）

config 三元组与腿构造、`--hedge/--base` CLI 校验、engine 工厂、console 工厂与 🩺 诊断、
secrets 键格式校验与完整性要求、live 启动预检、资金费支持矩阵、discovery 目录/feed 工厂、
star_probe/basis_probe、v2 前端卡片与下拉框。

## 6. 上线（见 DEVOPS §4）

1. `git pull` 服务器 + `systemctl restart entropy-console`（worker 不受影响，KillMode=process）；
2. 🔑 先过 🩺 诊断（auth/market/下单路径），再上观察线；
3. 私有 WS fill 判别**实盘核对**后才开 maker；
4. 引擎代码进 worker 要在 Runs 页逐个 restart，持仓线选安静窗口。

---

### 检查清单（速查）

- [ ] 四问有答案：断线撤单 / 私有流认证 / fill 判别方案 / 订单 TTL 与订单模型
- [ ] `venue_<name>.py`：Venue 合同 + 三钩子 + venues_common 基类（死手开关任务如有）
- [ ] `venue_registry.py`：VenueSpec + UI_CARDS（+ NEED_GROUP 如需）
- [ ] Creds dataclass（config.py）`.complete`
- [ ] 单测 + maker shim + `test_venue_registry.py` 绿 + golden 未漂移
- [ ] `.env.example` / requirements-live / README×2 / config.example / watchlist
- [ ] 🩺 诊断通过 → 观察线 → fill 实盘核对 → 开 maker

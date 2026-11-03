# 通用合约网格：技术设计与开发规格

日期：2026-10-11。产品基线：[GRID-TRADING-PLAN.zh-CN.md](GRID-TRADING-PLAN.zh-CN.md)。本文件用于指导开发与验收；接口、表和模块中标为新增的内容尚未实现。没有读取真实密钥、连接交易账户或发送订单。

执行交接新增 [GRID-TRADING-AI-HANDOFF.zh-CN.md](GRID-TRADING-AI-HANDOFF.zh-CN.md)，细化精确模型、`grid-calc-v1` 取整/布局、固定验收向量和 M0 子任务边界；用于驱动开发的提示词见 [GRID-TRADING-AI-PROMPT.zh-CN.md](GRID-TRADING-AI-PROMPT.zh-CN.md)。M0 基础实现不等于 G01–G03 所有真实合同已认证。

用户最新执行要求为一次连续尽可能多开发：按依赖推进 G01–G18，M0 通过后继续 M1/M2/M3，不逐阶段等待新指令。代码/模拟验收和真实交易认证分开记录；缺实盘证据只阻断对应真实资格，继续其它可独立开发工作。技术放行门槛不因此降低。

## 1. 已确定的技术决策

| 决策 | 采用方案 | 原因与边界 |
|---|---|---|
| 运行拓扑 | 一个独立 GridRuntime 服务，内部一个网格一个 actor | 网站重启不影响执行；多个网格共享账户连接、签名调度和预算 |
| 持久化 | 新增 `data/grid-runtime.sqlite`；只有 GridRuntime 写入 | 不让新运行服务和旧控制台并发写 `console.sqlite`，不依赖控制台在线才能补单 |
| 技术栈 | Python asyncio + aiohttp + SQLite WAL，沿用现有 SDK 与前端模块 | 首个单机版本无需 Redis、消息代理、微服务或前端框架迁移 |
| 交易语义 | 初版为稳定币保证金线性永续、净持仓模式 | 反向合约与双向持仓不能仅靠配置打开，分别扩展算法与适配合同 |
| 账户分离 | 网格与套利使用各自资金账户与凭证，由使用者保证不同账户 | 不实现跨策略硬互斥或旧 worker 接管；网格内部保留账户/市场占用与签名调度 |
| 执行一致性 | 持久事件 + 订单意图 + 稳定成交键 + 真实对账 | 不承诺交易所端端到端 exactly-once；网络未决要停止相关新增风险 |
| 配置与预览 | 严格 schema、不可变版本、短期预览、执行前再次校验 | 浏览器提交参数不是订单执行依据 |
| 前端 | V2 内网格与套利提供独立模块入口 | 复用网站框架、登录和语言；账户接入/配置、策略与运行记录分别管理 |

这里把产品规划中的“独立 grid worker”具体化为独立运行服务，**不为每个网格再启一个进程**。每个 actor 是有顺序邮箱的状态机，不是线程或独立数据库写入者。单机服务暂不做多副本自动接管；以后扩展部署模型时再设计跨机器领导权与数据库。

2026-10-11 用户调整：网格与套利由使用者配置不同资金账户，移除跨策略互斥及统一旧 worker 治理要求。最新边界见 [模块分离决策](planning/grid/MODULE-SEPARATION.zh-CN.md)。网格内部仍统一管理账户预算、订单和签名。

## 2. 现有代码的接入点和差距

| 已核对文件 | 现有事实 | 开发动作 |
|---|---|---|
| `console/storage.py` | 明确仅控制台写；已有 runs、operations、策略账本；历史事件表带导入来源字段 | 新运行库独立迁移；旧库只存带来源的显示投影，不充当交易权威 |
| `eventlog.py` | 队列满会丢记录；写日志失败只标 degraded | 仅用于诊断；新增的交易事件提交失败必须阻止新增订单 |
| `maker.py:FillEvent` | 数量、价格、费用为 float；缺独立 trade id、费用币种等字段 | 新增精确 ExecutionEvent，不从 float 反推真实交易数值 |
| `venues_common.py:OrdersFeedBase` | 去重状态在内存；字典可清理 | 可复用重连框架；执行去重必须在数据库，历史与实时用相同稳定键 |
| `venue_backpack.py:place_maker` | clientId 由 adapter 内部生成；仅实现旧 maker 合同 | 增加外部传入稳定 client id 的新交易合同，不让桥接层偷偷重新生成 |
| `venue_hl.py` | nonce 共享仅限两个 adapter 实例；现有 `start_tasks` 没有网格私有成交流 | 新 AccountExecutor 统一分配 nonce，增加成交/订单/资金费服务 |
| `venue_lighter.py` | 当前通过刷新 nonce 应对共享 key 竞争；不是持久的共享调度 | 网格通过账户/签名执行队列消除本系统内竞争；保留旧逻辑兼容 |
| `console/operations.py` | 旧套利有自己的操作预览与冲突处理 | 保留旧响应与行为，不接入网格账户占用；网格使用独立持久操作模型 |
| `console/supervisor.py` | 识别 `main.py` 双腿 worker 并分配端口；Linux 用 `/proc` 接管 | 不给网格塞假 hedge；新增 RuntimeClient/RuntimeStatus，旧 supervisor 继续管理套利 |
| `console/server.py` | 账户展示部分按交易所分组；认证允许 token 在 query | 网格按真实账户聚合；新增写接口只接受安全认证路径，不把 token 放网格推送 URL |
| `webui/v2/app.js`、`webui/api.js` | 模块化 hash 路由、统一请求与语言处理 | 新页面遵守 mount/destroy；新增可取消请求、带操作幂等键的请求辅助方法 |
| `venue_registry.py` | 统一目录，maker 标记不能表达完整网格资格；本地已有 Perpl 接入变化 | 新增独立网格能力解析；Perpl 和其他新增所按同一合同认证，已有接入不等于网格合格 |

技术规格以本次读取到的代码为依据；现有 Perpl 等并行开发文件不在本次修改范围。当前只新增/更新规划文档。

## 3. 服务拓扑、模块与所有权

```text
浏览器
  └─ 已有 Console /api/grid/*（鉴权、校验请求形态、代理响应）
       └─ RuntimeClient ── Unix domain socket ── GridRuntime
            ├─ ControlService：草稿、预览、版本、操作任务
            ├─ AccountSession：账户、市场快照、私有流、回补
            ├─ GridActor[grid_id]：按序消费成交和命令
            ├─ RiskService：预算预留、保证金与保护资格
            ├─ AccountExecutor：实际发送、nonce、限速、订单未决
            ├─ EventStore / OrderJournal / InventoryLedger
            └─ ProjectionService：UI 状态、收益、通知、健康检查

GridRuntime → 交易所
GridRuntime → data/grid-runtime.sqlite（唯一写入者）
Console     → 已有控制台数据库（仅显示投影与原有业务）
```

本机 IPC 首选 `data/run/grid-runtime.sock`，目录 0700、socket 0600；开发和生产均适用 macOS/Linux。运行服务加载同一受限服务器密钥来源，浏览器和 RPC 请求只传 credential_ref。数据目录可通过部署配置覆盖，密钥不写入启动参数、RPC 负载、数据库或事件日志。

交易任务、保护、价格/收益条件、资金费采集、估值都由 GridRuntime 执行；Console 不在线时继续工作。Console 只代理经过授权的控制命令，不在路由函数里新建交易所客户端直接下单。

推荐目录（新增）：

```text
grid_main.py                     # 运行服务入口
entropy_arb/grid/
  config.py, models.py            # 精确值、配置、状态、身份
  identity.py, capabilities.py    # 实际账户/市场、认证资格
  planner.py, sizing.py           # 网格与数量计算、执行前预览
  actor.py, reconciler.py         # 策略事件处理、期望订单与真实订单比较
  runtime.py, rpc.py              # 运行服务与本机控制面
  store.py, migrations.py         # 单写入者、事务、升级
  executor.py, journal.py         # 提交/撤单/查询与持久意图
  risk.py, protection.py          # 预算与保证金、原生/服务端保护
  conditions.py                  # 价格/收益/时间/信号条件与触发去重
  recovery.py                     # 历史回补、未知订单和状态恢复
  inventory.py, pairing.py        # FIFO 库存与网格配对解释
  marketdata.py                   # 真实 OHLCV/mark/funding 数据与覆盖
  automation.py, notifications.py # 有额度的自动资金任务与通知 outbox
  templates.py, exports.py        # 草稿/模板和一致导出
  projections.py, metrics.py      # UI、收益、健康与告警
  adapters/base.py                # 新合同；不破坏旧 maker 合同
  adapters/hyperliquid.py, backpack.py, lighter.py
entropy_arb/console/grid.py       # 网格 HTTP 路由与 RuntimeClient
entropy_arb/webui/v2/grid/        # list/create/detail/adjust/chart
deploy/entropy-grid.service      # 新独立服务
```

适配器可以组合现有签名/行情工具，但对网格公开的新接口不调用会自行生成 ID、float 取整、市场级盲撤的旧方法。保留旧方法行为，逐步抽出共同实现，避免改变旧套利的黄金测试结果。

### 3.1 并发规则

一个 AccountSession 对应一个实际执行账户；账户私有流分发到多个市场，再路由给唯一占用该市场的 actor。网格 actor 不直接进行网络 I/O，也不直接提交数据库事务。

单一 StoreWriter 在专用线程创建并使用 SQLite 连接；运行事件循环不会被 fsync 长时间阻塞。每项事务提交成功后才更新 actor 的可执行状态；提交失败进入停开仓状态。队列有界；交易事件溢出会触发 RECOVERING 并强制回补，行情更新可以合并，二者不能使用同一种丢弃策略。

AccountExecutor 为每个认证过的 signer_scope 串行管理签名序号，按账户/交易所/IP 的实际限流范围调度。优先级依次为撤新增风险单、保护与减仓、查询未决订单/回补、普通补单、首次开仓和批量重建；为所有类别保留有界服务份额，避免低优先级无限饥饿。

需要本地 nonce/counter 的合同，在签名派发前持久提交分配结果与 high-water mark，重启不能按内存初值复用。依赖链上/服务端 nonce 的合同从认证源恢复并核对未决尝试；SDK 隐式递增和重试必须受同一执行器管理。

所有普通新增风险请求在发送前检查控制 epoch、运行模式、订单意图状态、数据时效和预算。暂停/止损使 epoch 前进，队列中旧请求失效。已经发出的请求不能靠 epoch 撤回，必须进入撤单与晚到成交核对。

## 4. 身份、配置与精确值

### 4.1 三种不同作用域

- `account_id`：稳定 UUID，代表实际持仓/权益账户；根据 venue、deployment、owner/subaccount 认证身份解析。同一个账户的不同 API key 仍是同一个 account_id。
- `market_key`：`deployment + native instrument id + dex + product type + settlement asset`；展示 symbol 不是主键。HL 主 dex 的空值是明确值，不转换成 `io`。
- `signer_scope`：交易所实际 nonce/签名并发范围。HL agent 地址、Lighter deployment/account_index/api_key_index 等分别解析；它与 account_id 不一定一一对应。

`grid_id` 是每次创建的独立 UUID，绑定 account_id 和 market_key；重启、调参、移动不变。换账户、换市场、换方向创建新实例并记录 parent_grid_id。凭证轮换不改经济账户 ID，但改变 credential_revision，废弃旧预览与未发送签名任务。

### 4.2 配置示例（仅用于接口说明）

```yaml
schema_version: 1
strategy_type: futures_grid
account_ref: account-uuid
credential_ref: grid-hl-credentials
market_ref: instrument-uuid
direction: neutral                  # long | short | neutral
spacing: arithmetic                 # arithmetic | geometric
lower_price: "100000"
upper_price: "110000"
grid_count: 20                      # 区间数，价格线为 21 条
capital:
  total: "1000"
  reserve: "200"
  asset: USDC
leverage: 3
margin_mode: cross                  # 必须是账户/市场实际支持的模式
initial_position: deferred          # immediate 只允许多头/空头
start:
  type: immediate                   # price/timestamp 由对应 schema 定义
protection:
  stop_loss: {type: price, source: mark, operator: lte, value: "98000"}
  take_profit: {type: price, source: mark, operator: gte, value: "112000"}
  exit_action: flatten
execution:
  grid_order_policy: post_only
  close_slippage_bps: "30"
trailing:
  mode: off
```

schema 禁止未知字段、NaN/Infinity、负资金和不存在的枚举；字段有交叉校验，例如 reserve < total、L < U、多空保护方向、当前能力支持 margin_mode。P1 字段尚未实现时返回 FEATURE_UNAVAILABLE，不静默忽略。

价格、数量、金额和费率从交易所原始字符串解析为 Decimal；禁止经过 float。订单号、成交号、账户索引和大整数在 JSON 中统一为字符串，避免浏览器精度截断。数据库保存规范化 Decimal 文本，禁止不同字符串形式表示同一价格参与不同唯一键。

统一 `qty_base`；涉及交易所合约张数时 adapter 按 instrument 的实际 multiplier 和 native step 转换。归一化记录同时保留 native_qty/native_price 与规则版本。行情绘图可用 Number，提交、校验、执行和账本不使用图表计算出的浮点数量。

## 5. 新交易所合同

```python
class GridVenue:
    async def resolve_account(self, credential_ref) -> AccountIdentity: ...
    async def load_instrument(self, market_ref) -> InstrumentRules: ...
    async def account_snapshot(self) -> AccountSnapshot: ...
    async def market_snapshot(self, instrument) -> MarketSnapshot: ...
    async def submit(self, order: OrderRequest) -> SubmitResult: ...
    async def cancel(self, order: OrderRef) -> CancelResult: ...
    async def lookup(self, order: OrderRef) -> LookupResult: ...
    async def open_orders(self, scope) -> Snapshot[ExchangeOrder]: ...
    async def fills(self, scope, cursor, through) -> HistoryPage[Execution]: ...
    async def funding_payments(self, scope, cursor, through) -> HistoryPage[Funding]: ...
    def subscribe(self, scope) -> AsyncIterator[VenueEvent]: ...
    # 可选能力：set_leverage / set_margin / protection / deadman 等
```

类型和异常是合同的一部分：

| 类型 | 必须字段与语义 |
|---|---|
| InstrumentRules | native id、product、base/settlement、tick、qty_step、min_qty/min_notional、max_orders、维护保证金档位及来源/修订；未知限制标 unknown |
| OrderRequest | intent_id、稳定 client_id、account/market、side、精确 qty/price、TIF、reduce_only、request_deadline、epoch；adapter 不得替换 client_id |
| SubmitResult | ACCEPTED / REJECTED / UNKNOWN，实际 order id、拒绝码、提交阶段、可查询线索；HTTP 200 或 tx hash 不必然代表已挂单 |
| LookupResult | FOUND_OPEN / FOUND_TERMINAL / NOT_FOUND_TRANSIENT / ABSENT_FINAL / UNKNOWN；只有认证过的终局证据才能给 ABSENT_FINAL |
| CancelResult | 已提交撤单、明确终止或 UNKNOWN；请求成功不代表从未成交 |
| Execution | 稳定 execution_key、order/client id、原始 trade id、side、qty_base、price、fee_amount/fee_asset、maker 标记、exchange time、received time、原始序号与终局性 |
| HistoryPage | records、next_cursor、coverage_start/through、complete、truncated、source_revision；返回空数组不自动等于覆盖完整 |
| AccountSnapshot | account_id、资产分币种、可用/占用保证金、实际仓位、保证金模式、原生强平指标、源时间 |

SubmitResult 自带的累计 executed quantity 是恢复线索，不再作为第二份成交入账。只有带统一稳定成交键的原始 fills 才进入库存。实时与 REST 回补经过同一个 normalize 函数，键的范围由 adapter 认证，不能假定 trade id 在所有市场/账户全局唯一。

能力注册单独增加 `grid_contract_version` 和认证 resolver；保持原 `VenueSpec` 字段兼容。`maker_capable=True` 不是放行网格的条件。每项能力返回 scope、限制、认证版本、日期和证据；账户/部署改变后重验。

只读 `MarketDataProvider` 独立提供 OHLCV、bid/ask/last、mark、资金费率/支付时点和历史覆盖；复用 AccountSession 的连接与限流预算。每条记录带 source、market_key、规则版本、源时间与 final/partial 标记。缺 K 线能力时显示行情能力限制，不从盘口样本生成假 K 线；推荐与回放额外校验历史费用/资金费覆盖。

### 5.1 首两家与后续接入

首个合同候选为 HL 主永续，第二个候选为 Backpack；审计失败时交换顺序，不能降低合同要求来赶首发。

- HL：外部 cloid、ALO/GTC/IOC，按 oid/cloid 查询，原始用户 fills 和时间回补；成交关闭聚合以保留粒度；实现原生保护、杠杆、费用及资金费支付读取。cloid 是关联标识，不直接等于重复提交幂等承诺。[交换接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)、[查询接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- Backpack：外部 clientId，订单/成交历史与资金费支付、私有流；确认 clientId 的有效范围、复用限制和终局查询方式。回补包括强平等系统成交，不仅 User fills。[官方 API](https://docs.backpack.exchange/)
- Lighter：主网和 RH 单独认证；持久 client_order_index 与 nonce 调度，区分交易提交/接受/成交，补 authenticated account orders/trades 与历史覆盖。[官方 WS](https://apidocs.lighter.xyz/docs/websocket-reference)
- 其他所/dex，包括 Perpl：以同一合同测试、恢复演练和流动性/容量认证开放；不以接入名单宣称功能已齐。

费用和历史分页尤其要按官方语义归一化。例如 HL 的 fill fee 已包含可选 builderFee 时，不能再扣一次；历史条数限制要转换为覆盖缺口，不只截断列表。[HL 查询接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)

## 6. 持久模型与事务

### 6.1 表与关键约束

以下为新运行库中的最小模型，具体 SQL 在 G02 编码时生成并通过迁移测试。

| 表 | 主字段 | 关键约束 |
|---|---|---|
| accounts / credentials | account_id、认证身份、credential_ref/revision、signer_scope | 凭证表仅存引用/元数据，无秘密；经济账户身份唯一 |
| instruments / capabilities | market_key、规则与能力 snapshot/revision | 老版本不可覆盖，订单引用当时的规则 |
| grids | grid_id、account/market、status、desired_mode、epoch、active_revision | 同 account/market 的非结束占用唯一；有残仓仍占用 |
| grid_versions / levels | revision、规范配置 hash、原始输入、规则版本、价格线 | 同 grid/revision 的 canonical price 唯一；历史版本不可改 |
| events | seq、event_key、scope、event/received time、payload、schema | event_key 唯一，append-only；原始事件不可改，处理进度另存 checkpoint |
| order_intents | intent_id、cause_key、grid/revision/level、qty、policy、epoch、state | cause_key 唯一，同因果事件不产生第二个补单意图 |
| exchange_orders / attempts | account、client/order id、intent、累计成交、终态、raw receipt | account/client id 唯一；请求尝试 append-only，不覆盖未知尝试 |
| executions | execution_key、order、Decimal 数量/价/费、源时间与序号 | 稳定键唯一，手续费补充通过新修订事件更新，不再次计数量 |
| inventory_lots / allocations | lot、真实余量、来源 fill、减仓 reservation | 减仓预留不得大于真实可减库存；配对消耗不得重复 |
| grid_pairs / pair_allocations | 经济循环、买卖 fill、数量和费用分配 | 同 fill 已分配量不超过真实量，费用守恒 |
| capital_movements / budget_reservations | 资金分配/变化、预留、实际转移证据 | 资金流不记收益；未决订单仍占预算 |
| operations / operation_steps | 请求幂等键、预览、版本、执行阶段、每步结果 | 幂等键 + 请求 hash；同 grid 只能一个变更任务 |
| previews / request_dedup | 配置、行情/账户/能力修订、过期时间、结果 | 预览被消费后不能用于另一个请求 |
| scope_claims / counters | 账户/市场占用、signer counter、runtime epoch | 硬占用和“进程存活租约”分开；进程死不释放残仓占用 |
| checkpoints / coverage / projections | 应用到的 seq、覆盖边界、完整度、快照版本 | 快照是可重建缓存，不覆盖事件事实 |
| drafts / templates | owner_scope、严格配置、schema_version、revision | 仅复制参数；实例/成交/凭证秘密不被复制 |
| automation_policies / automation_runs | policy_revision、额度/来源、触发事实、operation_id、计数 | 触发键唯一；在事务中先占累计额度，再启动资金 saga |
| notification_preferences / notification_outbox | scope、渠道引用、event_key、状态、重试时间 | 通知键去重；无秘密，交易提交不依赖通知成功 |
| export_jobs | grid、过滤区间、source_seq、方法/覆盖版本、结果引用 | 导出固定一致边界，不混合前后两次修订 |

order id 或交易键缺失的事件先入 quarantine；不能按价格、数量、秒级时间猜测去重后放进已核对账本。同一 execution_key 收到不同数量/价格时为 DATA_CONFLICT，不静默选最新值。

### 6.2 三个关键事务

**创建事务**：校验预览及幂等键 → 注册实例与配置版本 → 占用 account/market → 预留预算 → 写 START 操作。交易所杠杆等网络动作在提交后执行并单独核验，不在 SQL 事务里等待网络。

**成交事务**：插入新的 execution → 更新订单累计量 → 更新真实库存、费用分配、配对和可减仓预留 → 写补单/调整意图 → 写新 checkpoint/projection seq。事务提交之后 executor 才能发送补单。重复 fill 只校验一致性，不重复改变任一数量。

**发送事务**：选择可执行意图，绑定唯一 client id，写 dispatch attempt 与预算状态，提交 → 网络发送 → 回执事务。发送前崩溃重启时不能凭 dispatch 状态判断已经/尚未发送，按 UNKNOWN 处理并查询。

### 6.3 数据库运行规则

SQLite 设置 WAL、foreign_keys、synchronous=FULL，使用本地持久盘，明确 busy timeout；不放 NFS 或临时目录。Runtime 通过本机进程锁保证唯一写入者，StoreWriter 只拥有一个写连接。[SQLite WAL](https://www.sqlite.org/wal.html)

迁移采用显式 BEGIN/COMMIT 与逐条执行，schema version 与迁移记录一起提交；失败回滚到上一个版本。不能直接照搬一段代码并假设 `with connection` 内的 executescript 一定提供完整迁移事务。升级到未知更高版本时拒绝启动交易。[Python 3.11 事务控制](https://docs.python.org/3.11/library/sqlite3.html#transaction-control)

备份用 SQLite online backup 生成一致快照；manifest 从已完成的备份读取 schema/方法版本和最后 seq，不拼接实时库的另一个时间点。只读备份源连接与目标连接仅供备份使用，不参与业务写入。不要运行中仅复制 `.sqlite` 而遗漏 WAL。恢复备份后默认 RECOVERING，禁止直接从旧快照续单。[SQLite 在线备份](https://www.sqlite.org/backup.html)

## 7. 订单意图、重试与真实状态

### 7.1 订单状态

```text
PLANNED → DISPATCHING → ACCEPTED → OPEN / PARTIAL → FILLED
                  ├─ REJECTED
                  └─ UNKNOWN → 查询/回补 → 已确认状态或继续 UNKNOWN
OPEN/PARTIAL → CANCEL_REQUESTED → CANCEL_PENDING → CANCELED / FILLED / UNKNOWN
```

终态与累计成交量独立；CANCELED 可以已有成交。订单状态更新迟到不得把 FILLED 回退为 OPEN；成交补充可以更新终态累计量。orderAccepted 晚于 fill 到达时，按预先持久化的 client id 映射处理。

客户端幂等只保证本系统重复请求复用同一实例/操作；交易所 client id 必须逐家认证。HL 128-bit cloid 可以使用持久意图 UUID 的认证编码；窄整数 client id 使用数据库单调计数器和永久映射，不能直接截短 hash。耗尽/不允许复用时停止创建并显示原因。

### 7.2 重试表

| 结果 | 动作 |
|---|---|
| 明确规则/保证金拒绝 | 记录拒绝，修正预览或停开仓；不得重试同一个无效请求 |
| 明确 post-only crossing 拒绝 | 保留该价位的需求，等可 maker 再挂；不默认改价或吃单 |
| 已证明未被接受的 429/限流 | 根据 venue 限流语义退避，重新检查 epoch、预算和价格；新尝试可否复用 client id 由合同决定 |
| 超时、5xx、连接中断、tx 提交未确认 | UNKNOWN，保留预算并查订单/成交；不能盲目重发 |
| NOT_FOUND_TRANSIENT | 查询退避并回补；单次 404 不证明未下单 |
| 认证过的 ABSENT_FINAL 或明确未发送 | 原意图可按合同重新提交；保留所有请求与查实证据 |
| 无法判定 | NEEDS_ATTENTION，相关新增风险停止；不把超时倒计时结束当作成功或拒绝 |

恢复 UNKNOWN 的证明条件由 adapter 提供，包含请求有效期/接受语义、查询可见性、历史覆盖和结算边界。无法证明终局缺席的场景保持未决；人工处置也必须先核对真实订单/仓位，不能“标记失败后再下一单”。

### 7.3 期望订单与实际订单

逻辑档位、订单意图、交易所实际挂单分别保存。reconciler 根据逻辑需求聚合同价、同方向、同标志的数量；首版同一个物理 slot 只允许一张已确认活动单，加量/减量先撤查再替换。可减少撤单的原生 modify 是后续优化，不能假设它原子执行且没有晚到成交。

同一价格出现相反方向需求时禁止同时发出可能自成交的订单。先处理真实可减库存与已存在订单，延期的开仓需求标 BLOCKED_SLOT；内部抵消需求不能伪造成交或网格利润。中性普通网格单可跨净仓位零点，不能全部设 reduce_only；多空的退出单和退出模式强制 reduce_only。

创建、补单、移动和热修改共用同一 reconciler，不能各写一套补单规则。

## 8. 网格布局、数量求解与预算

### 8.1 规范价格线与首次布局

先将下界向上、上界向下规范到 tick，再生成等差/等比的 N+1 条线，逐条用确定性的最近 tick 规则规范化；相等距离的取整规则固定并版本化。发现重复、出界或净间距不够时返回具体字段错误，不临时改变格数。

`grid-calc-v1` 的具体 context、tick 正中点取高、mark 参考价、按参考价分区/按盘口过滤、初始真实库存近出口优先分配，以执行交接第 4–5 节为准；新规则必须增加方法版本和验收向量。

一条价格线只保留一个 canonical price，买卖方向改变不重新取整到不同价格。等比计算用固定 Decimal context 和规则版本；最终价格线持久化，重启不重新用另一套浮点算法生成。

| 模式 | 初始化 | 长期库存约束 |
|---|---|---|
| long / deferred | 为当前可被动挂买的区间下端建买单；尚未买入不挂卖出退出单 | 只开多；实际成交后建立相邻高价 reduce-only 退出需求 |
| short / deferred | 为当前可被动挂卖的区间上端建卖单；尚未卖出不挂买入退出单 | 只开空；实际成交后建立相邻低价 reduce-only 退出需求 |
| long / immediate | 预览需预建的高位循环库存；有限价保护的初始 IOC，按实际成交分配循环 | 不用计划开仓量提前挂无库存支持的退出单 |
| short / immediate | 对称计算需预建的低位循环库存，初始 IOC 按真实成交分配 | 同上 |
| neutral | 距启动参考价最近的线留空，等距离取较高线；低线买、高线卖 | 可有正/负净库存；“中性”不意味着不承担方向风险 |

long 的循环入口为 `P_0…P_(N-1)`、出口为相邻的 `P_1…P_N`，short 对称。deferred 下未激活的循环，在对应入口成为当前有效被动报价且资金校验通过后才激活，不在价格未到达时凭空填充库存。

启动价在区间外，首版进入 WAITING_RANGE，保护与行情继续监控，进入区间后重新预览初始化；不在区间外默认立即建立满仓。运行后出区间按产品规则保留区间内订单与保护，限制新的风险需求。

部分成交按 qty_step 累计，达到 venue 最小量/名义后建立相邻补单需求；暂时不足部分以 DUST_PENDING 显示并计入真实库存。同档补单冲突通过 reconciler 解决，不能按每个回调直接调用 adapter。

### 8.2 每格量的求解

求解变量为整数数量步数 k，`q_base = k × qty_step_base`。最小值同时满足所有会被使用的价格线的 min_qty/min_notional；若低价退出单也有最低名义限制，一起纳入。合约张数换算在每一步保留精度。

planner 为候选 k 输出完整的初始布局、未成交订单、可能方向库存和场景；至少覆盖持续下跌、持续上涨、先涨后跌、先跌后涨、初始建仓部分成交、止损触发且平仓发生滑点、当前其它网格的不利库存。

认证风险模型计算每个场景中的：

- 真实初始仓位 + 可能成交后的净仓位。
- 交易所初始保证金/维护保证金档位与尚未成交的订单冻结。
- 相对快照的持仓浮亏增量、退出成本、费用和风险缓冲。
- 全仓/逐仓实际边界、账户其它品种的预算占用。

使用账户风险模型的“全场景资金需求”保守包络，只有证明其随 k 非递减时才做整数二分；否则枚举有界候选。规则缺失或无法验证账户风险模型时不给“可实盘创建”的结果。最终解再次按交易所 native 数值校验。

必须同时满足：普通交易的资金需求 ≤ total−reserve，包含不利场景的需求 ≤ total，以及账户整体资金/风险约束。不能仅校验 `q × N × price / leverage`。原生快照已有保证金冻结和系统预算预留分别建模，不能重复扣同一笔资金，也不能漏掉未决订单。

Hyperliquid 新建/显式重新求量时，再将普通交易资金需求限制为普通预算的 90%，并展示实际运行余量，避免数量刚好用尽预算后因极小浮亏或手续费立刻暂停。恢复、逐次发单和固定原数量的风险核验仍使用完整普通预算及原来的不利场景/全账户门槛，不静默缩小旧数量、不自动增加资金或降低风险门槛。

已有活动网格的最大潜在风险仍保留在账户预算中，不把目前空闲的保证金二次分配给第二个网格。创建、追加规模、移动的所有订单在同一账户队列内原子获得预算预留，发送前再读新快照。

预算是系统执行上限，不能把全仓账户的策略投入写成交易所保证金隔离或最大亏损保证。资金费率用于情景估计，支付事件用于账本；预计费率不能充当已经发生的费用。

### 8.3 planner 的输出合同

`Plan` 必须包含价格线与逻辑 slot、每格量、首次真实订单需求、初始库存动作、资金分配、逐格净价差区间、最坏库存、账户风险场景、保护方式/覆盖缺口、能力限制、费用来源以及计划 hash。

每项数字带单位/币种；不确定项是 null + reason，不输出 0。预览记录所有规则与账户快照修订，UI 直接展示 plan 的数值，不另算第二份每格收益或最低资金。

## 9. 状态机、生命周期与恢复

### 9.1 状态与允许动作

`status` 描述当前执行阶段，`desired_mode` 为 ACTIVE / PAUSED / REDUCE_ONLY / STOPPED；运行进程是否存活是第三个字段，不能混用。

| 状态 | 允许发送 | 转移条件 |
|---|---|---|
| DRAFT / READY | 无交易单 | 有效预览并提交创建 |
| WAITING_TRIGGER / WAITING_RANGE | 核对与保护，无网格新增风险单 | 条件成立、范围有效、账户校验完成 |
| STARTING | 按启动任务逐步建立；每步重验 | 初始真实库存、保护、挂单已核验 |
| RUNNING | 当前 epoch 的合法网格单 | 无未决缺口与风险阻断 |
| PAUSING / PAUSED | 撤普通网格单、保护、回补 | 撤单和晚到成交确认后 PAUSED |
| REDUCE_ONLY | 已核对库存支持的减仓、保护、查询 | 不生成普通开仓需求 |
| RECONFIGURING / RECOVERING | 核对、必要保护/撤单；不发新开仓 | 阶段任务或恢复屏障通过 |
| STOPPING | 撤普通单、核对、按选择退出 | 订单和仓位有最终证据 |
| CLOSED | 无交易单 | 已平仓且网格/保护订单核对清理 |
| STOPPED_WITH_POSITION | 保护与已授权的残仓处置 | 仍占市场，保持收益与监控 |
| NEEDS_ATTENTION | 按失败类型限定保护/撤单/减仓，禁新开仓 | 明确核对与重新预览恢复 |

暂停、停新增风险、退出不能由 SIGTERM 代替。普通运行服务关停流程进入停开仓并撤普通单、核对，将状态落库，保留可识别保护和仓位；若超时则记录未决并让原生掉线兜底生效，下一次启动先恢复核对。

### 9.2 创建、暂停与结束

创建任务：占用/预算提交 → 订阅与初始账户核对 → 确认账户模式/杠杆 → 建立可支持的保护 → 初始建仓（如有）→ 实际成交归属 → 挂单 → 再核对 → RUNNING。

如果保护需要先有持仓才能建立，要在预览中标明首次成交到保护确认之间的服务端覆盖阶段；新增风险受到该阶段专用上限限制。不能声称整段过程都已被原生条件单保护。

暂停任务：epoch 前进、失效未发送开仓/普通补单 → 撤普通网格单 → 回补撤单期间成交 → 核对真实挂单 → PAUSED。价格保护、估值、资金费和真实库存继续运行。恢复需要新的风险预览，不直接重放旧补单队列。

结束平仓任务：冻结普通需求 → 撤网格单、处理未知提交 → 回补并核对真实净仓位 → 预留 reduce-only 数量 → 受滑点限制的退出 → 再回补核对 → 清理无仓位保护 → CLOSED。退出不足最小量、流动性不足、限速或订单未决时显示 PARTIAL/NEEDS_ATTENTION 和真实剩余量。

结束留仓任务：冻结/撤普通单 → 回补核对 → 保留能独立存活的保护及明确交接记录 → STOPPED_WITH_POSITION。进程停止和“结束”都不释放有残仓的市场占用。残仓平仓也走持久操作任务。

止盈止损触发时先持久记录触发事实、冻结配置的 exit_action 与新 epoch，优先中断普通任务，再进入相应结束流程。原生条件单的成交也触发同一路径；不能让剩余网格单在保护平仓后重新开仓。触发事实不是“全部仓位已平”的证明。

### 9.3 恢复屏障

启动、私有流断线、队列缺口、时序冲突和外部改仓触发同一流程：

1. 获取 runtime/账户/signer 控制权，停止新的风险派发；让旧 epoch 请求失效。
2. 订阅私有流，将到达事件持久缓存；有限队列溢出扩大覆盖缺口并停止恢复推进。
3. 从最后“已完整覆盖”的成交/资金费 cursor 向前取经过认证的重叠区间，再分页回补到截止边界。不能使用“最后收到一笔的时间”冒充已覆盖边界。
4. 查询所有本策略正常/条件挂单和 UNKNOWN 请求，匹配 stable client id；发现外部订单/仓位变动进入处置流程。
5. 按可靠交易顺序重建真实库存、订单累计量和 grid 配对映射；跨源重复只入一次。无法证明顺序时记录时序不确定，暂停相关补单，不靠随意排序 trade id 宣称完整。
6. 将快照边界与缓存中的晚到 fill 对齐，核对每张订单成交量和实际账户仓位；需要时再次取快照/回补，不要求市场完全静止。
7. 核对原生保护、能力/凭证/保证金与费用修订，重建最新版本的合法期望订单。
8. 只有历史覆盖、未知订单、数量守恒、预算和保护均通过，才允许恢复相应 desired_mode；否则 NEEDS_ATTENTION。

交易所历史保留不足、单时间桶分页截断、缺稳定成交键、累计成交和明细不一致均构成恢复失败。需要完整回补时宁可维持暂停，也不以手动余额/仓位快照虚构丢失的交易利润。

### 9.4 外部变更

外部手动订单、非本策略来源的成交、强平/ADL、用户改杠杆、市场规则变化均有独立事件。首版不自动接管既有手动仓位，也不按比例分摊其收益。发现漂移先停止新增风险，展示已知策略数量和账户真实数量的差额；明确处置后创建新的归属检查点。

同一个成交的时间/费用后补是“事实修订”，不是新成交。影响已公布收益时新增 reconciliation 版本并显示修订来源，不能静默重写过去已核对数字。

## 10. 热修改、移动和资金操作

所有操作记录不可变 request、base_revision、preview_hash 和可恢复步骤，采用持久 saga，不假定多张交易所订单能在一个原子事务中撤换。

| 操作 | 共同步骤 | 特有规则 |
|---|---|---|
| 修改区间/格数 | 预览 → epoch 前进 → 停开仓 → 撤旧开仓 → 核对晚到成交 → 建新版本 → 核验 → 启用 | 旧库存保留或退出由用户选择；只在任务完成后 active_revision 切换 |
| 移动网格 | 同上，普通补单与移动不能同时改同一 slot | 固定宽度平移，保留旧库存退出需求；费用、次数、边界、冷却、预算先校验 |
| 补风险缓冲 | 预览 → 更新预算/原生保证金动作 → 查询核验 | 不改每格数量；没有原生隔离时标明只是系统保留预算 |
| 增加交易规模 | 重算数量和库存 → 与调参相同的订单变更 | 预览初始补仓量与费用，未决时原预算不释放 |
| 减少资金/提取利润 | 风险校验 → 释放安全的预算或执行允许的实际划转 → 核验 | 预算释放与原生转账分别记录，不能拿浮盈或待核对利润当可提现现金 |

执行失败按已完成步骤恢复，不再次执行已确认的保证金转移或市价动作；转移请求若未决，先查实，不能凭 task retry 重复划转。首版没有真实转移权限/接口时，明确返回 unavailable，不执行别的等价猜测。

保留库存时，旧版本的 inventory lot 与退出映射持续存在，新版本建立新开仓/补单需求；不把旧库存重命名为新网格的新资金。配对和订单都能追到原版本。

激活新版本前重新验证计划；普通任务失败默认不恢复旧开仓。只有另一次完整预览通过，才能按显式恢复动作重新启用旧版本。暂停/结束优先中断调参任务，留存中间状态。

### 10.1 自动补缓冲与再投资

P1 提供持久 `AutomationPolicy`，默认关闭，配置限定资金来源、单次/累计额度、最高交易规模、最小间隔、触发阈值和政策有效期。用户启用时预览这些边界；运行中不能扩展来源、额度或杠杆。

自动补缓冲调用与手动相同的补缓冲 saga，不改变每格量；自动再投资调用增规模 saga，只使用已对账且可安全释放的现金利润。浮盈、资金费估计和覆盖不全的利润不进入再投资池。经济触发键去重，额度预留和创建操作在同一事务完成；未知划转继续占额度。

账户/账本/保护不完整、已有变更任务、资金来源失效或额度不足时停止自动任务并显示原因。暂停、只减仓和结束立即禁止自动增加资金规模；失败不凭重试恢复自动投入。政策、实际占额与每次动作在详情页可追溯。

### 10.2 收益保护、移动止盈和高级触发

ConditionEngine 使用统一版本化条件：type、source、operator、threshold、有效期、cooldown、action 与政策 revision。价格条件绑定 last/mark 等明确源；金额/ROI 条件绑定完整账本的固定分母和估值方法。移动止盈的收益 high-water mark 持久化，重启不重置。缺收益覆盖时条件标 unavailable，停新增风险，不用估计收益触发已承诺的精确保护。

时间条件统一保存 UTC，界面显示用户时区；RSI 只使用已结束且连续覆盖的 K 线，记录周期、参数、warm-up 与指标版本。webhook 使用独立限定 scope 的签名凭证，校验 event id、时间窗和重放；不共享交易所密钥。所有触发先持久去重，再提交同一生命周期操作，不直接调用 adapter。止损/暂停优先于移动、再投资和普通补单。

移动网格的边界、次数和冷却与配置 revision 一起持久化；移动失败不消耗成功次数，但保留触发与任务证据，避免每次行情回调制造新任务。运行移动与修改区间遵守相同 saga，旧库存退出映射继续有效。

## 11. 风控、保护与占用治理

### 11.1 两类占用

**经济占用**是账户/市场/预算的长期所有权，有残仓和未知订单就不自动过期；**进程控制权**是短期租约/本机锁，崩溃可由新 Runtime 接管。不能因进程心跳超时把市场释放给另一个网格。

Runtime 的账户和 signer 控制仅覆盖网格写入者，网格模块内部使用统一持久占用与本机锁。套利启动、平仓和诊断不查询网格占用，不因网格 Runtime 离线而被阻断。使用者负责为两套系统配置不同实际资金账户及各自凭证。

网格不接管已有套利进程，不自动停旧实例，也不宣称支持共享账户。网格实际账户身份或授权关系无法解析时，仍拒绝网格真实开单；该校验不扩展到套利模块。

允许多个网格在同一网格专用账户管理不同合约；同账户同市场一网格，包括已停止残仓。账户是否与套利重复由使用者保证，不实现跨策略查重或硬阻断。

网格占用协议由 Runtime 单写持久 claim，记录 owner_type/owner_id 和认证作用域，仅供网格内部执行与诊断。进程死亡只释放内核锁，经济 claim 保留待恢复；只有订单/仓位核对证明成立后才释放或转移，不靠租约自动删除。旧套利不参与该协议。

### 11.2 三道校验

1. 配置/预览：schema、市场规则、费用、最小量、最大库存、能力和账户占用。
2. 每次派发：当前 epoch、数据时效、真实 free margin、预算预留、单笔价格/数量与风险增量。
3. 持续风险：账户净仓位和占用、规则/费率变更、原生保护覆盖、强平指标、资金费和系统缺口。

行情过期、private stream 未确认、磁盘/数据库故障、预算不可用、保护确认失败和未知订单都能阻断新增风险。某所独立故障优先隔离到对应 AccountSession；数据存储失效影响所有网格并关闭所有新增风险通道。

原生保护每个订单有来源/数量/方向/价格和查询证据。保护更新遇到非原子撤换，先确认仍有效的覆盖再允许加仓；无可证明覆盖时限开仓并明确当前服务端保护阶段。所有已经发出的风险订单也纳入保护的潜在库存，不只看当前 position。

死手开关记录 scope、deadline、renew interval、触发配额和剩余次数。账户级 cancel-all 只有专用账户或明确兼容作用域才可用；它只能撤单，不能平仓。原生订单失效时间和原生保护可以成为另一类认证兜底。独立监控只读检查并告警，不在运行服务死亡后随意创建第二套共享 signer 的下单器。

### 11.3 首版运行参数

参数在 RuntimePolicy 中版本化，不能散落在 adapter 常量。初始建议预览有效期 30 秒、UI 快照 1 秒、账户巡检 10 秒；策略与 venue 可收紧。盘口时效、账户保证金时效、私有流可确认边界、查询退避、请求有效期和掉线续期必须按目标部署认证，不能对所有所硬设同一个秒数。

重试次数有上限；不自动加杠杆、不自动扩大资金来源。自动资金动作仅在第 10.1 节已启用政策的额度内执行。退出失败显示剩余风险并告警，通知失败不阻止风险动作；没有通知渠道时仍在网站与服务健康状态可见。

## 12. 库存、配对和收益

真实库存账沿用现有 Decimal/FIFO 方法语义，但把纯函数从控制台依赖中抽离成可共用库；旧调用保留兼容导入。网格事件不硬塞入带 CSV 导入来源的旧表，运行库是权威，控制台通过带 source_seq 的投影读取。

库存账以 actual fills 计算已实现毛盈亏和剩余 lots；网格配对账以真实 fill 数量链接解释完成的经济循环。一个 fill 的经济配对数量和费用不能重复消费；FIFO 归属与网格配对归属分别记录，不能把配对利润再加到 FIFO 盈亏上。

未完成配对的累计实际交易费计入总收益，未来配对完成再改变它在解释项里的分类，不能再扣一次。资金费采用实际支付，缺覆盖则 incomplete；查询明确覆盖且无支付才是该区间的零。

主公式保持：`净收益 = 毛已实现 + 浮盈变化 + 净资金费 − 全部交易费 − 其他实际费用`。配对利润不是主账本余额，也不作为保证金可用资金或独立收益型止损的触发金额。

公式各项先统一到明确的结算/计价币种。非结算币手续费保留原币费用与当时的转换来源/时间；缺可靠汇率时保留分币结果并标 incomplete，不按稳定币恒等于美元或用今天汇率补造历史净收益。

### 12.1 必须通过的数字算例

线性合约 multiplier=1；手续费 0.01%，无资金费；初始无仓位，期末 mark=1010。

- 买单计划 0.010，在 1000 部分成交 0.004；实际买入费 0.0004。
- 相邻卖单在 1010 成交 0.003；实际卖出费 0.000303。
- 真实余仓 0.001，原买单未成交 0.006。
- 配对毛收益 `0.003×10=0.030`；分配买费 0.0003，配对净收益 0.029397。
- 剩余库存浮盈 0.010，未配对买费 0.0001；期间总净收益 `0.030+0.010−0.000703=0.039297`。

该买 fill 重放任意次数不改变结果；再补到手续费明细也不能重复加仓。卖单余量与后续补买需求按真实量更新。还需通过跨零净持仓、跨多档成交、非相邻配对、负费率返佣、资金费后补和强平算例。

显示 reconciled / estimated / incomplete / no_data 及方法版本、覆盖区间、缺项。期间边界估值必须使用当时持久记录的 mark/source，不能用今天价格回算过去。旧数据缺边界估值时仅给可证明的部分，不生成完整净收益。

资金流单独建账，实际存取/预算分配/释放不算收益。简单 ROI 分母和时间加权收益率按产品规格实现，资金变更时分段；收益率触发所用分母规则冻结且可追溯。

## 13. HTTP/RPC 合同与错误码

### 13.1 新 HTTP 路由

| 路由 | 请求核心字段 | 响应/副作用 |
|---|---|---|
| `GET /api/grid/capabilities` | account_ref、market_ref | 有效规则、资格、限制、费用和保护来源 |
| `POST /api/grid/previews` | purpose=create、严格 config | Plan、检查结果、preview_id/hash、expires_at；无交易单 |
| `POST /api/grid` | preview_id/hash、request_id、confirm_market | 202，grid_id、operation_id、当前 seq；只接受有效计划 |
| `GET /api/grid` | filters、cursor、limit | 包含运行、停止和残仓的实例；稳定排序和分页 |
| `GET /api/grid/{id}` | 可选 since_seq | 真实状态、active_revision、desired_mode、健康与收益证据 |
| `GET /api/grid/{id}/levels|orders|fills|pairs|events` | cursor、limit | 来源/版本/真实量与状态，无假成交 |
| `GET /api/grid/{id}/performance` | start/end/timezone/method | 统一账本结果、覆盖和缺项 |
| `POST /api/grid/{id}/exports` | kind、区间、字段/格式 | 一致导出任务，带 source_seq、覆盖和方法版本 |
| `POST /api/grid/{id}/operations/previews` | action、base_revision、参数 | 执行步骤、仓位/订单/资金影响、预览 |
| `POST /api/grid/{id}/operations` | preview_id/hash、request_id | 202 + operation_id；非同步“成功” |
| `GET /api/operations/{id}` | operation_id | 按 source 路由到旧服务或 GridRuntime，保留兼容形态 |
| `GET /api/grid/requests/{request_id}` | 已授权作用域中的 request_id | 超时后查询原请求的实例/操作结果；不创建任务 |
| `GET /api/grid/stream` | 授权的 scope、resume_seq | 带 seq 的快照/事件流；断流后客户端恢复快照 |
| `GET /api/grid/market-data/bars` | market_ref、interval、from/to、cursor | 真实 OHLCV、来源/覆盖、未完成 K 线标记 |
| `GET/POST /api/grid/templates` | 模板严格配置、schema/revision | 参数模板；复制得到新草稿，不继承实例/仓位 |

所有 Decimal 与大整数是字符串；时间为有明确时区的 ISO 8601。响应包含 schema_version、source、as_of、projection_seq、stale、reconciliation_status，运行状态与收益证据独立。

预览绑定 config hash、account/market、规则/能力/凭证 revision、风险快照、base_revision、保护政策和有效期。成功消费时与实例/操作写入同一事务。相同 request_id + 相同 request hash 返回原结果，换请求内容则 IDEMPOTENCY_CONFLICT；网页重试不能产生第二份订单任务。

预览价格轻微变化是否可沿用，由持久化的 tolerances 决定；资金变化、配置变化、保护/能力修订或超出价格容忍范围需要重预览。执行前的再次检查失败不偷偷按新数量执行。

统一错误形态：

```json
{
  "error": {
    "code": "PREVIEW_STALE",
    "message": "账户资金已变化，请刷新开单预览",
    "retryable": false,
    "field_errors": {},
    "details": {"changed": ["account_snapshot"]}
  },
  "request_id": "client-request-uuid"
}
```

错误码与 HTTP 状态固定如下；异步任务提交后的交易所拒绝写入任务结果，不回头改变已经返回的 HTTP 202。

| HTTP | 错误码 |
|---|---|
| 400 | VALIDATION_ERROR（请求格式或 schema 错误） |
| 401 / 403 | AUTH_REQUIRED / FORBIDDEN |
| 404 | NOT_FOUND（授权范围内实例或请求不存在） |
| 409 | ACCOUNT_IN_USE、SIGNER_IN_USE、PREVIEW_EXPIRED、PREVIEW_STALE、VERSION_CONFLICT、IDEMPOTENCY_CONFLICT、ORDER_UNKNOWN、DATA_CONFLICT、RECOVERY_INCOMPLETE |
| 422 | UNSUPPORTED_INSTRUMENT、FEATURE_UNAVAILABLE、CAPABILITY_UNVERIFIED、ACCOUNT_UNRESOLVED、INSUFFICIENT_MARGIN、BUDGET_EXCEEDED、PROTECTION_UNAVAILABLE |
| 429 | RATE_LIMITED（控制面请求，带 Retry-After） |
| 503 | MARKET_STALE、RUNTIME_UNAVAILABLE |

返回可操作原因，不能把所有错误写成请求失败。网格操作 ID 使用独立 `gop_` 命名空间，旧 ID 继续按原路径查询；Console 不依赖内存映射判断操作来源。

### 13.2 本机 RPC

RPC 使用 aiohttp UnixConnector，内部接口与 HTTP DTO 共享模型，但不开放公网。command/preview 返回明确操作 id；客户端超时后按 request_id 查询，不能重新制造一个 request_id。

消息带 protocol_version、runtime_epoch、request_id 与 deadline，启动时进行兼容握手。不匹配时 Console 显示需升级并禁写，旧 Runtime 继续已知策略管理。Console 在线与 Runtime 在线分别显示。

推送由 Runtime 保留有界序号窗口；过期 resume_seq 返回 RESET_REQUIRED，前端拉完整快照。UI patch 必须包含 base_seq，对不上就丢弃 patch 并拉快照；不存在“连上后看起来绿了但数据还是昨天”的状态。

### 13.3 鉴权与密钥

新增控制路由必须校验现有授权，并补 Host/Origin 与浏览器写入防护；本机免 token 模式也不能默认信任任意网页跨源写。命令行无 Origin 的请求通过显式服务端授权渠道区分，不因为少一个 header 自动可信。

推送优先采用支持 Authorization header 的 fetch 流；不把长期控制台 token 加到新网格流 URL。若选择 WS，则另发短期、一次性、限定 scope 的升级票据并脱敏日志，不直接复用 query token。不改变旧连接的兼容性时，旧路径仍标明为迁移项。

RPC/数据库/导出仅含凭证引用与公开认证元数据；私钥只在受限服务内存和现有秘密来源。日志对签名、Authorization、认证 token 和请求秘密字段统一脱敏。凭证更新先使旧预览失效，再让 Runtime 通过受限来源重载；不能仅因为旧页面显示“密钥已保存”就认为运行实例已经换钥。

## 14. 前端与现有总览整合

新增路由 `#/grid`、`#/grid/create`、`#/grid/:id`、`#/grid/:id/adjust`；静态路由优先，避免把 create 当作实例 id。页面遵守现有 `mount(container, ctx)` 和 `destroy()`，退出页面取消请求、订阅与 ResizeObserver。

创建页面的图表只修改配置草稿；拖线与表单统一进入同一个 draft store、严格格式化后发 preview。input 改变立即将旧预览标为过期，debounce 只用于减少请求；迟到 preview 通过 request sequence 丢弃。切换账户/交易所/合约清理不兼容字段，但保留可解释的原草稿。

显示价格线、逻辑待激活档位和真实订单三种层；订单必须带来源 revision。前端不能把价格穿线动画当作真实成交。图表数据新增真实 OHLCV/mark/funding 数据合同，`venue_bars.py` 的分钟盘口不当 K 线使用。

先验证 Lightweight Charts 等图表方案的原生拖线/覆盖层实现方式与许可，固定依赖版本并本地打包；图表模块具有可替换接口，策略逻辑不依赖图库。此规格不假设某个库原生提供所有控件。

总览采用新 `StrategyRef = {source, type, id}` 和新增聚合目录接口；旧双腿 `/api/strategies` 响应不塞带空 hedge 的假记录。V2 更新后可以将两来源合并展示和跳转；账户聚合按 account_id 去重，网格资金与旧账户数据身份无法对齐时显示待核对。

控制台侧历史投影带 grid_id、source_seq、as_of、stale 和方法版本，通过可重放 outbox 导入并有唯一键；不同数据库之间没有原子事务，不能把“旧库已显示”作为订单已经执行的证据。Runtime 不可用时保留旧投影并明确过期，禁用操作，不能删除列表中的实例。

移动端、空状态、部分成功、剩余仓位、首次保护未确认和收益 incomplete 都是正式页面状态。操作按钮按服务端 allowed_actions 渲染；服务端依然校验，不能靠 disabled 按钮维护交易规则。

### 14.1 草稿、导出和通知

草稿/参数模板保存 schema 与 revision，不保存运行状态、库存或订单。复制、批量复制和换账户都生成新草稿，每份独立校验能力、资金与冲突；批量复制不隐式执行批量下单。旧 schema 迁移后仍须重新预览。

订单/成交/配对/收益导出使用固定 source_seq 和方法版本；历史修订时导出新版本，不覆盖旧结果。保留 UTC 原始时间、用户时区、原币/转换来源与 incomplete 标记；下载只允许授权范围内的任务。

通知由独立 outbox 消费者按 event_key 和政策去重、限速、退避；支持用户已配置且启用的渠道引用。异常、恢复、触发和残仓为默认有意义事件，逐笔成交通知需单独开启。偏好可区分渠道/事件；通知发送失败仅影响通知状态，不回滚成交或阻塞平仓。网站待处理列表与通知共用事件事实，避免只发消息却没有可追查记录。

## 15. 部署、监控、容量与升级

生产新增独立 `entropy-grid.service`，不成为 Console 的同组子进程；Runtime 与 Console 分别重启。保留现有 `entropy-console.service` 的运行模型，不为了新服务修改旧 worker 的 KillMode 行为。开发环境以独立命令启动 runtime，macOS 不依赖 `/proc` 找到服务。

运行库、socket、锁、配置和秘密文件按持久数据目录管理，备份/监控有独立路径。systemd on-failure 重启只启动 RECOVERING，不自动跳过账本检查。首次发布 feature flag 关闭真实派发，只允许查看、离线 fixture 预览和测试环境。

健康检查分为 liveness、存储可用、市场数据、私有流覆盖、交易资格与保护覆盖。只有“有进程”不能显示策略健康；readiness 不通过时阻止新建和新开仓，但保持查询和合法风险处置路径。

指标至少包括：待处理 fill 数、最老未入账时长、commit 延迟、outbox backlog、fill→补单排队/提交/确认时间、UNKNOWN 数量与年龄、回补覆盖、库存差额、可减仓超额、账户预算、实际拒单/429、原生保护差额、剩余死手触发配额、事件投影落后量。金额指标按币种和账户显示，不编造跨币总金额。

单 Runtime 是首版故障域；通过原生保护、掉线撤单、持久恢复降低影响，不宣称已经有自动跨机器高可用。后续拆多 Runtime 时必须增加跨主机控制权和一致性设计，不能仅启动第二个服务副本。

初始容量认证目标建议为 3 个账户、10 个网格、每网格最多 60 个逻辑区间；这是测试目标，不是所有交易所的保证上限。实际限制来自账户订单、条件单、请求权重、成交量限制及用户预算。预览同时给出初始化请求预算和预计耗时，低请求速率的所不能因为能挂 60 单就宣称可以瞬间建完。

普通合成负载目标为每秒 10 个成交事件，短时峰值 50 个；测量本地持久入账至补单进入派发队列 P95，目标 100ms，交易所网络/结算单独记录。达不到就降低认证容量或改进路径，不通过关闭 fsync、漏记 fill 或扩大无限队列获得漂亮指标。

发布：暂停新建 → 核对可恢复任务 → 一致备份 → 运行兼容迁移 → Runtime 恢复核对 → 只读观察 → 打开对应已认证市场。旧 binary 不能打开未知更高 schema；回滚应用需支持现有 schema，恢复旧备份一律重新回补，不能丢掉升级期间真实成交。

## 16. 可直接分配的开发任务

每项交付都包含明确验收；估时在首家 adapter 审计后确定。DTO 固定后，界面、计算和存储模块可并行开发，真实交易接入仍遵守依赖顺序。

| 任务 | 交付 | 前置依赖 | 完成标准 |
|---|---|---|---|
| G01 账户与合同审计 | 身份规则、首两家 GridVenue 能力与失败矩阵 | 无 | 每项能力有官方来源、证据和缺口；确定 client id/回补/保护语义 |
| G02 模型与运行库 | DTO、schema、事务 StoreWriter、迁移/备份 | G01 身份 | Decimal/大整数精度、唯一键、迁移中断、备份恢复通过 |
| G03 planner/sizing | 价格线、布局、数量求解、风险 Plan | G01 规则；可与 G02 同期 | 三方向×两间距、部分初始库存、成本/最小量与场景算例通过 |
| G04 网格内部账户占用 | 网格本机控制锁、经济占用与预算；独立账户配置 | G01/G02 | 网格内部同账户不同 key、同 signer 跨作用域不能绕过；跨策略分账户由使用者保证 |
| G05 精确成交合同 | 新 ExecutionEvent 与归一化/历史源 | G01/G02 | 实时与 REST 稳定键一致；float 不进入交易账本 |
| G06 journal/executor | 外部 client id、意图/尝试、预算与共享限速 | G02/G04/G05 | 响应丢失、迟到、429、撤单竞争不会重复订单/释放错误预算 |
| G07 actor/reconciler | 基础网格、部分成交、slot 聚合、库存与配对 | G03/G05/G06 | 数量/费用守恒，无自成交补单，跨零与乱序进入正确处理 |
| G08 lifecycle/protection | 创建、暂停、只减仓、结束、原生保护与兜底 | G07 | 真实减仓额度、保护阶段、残仓和终态可核验 |
| G09 recovery | cursor 覆盖、未知订单、重启与外部变化 | G05–G08 | 进程任意关键点退出后恢复不重单；有缺口保持停开仓 |
| G10 网格 API/RPC | 预览/幂等/任务/流/鉴权与 RuntimeClient | G02/G03；执行依赖 G08/G09 | 重试、旧预览、变版本和服务断开响应符合合同 |
| G11 开单与详情 UI | 专业/引导创建、图表/只读行情、管理、残仓、模板/复制 | DTO 后可并行；实连依赖 G10 | 表单/图表/预览同源，迟到响应、窄屏和重连不误显示 |
| G12 账本与总览桥接 | 纯 FIFO、pairing、资金费、ROI、总览投影、导出与通知 outbox | G05/G07/G10 | 数字算例、资金流、修订、导出一致性、投影/通知幂等通过 |
| G13 首家 adapter 完整认证 | 首家部署与 market 所有新合同接口 | G01/G05/G06，闭环依赖 G08/G09 | 测试环境故障演练；完整报告，无未知资格项假通过 |
| G14 部署和稳定性 | 独立 service、备份/升级、容量/指标、feature flag | G08–G13 | Console 重启不影响 Runtime；Runtime 重启完成恢复 |
| G15 第二家认证 | 不同技术栈的第二 adapter | G13 合同冻结 | 不新增 engine venue 分支，同一套 conformance 与恢复通过 |
| G16 热修改与资金任务 | 版本 saga、保留库存、缓冲/规模/减资、受限自动补缓冲/再投资 | G08/G09/G12 | 每步中断恢复，未知转移不重复、额度不绕过、利润不重置 |
| G17 移动与高级保护 | 有界平移、收益条件、定时/RSI/webhook 启停 | G16/完整账本 | 边界、冷却、预算/保护失效、签名 webhook 重放防护通过 |
| G18 推荐与回放 | 真实 OHLCV/费用/资金费数据、候选参数和回放 | G03/G07/G12 | 样本缺失拒推；K 线路径/排队不确定显式呈现 |

里程碑：

- **M0 可开发基线**：G01–G03，接口/模型/公式与 fixture 可评审，无实盘派发。
- **M1 单所闭环**：G04–G14，先离线和测试环境，再在明确交易授权下最小实盘验证。P0 只作内测。
- **M2 通用性证明**：G15，至少两种交易所技术栈通过同一合同。
- **M3 主要能力对齐**：G16–G18，加完整移动端与逐项产品验收；此时才评定最终目标完成度。

关键路径为身份/合同 → 持久执行 → 保护/恢复 → 真实账本与首所认证 → 第二所 → 高级功能。UI 可以先对固定 DTO 开发，不能先让 UI 直接调用旧 adapter 交易来绕开路径。

## 17. 测试方案与放行门槛

### 17.1 最小有意义的测试套件

- `test_grid_planner.py`：网格线唯一、范围、费用后间距、整数数量、margin tiers 和最坏库存；包含边界而非只镜像公式。
- `test_grid_store.py`：成交/意图同事务、重放唯一键、因果键冲突、故障回滚、迁移中断和恢复备份。
- `grid_contract.py` + 每所 shim：外部 ID 保留、精确量/价、拒绝/未决语义、撤单竞争、私有流/历史同键、系统成交与资金费覆盖。
- `test_grid_recovery.py`：发送前后崩溃、回执丢失、fill 先于 ack、断线跨分页、迟到成交、UNKNOWN 后重启、旧保护/残仓。
- `test_grid_account_scope.py`：网格内部真实账户/不同 key、HL dex、Lighter 两部署和签名作用域竞争；套利操作不依赖网格服务。
- `test_grid_ledger.py`：第 12.1 算例、跨零、部分费用分配、资金流、期末估值、修订与 incomplete。
- `test_grid_api.py`：过期预览、同请求不同内容、双击创建、操作互斥、scope 鉴权、运行服务不可用。
- `test_grid_operations.py`：资金 saga 的每步中断、自动政策累计额度与重放、暂停取消自动投入、导出边界和通知去重。
- 浏览器闭环：创建→预览→任务→详情→暂停→恢复→调参→结束/残仓；检查 360/768/1280 与断流/乱序响应。

旧回归至少包含 venue registry、golden legconf、maker contract、supervisor/operations 和现有账本套件。只在修改对应接口时扩展相关检查，不为文档添加无意义测试。

### 17.2 任何一个未通过都不能开放无人值守完整网格

1. 任意 fill 重放、重启和回补后，真实库存、订单成交、配对量、手续费均守恒。
2. 任何未知提交都不会触发盲目重发或错误释放预算。
3. 任意旧 epoch 的未发送请求不会在暂停/止损之后继续开仓；在途请求已纳入撤单和晚到成交流程。
4. 网格内部账户/市场和 signer 的冲突不能通过换配置或 key 名绕过；套利与网格分别使用账户，跨策略分离由使用者保证。
5. 恢复缺口、磁盘故障、流断连和规则变更按公布边界阻断新风险。
6. 价格保护、死手开关/订单失效和服务死亡的作用范围经认证；不能把撤单当作平仓。
7. 成交、费用、资金费和边界估值与交易所实际记录可对账；未知不展示成零。
8. 完整闭环和故障演练通过，且真实成交测试有明确的用户交易授权。

## 18. 首批输出与后续实施顺序

本技术规划已经冻结运行拓扑、数据所有权、精确合同、事务纪律、账户隔离与验收路径。交易所仍需通过 G01 填齐实际 client id/订单终局查询、保护和风险模型证据；这属于计划中的首个开发任务，不是留给 UI 自行决定的空白。

开始编码时先做 G01–G03：生成能力审计报告、定义 DTO/schema、实现独立运行库和纯 planner，接固定 fixture 展示可执行预览；此阶段不开放真实下单。随后建设 G04–G09，再让 API/UI 接入经过恢复验证的执行链路。

上述顺序在同一次连续开发中推进；基础阶段完成不是停止点。完成单所代码和模拟闭环后继续第二所及高级功能。真实合同/账户证据未知时保留该项缺口，不以“待实盘验证”为由停止不依赖它的页面、通用逻辑和故障验证。

技术资料核对来源：[Python sqlite3 事务控制](https://docs.python.org/3.11/library/sqlite3.html#transaction-control)、[SQLite WAL 与持久性](https://www.sqlite.org/wal.html)、[SQLite 在线备份](https://www.sqlite.org/backup.html)，以及第 5 节逐家官方 API。软件升级或 SDK 变更时重新核对相关合同，不用本文件固定记忆覆盖新规则。

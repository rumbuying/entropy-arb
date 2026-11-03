# 网格开发交接补充：给执行 AI 的确定规则

日期：2026-10-11。配套文件：[产品方案](GRID-TRADING-PLAN.zh-CN.md)、[技术规格](GRID-TRADING-DEVELOPMENT-SPEC.zh-CN.md)、[启动提示词](GRID-TRADING-AI-PROMPT.zh-CN.md)。本文件补充编码和验收规则，未实现交易代码。

最新用户决定优先：网格与套利为独立网站模块，账户/凭证配置和运行记录分别管理，使用者保证不同资金账户。不开发跨策略硬互斥、不升级旧 worker 占用协议；只保留网格内部账户、预算、签名与恢复控制。见 [模块分离决策](planning/grid/MODULE-SEPARATION.zh-CN.md)。

执行范围已按用户最新要求扩大为连续推进 G01–G18。M0-A 是起步子任务，完成后继续执行、网站、第二所和高级功能，不再逐批等待新指令。下文的基础规则保持有效，真实派发与生产操作仍独立于开发授权。

## 1. 为什么还需要这份补充

技术规格已经有架构和任务依赖，但不能把下列内容交给执行 AI 自行猜测。

| 缺口 | 本次处理 | 后续必须交付的证据 |
|---|---|---|
| 产品和技术文档使用不同预览路径 | 统一为复数 `/previews`，以技术 HTTP 合同为准 | API 路由/请求测试 |
| 价格取整、参考价和初始库存分配未精确到步骤 | 固定 `grid-calc-v1` 默认算法 | 固定输入/预期输出，不只测试自写公式 |
| DTO 只有字段概要 | 下文固定关键类型、拒绝规则和 Plan 资格字段 | 严格模型、序列化合同和样例 |
| SQL 只有表纲要，未验证失败回滚 | 划清第一批存储范围与唯一约束 | 迁移/事务/备份的故障测试 |
| 风险模型尚无逐所认证公式 | 明确 fixture 求解与真实模型分开，未知阻断资格 | 官方来源、目标部署和合同认证报告 |
| 测试清单缺可直接判断的数字 | 新增 [固定验收向量](planning/grid/acceptance-vectors.json) | 向量结果与独立故障测试 |
| 原型在项目外，不便接手 | 新增 [仓库内界面参考](planning/grid/grid-workbench.reference.html) | 后续 UI 与参考核对，演示数据不能上线 |
| 大任务容易被骨架、模拟和绿测试冒充完成 | 按依赖分阶段连续实施，逐项记录代码/测试/实盘证据 | 实现清单、实际命令、失败与未测项 |

## 2. 本次授权范围：连续开发 G01–G18

本次目标是尽可能完成 G01–G18 的全部可实现代码、集成与验证。M0、M1、M2、M3 表示依赖和验收顺序；满足条件后自动继续，不把阶段结束当作停止点。以下子任务是起步顺序，完成后扩展完整运行模型和后续模块。

| 起步子任务 | 基础交付 | 基础完成不等于 |
|---|---|---|
| G01-A | 仓库审计；HL/Backpack 官方合同初审；身份规范和缺口表 | 真实账户解析或实盘认证完成 |
| G02-A | 严格核心 DTO；独立运行库的迁移、事务容器、事件去重、意图唯一键、幂等记录和一致备份 | 所有运行表、真实成交事务、actor 已完成 |
| G03-A | 确定性价格线、初始布局、部分初始库存分配；风险模型协议与 fixture 求解；离线预览入口 | 某交易所风险模型已认证或可以创建真实订单 |

允许连续开发、修改代码、隔离测试、读取公共行情和编写部署文件。当前没有新增真实交易授权：不读取真实 `.env`/秘密、不加载真实凭证、不向真实交易账户挂撤单/改杠杆/划转，不启动/重启生产服务或访问生产运行库。需要交易的验证使用隔离模拟 adapter；如已有明确测试账户授权，仅按该授权执行。缺真实权限或认证时记录对应阻断，继续独立开发，不在每阶段重复询问实现许可。

代码范围包括 `entropy_arb/grid/`、运行入口、console/API、V2 页面、adapter 兼容公共层、测试、依赖声明和新增部署文件。不开发旧启动/平仓跨策略占用桥接。旧合同/行为保持兼容，公共改动必须有回归证据，不做无关重构或全局升级。旧 console 库仍不存交易权威数据；生产运行库、真实配置、秘密和黄金结果不是可覆盖对象。固定验收向量不得为了测试变绿修改 expected，可以另加独立向量。

新增模块要形成真实工作路径，不预建所有空模块、空 API 或永远返回成功的适配器。实现同一套 Runtime→API→UI 的隔离模拟闭环，并实现真实 adapter 合同；模拟出口和真实出口在工厂、账户身份和存储上隔离。模拟通过不授予真实资格。

Runtime 的 `execution_mode=simulation|production` 由受限服务端启动配置决定，不接受网页请求切换。在 simulation 中，VALID 的 fixture Plan 可创建模拟实例，仍保持 live_eligible=false；只可绑定 fixture 账户、模拟 adapter 和专用运行库，沿用真实状态机/事务/API。production 只允许 observed 来源及全部有效资格；真实派发默认关闭，并在最后发送出口复验。不可通过浏览器字段把 fixture Plan 或模拟身份送入真实 adapter。通知测试使用假渠道，不实际发消息。

## 3. 读取顺序与已有代码事实

1. 先检查仓库及适用父目录的 AGENTS.md、工作区变更，记录基线；不能覆盖原有修改。
2. 读本文件、技术规格、产品方案；产品目标遵循产品方案，具体接口遵循技术规格，本文件的编号规则细化两者。发现超出已注明差异的新冲突，记录后暂停依赖该冲突的实现，继续独立任务。
3. 阅读 `entropy_arb/config.py`、`venue_registry.py`、`maker.py`、`venues_common.py`、`venue_hl.py`、`venue_backpack.py`。
4. 阅读 `entropy_arb/console/storage.py`、`identity.py`、`ledger.py`、`operations.py`、`supervisor.py`、`server.py`；这些名称均相对 `entropy_arb/console/`。
5. 查看 `tests/test_config.py`、`test_venue_registry.py`、`test_golden_legconf.py`、`test_strategy_identity.py`、`test_ledger.py`、`test_operations.py` 和 `tests/maker_contract.py`。

旧身份模块实际位于 `entropy_arb/console/identity.py`，不是包根目录。已有操作测试是 `tests/test_operations.py`，不是 `test_console_operations.py`。网格模块和上述网格测试尚待开发；不能把文件不存在认定为已实现。

旧 eventlog 允许丢记录；旧 FillEvent 经 float；旧 maker 下单可能自行生成 client id；旧身份为双腿；旧 console 是数据库唯一写入者。只借用兼容组件，不把这些行为带入新执行合同。

SDK/依赖版本从实际环境和依赖文件记录，不能把范围声明当作安装版本。当前 live 依赖包含未固定 commit 的 Lighter SDK；初审记录问题，需要新适配时在隔离环境验证固定版本并说明兼容性，不全局升级现有环境。pytest 是既有测试方式；缺 pytest 或 SDK 时记录环境缺口，不能标测试通过。

## 4. 类型与序列化合同

核心模型采用 frozen dataclass + 显式 `from_dict/to_dict`；优先标准库，沿用项目依赖，不新增一个仅用于 DTO 的框架。未知字段报错，禁用宽松类型强制转换。

| 类型 | 固定规则 |
|---|---|
| DecimalText | 外部必须为字符串；格式 `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`；禁指数、空白、NaN/Inf、JSON 数字、bool。正值/零/负值许可按字段校验 |
| 规范 Decimal 文本 | 非指数普通小数，去多余小数零，负零为 `0`；如 `100.00→100`。原始源值另存，不在规范化时经过 float |
| ID | 字符串；原生订单/成交/账户索引不可转换成 JS Number；UUID 与原生 ID 是不同字段 |
| 整数 | grid_count、revision、schema_version 真正 int，拒绝 bool 和 `20.0`；API 中 seq/epoch 等可增长计数为字符串 |
| 时间 | DTO 用带时区 ISO 8601；源时间与接收时间分开。测试 clock 注入，不用当前时间制造不可复现 hash |
| 可用性 | 未知为 null + reason；不能替换成 0、false 或已认证 |

至少定义 `GridConfig`、`AccountIdentity`、`MarketIdentity`、`InstrumentRules`、`CapabilityEvidence`、`MarketSnapshot`、`RiskSnapshot`、`ExecutionEvent`、`OrderIntent`、`Plan`。配置支持技术规格第 4 节的基础字段；未实现的 P1 功能返回 FEATURE_UNAVAILABLE。

账户认证键使用 venue + deployment + 认证 owner/subaccount，不能用 profile 或 API key 昵称。账户内可有不同市场；signer scope 独立记录。HL 主 dex 的 `""` 与其他 dex 名称严格区分。M0 的 fake identity 必须标 fixture，不能变成真实账户 UUID 注册。

统一数量为 qty_base，InstrumentRules 中 multiplier 仅用于 native contracts 与 qty_base 换算，不再在 qty_base 盈亏中乘第二次。金额始终附 asset；多币不直接相加。

### 4.1 Plan 最小输出

`schema_version`、`calc_version`、`plan_kind`、`validation_status`、`live_eligible`、`config_hash`、`plan_hash`、`rules_revision`、`capability_revision`、`risk_snapshot_id`、`reference_source/price`、规范边界、价格线、每格量/未知原因、逻辑循环、初始开仓需求、候选挂单、阻断项、风险与费用证据。

- `plan_kind = fixture | observed`；fixture 的任何能力标记都不产生真实资格。
- `validation_status = VALID | INVALID | BLOCKED`。VALID 只表示当前输入/模型通过检查，不代表可以实盘。
- **M0 的所有输出都必须 `live_eligible=false`**；普通业务错误有稳定 code/reason，不能输出假正常 Plan。
- 风险模型未知时可给价格线和带阻断原因的逻辑布局，但 `qty_per_grid=null`，不能把例子里的数量塞入真实模式。
- 每条线有 level_index + price；每个循环有 cycle_index + entry/exit_level；逻辑档位和实际 OrderIntent 分开。

### 4.2 hash 规则

`config_hash` 为规范化配置的 UTF-8 JSON SHA-256；固定 sort_keys、紧凑 separators、ensure_ascii=False、禁止 NaN，Decimal 用规范字符串。原始展示输入和秘密不进入 hash。

`plan_hash` 绑定规范配置、calc/rules/capability/policy revision、account/market、risk_snapshot_id、报价快照身份及实际计划内容。schema 列出精确字段；不加入随机 request_id、接收时间或临时 UI 状态。同一个受控输入重启后 hash 一致；报价/风险修订改变时 hash 改变。原始输入另存供审计。

## 5. `grid-calc-v1` 的确定算法

使用局部 Decimal context：prec=50，普通算术 ROUND_HALF_EVEN；价格映射为 tick 整数倍时使用以下明确规则。禁止更改全局 context 影响旧账本。输入超过此算法支持的精度/数量范围时明确拒绝，不静默截断。

### 5.1 价格线

1. 校验 L/U/tick > 0，L < U，N 真正整数。M0 fixture 允许 N=2…60；真实资格还要受认证容量限制。
2. `L*=ceil(L/tick)×tick`，`U*=floor(U/tick)×tick`；若 L*≥U* 则 INVALID。
3. 等差 `raw_i=L*+i×(U*−L*)/N`；等比 `raw_i=L*×Context.power(U*/L*, Decimal(i)/N)`。端点直接指定 L*/U*。
4. 中间线 `P_i=ROUND_HALF_UP(raw_i/tick)×tick`；正值正中点取高 tick。不能只对价格 `quantize(tick)`，tick 可能是 0.5 或 0.05。
5. 重复、非递增、出界报 VALIDATION_ERROR，reason 分别为 PRICE_LEVEL_COLLISION / PRICE_LEVEL_ORDER / PRICE_LEVEL_OUTSIDE；不删线、不改 N。

幂运算中间值遵守算法版本，不承诺任意精度下数学真值；固定 runtime/Decimal 实现，升级时重跑取整边界向量。正式价格线入库后不重新生成。[Decimal 官方规则](https://docs.python.org/3.11/library/decimal.html)

### 5.2 初始布局

首次参考价默认为有效 mark；source 与数值写入 Plan，缺 mark 不偷偷换 last。bid/ask 独立用于 post-only 检查。启动价不在闭区间 [L*, U*] 时 WAITING_RANGE，首次开仓和普通挂单为空。

中性：离参考价最近的线为空，相等距离取较高线；低于中心索引的线买，高于中心索引的线卖。买价必须严格小于 ask，卖价严格大于 bid；不满足则 WAIT_POST_ONLY，不改价、不吃单。

多头：循环 i 的 entry=P_i、exit=P_(i+1)，i=0…N−1。初始 entry<reference 的循环候选挂买；entry≥reference 的循环 deferred 时待激活，immediate 时纳入初始库存需求。

空头：循环 i 的 entry=P_(i+1)、exit=P_i。初始 entry>reference 的循环候选挂卖；entry≤reference 的循环 deferred 时待激活，immediate 时纳入初始库存需求。候选单再检查真实 bid/ask，不满足时保留逻辑需求但不发送。

中性不做初始方向建仓；多空没有真实库存时不产生可执行 reduce-only 退出单。Plan 可表达“预期库存后的退出需求”，必须明确条件，而不是已挂单。

### 5.3 初始建仓部分成交

immediate 的总目标是待预建循环数×q。真实成交按退出价格离启动参考价近到远分配，等距离按 cycle_index；多头优先低出口，空头优先高出口。

每循环分配不超过 q；剩余不足 q 的真实量仍形成部分 lot，达到退出单最小量/名义后才能成为退出需求。费用按分配量比例分摊，最后分配项承担有限精度余数，合计必须等于实际费用。

未成交初始目标保持待激活/未建立，不直接再发第二张 IOC。后续是否补建由新预览/持久操作决定；永远不能先按目标量挂完整退出单。

### 5.4 资金数量求解

RiskModel 协议至少返回 `ordinary_required(k)`、`adverse_required(k)`、`account_required(k)`、证据/修订与可证明的候选边界。输入真实规则、布局、账户快照和其它预算，不在 solver 内塞某个 venue 公式。

基础阶段用**测试用离散需求表**验证求解：`q=k×qty_step_base`，最低 k 满足所有相关入口/出口的 min_qty/min_notional，最大 k 有明确有限边界。初版枚举有界 k，选择同时满足 ordinary≤total−reserve、adverse≤total、account_required≤account_remaining 的最大 k；未找到则拒绝。随后按官方证据实现目标 venue 的真实风险模型和测试，证据不足不杜撰公式。

离散表是算法测试输入，不是保证金模型。真实风险模型的费用、冻结、维护档位、账户潜在仓位和保护证据未认证时 BLOCKED/CAPABILITY_UNVERIFIED。不能用投入×杠杆÷N、固定安全系数或“官方文档没写所以忽略”补全。

## 6. G02-A 存储的最小约束与测试

G02-A 起步先建 accounts/instruments 的公开元数据、previews/request_dedup、events、order_intents、executions 与 schema/migration/checkpoint 基础表；随后在同一次连续开发中扩展技术规格第 6 节全部所需模型、库存/配对事务和持久任务。每张表有版本/来源和明确主键，不向旧 console 库加运行表。

- `event_key`、`execution_key`、`cause_key` 的唯一约束在 SQL 中存在，不只在 Python 查询。
- request 去重范围为 `(owner_scope, request_id)`，持久绑定 request_hash 与结果；同 key 同内容返回原结果，不同内容 IDEMPOTENCY_CONFLICT。
- 成交唯一键的范围按 adapter 合同提供；M0 fake 合同也包含 deployment/account/market/source，不假设全局 trade id。
- 同成交的数量、价格、side、市场/账户不一致为 DATA_CONFLICT；同经济事实不同 received time 仍为重复。费用后补留修订记录，不能追加成交量。
- events 原始记录不可改；处理 checkpoint 单独更新。外部 SQL/adapter 不自行写库。
- StoreWriter 用专用线程持有写连接；async 调用等待事务提交后才可见结果。关闭时等待已接收写入完成，随后关连接和线程，不能用后台 daemon 丢失队列。
- WAL/FULL/外键；迁移按显式事务逐条执行，schema 与记录一起提交。备份 manifest 从完成的备份读取版本/seq。

有意义的验收包括：同事务写 execution 和 mock intent 时在两次 INSERT 间主动抛错，重开库二者均不存在；并发同键提交只落一份；同因果重复不生成两个意图；迁移中途故障版本/DDL 回滚；重开幂等映射存在；备份通过 integrity_check 且恢复边界一致。真实库存事务尚未实现，报告必须写清。

## 7. 官方合同审计怎么做

为每个 venue/deployment/capability 记录：接口/SDK 符号、源码位置、官方 URL、核对日期、安装版本、id 范围、查询终局性、历史分页覆盖、签名 scope、撤单/保护 scope、证据与缺口。

状态为 DOCUMENTED / IMPLEMENTED_UNVERIFIED / TEST_VERIFIED / LIVE_VERIFIED / UNKNOWN / UNSUPPORTED，不能把文档记载或 mock 通过写成 LIVE_VERIFIED。继续完成实现和本地/模拟合同验证；没有真实账户证据时保留身份/实盘资格缺口，不阻断可独立实现的适配和其它模块。

SDK/官方文档冲突时记录证据并阻断该能力，不猜参数。不能联网时记录未核对项，继续纯 DTO/store/planner；不得杜撰版本、参数或来源。

## 8. 固定向量与测试纪律

[acceptance-vectors.json](planning/grid/acceptance-vectors.json) 内 M0 向量覆盖取整、几何间距、碰撞、严格精度、中心空线、多空布局、初始部分成交和资金筛选。M1 向量在账本阶段实施；本次连续开发应推进完整账本和相应验收，不能停在只计算价格线。

expected 是独立验收视图，可以由测试提取模型字段比较；生产/CLI 计算只能接收 input。禁止把 expected 返回给 CLI，也禁止把向量读到业务模块后按 case id 硬编码结果。除这些向量外必须补提交/重开/回滚、旧接口兼容和失败边界测试。

以下为基础阶段命令，文件创建后执行；后续增加技术规格第 17 节的合同、恢复、API、账本、资金任务与浏览器验证，不仅运行下面几项：

```sh
python3 -m pytest tests/test_grid_config.py tests/test_grid_identity.py tests/test_grid_planner.py tests/test_grid_store.py -q
python3 -m pytest tests/test_config.py tests/test_venue_registry.py tests/test_golden_legconf.py tests/test_strategy_identity.py -q
python3 -m entropy_arb.grid.preview --fixture planning/grid/acceptance-vectors.json --case P01
git diff --check
```

CLI 显式 fixture 模式，无真实凭证入口。只对支持的 kind 做计算，不支持返回非零与清楚原因；输出 schema/version/plan_kind/live_eligible 与实际计算视图。上述样例 P01 只计算价格线，不暗示完整 Plan 已具备真实资金资格。

日志记录实际命令、退出码、摘要和未测项。不能以没有运行、跳过或不存在的测试称“通过”；旧基线已失败的项记录前后区别。禁止设置 UPDATE_GOLDEN 或放松断言掩盖回归。生产依赖不能因一个测试需要而全局升级。

## 9. 状态交接与验收

执行 AI 创建并维护：

- `planning/grid/REPO-AUDIT.md`：代码/工作区基线、已读路径和集成差距。
- `planning/grid/VENUE-CAPABILITY-AUDIT.md`：第 7 节的证据矩阵。
- `planning/grid/CONTRACTS.md`：实际字段/枚举、hash schema、SQL/迁移、HTTP/RPC/推送合同、状态转移和示例。
- `planning/grid/IMPLEMENTATION-STATUS.md`：全部 G01–G18、子任务及代码/测试/真实认证完成度；未完成默认 pending。
- `planning/grid/IMPLEMENTATION-REPORT.md`：修改文件、验证命令和结果、完整模拟闭环、部署/回滚说明。
- `planning/grid/LIVE-READINESS.md`：逐所/市场真实资格、已知缺口、证据和未授权/未验证事项。
- `planning/grid/RESUME.md`：已完成内容、精确未完成项和下一条可执行工作，供上下文恢复。

状态仅为 pending / in_progress / implemented / verified / blocked。implemented 表示真实代码存在；verified 还需要本任务的验收证据，并注明 offline/simulation/sandbox/live 的范围。G01-A 完成不把 G01 整项改为 verified；G02-A 同理。blocked 精确到受阻能力，其它可独立工作继续。代码/模拟验收与实盘认证分别记录，缺实盘证据不冒充整项已放行。

每个实现单元都检查：调用输入→真实行为→持久/序列化结果→失败路径→相关旧回归。M0 后自动推进依赖已满足的 M1、M2、M3；阶段结果写记录，不停下请求新任务。实盘授权/认证缺口可阻断真实发送和最终放行，但不能阻断与之无关的代码开发。暂停某项依赖时继续独立任务，未知资格不能用 UI 开关绕过。

持续开发至全部可实现任务与验证完成，或已无任何不依赖外部变化的可推进项。平台硬限制中断时先保存 RESUME，不能把中断记为项目完成。缺实盘证据时可交付充分实现/模拟验证的代码，但必须保留实盘未放行状态。

## 10. 连续开发阶段与需补的具体合同

| 批次 | 范围 | 开始前必须细化 |
|---|---|---|
| 执行基础 | G04–G06 | 真实账户/锁协议、完整意图状态转移、SDK 未决证据、nonce 与取消作用域；独立 shim 故障向量 |
| 策略与恢复 | G07–G09 | 每种事件的事务更新、slot 与减仓预留、快照/流截止边界、保护更新竞争、每个崩溃注入点 |
| 网站与账本 | G10–G12 | 完整请求响应 JSON、鉴权错误、推送 patch schema、列表分页、导出下载、真实 ledger/outbox；关键页面 fixture |
| 首所认证部署 | G13–G14 | 固定 SDK/部署配置、所有未决和恢复场景实证、可恢复升级/回滚；真实交易测试需单独授权 |
| 第二所 | G15 | 用同一合同认证，不在 engine 增加 venue 分支 |
| 完整产品 | G16–G18 | saga 每步补偿/续行、自动资金额度、移动/信号条件、回放路径假设、完整 UI 对齐矩阵 |

本次没有假装填完所有后续 SQL/JSON/交易所风险公式。它们需要在相应批次用实际接口与测试证据细化；启动提示词明确限制执行 AI 的自由猜测范围。

上述合同细化也是执行 AI 的连续开发工作：从已确定规则、源码和官方资料生成具体合同，先写独立验收再集成；兼容、可逆的实现选择记录后继续。真正无法证明的交易语义记入 LIVE-READINESS，不凭猜测实现和放行。

G13 的合同/代码/模拟部分通过后可以冻结合同并继续 G15；G16–G18 按本地功能依赖推进。G13 未完成的真实账户和实盘认证仍保留阻断，不能因此宣称 M1/M2 的实盘放行完成。

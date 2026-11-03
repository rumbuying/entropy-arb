# VENUE-CAPABILITY-AUDIT — 网格能力证据矩阵（G01）

日期：2026-10-11。规则（handoff §7）：每项能力记录接口/SDK 符号、源码位置、官方 URL、核对日期、安装版本、id 范围、查询终局性、历史分页覆盖、签名 scope、撤单/保护 scope、证据与缺口。状态只能是 DOCUMENTED / IMPLEMENTED_UNVERIFIED / TEST_VERIFIED / LIVE_VERIFIED / UNKNOWN / UNSUPPORTED。文档记载或 mock 通过 ≠ LIVE_VERIFIED。

证据来源分两类：
- **代码事实**：旧 adapter 仅有源码证据时为 IMPLEMENTED_UNVERIFIED；新增 GridVenue 有原始格式响应合同测试时可为 TEST_VERIFIED（离线）。均不构成 LIVE_VERIFIED。
- **官方文档**：2026-10-11 已联网复核下列 HL / Backpack 历史接口；DOCUMENTED 只证明官方接口存在，不证明本地实现或实盘认证。其他未核对项仍标 UNKNOWN。

## 能力清单（spec §9.2 基础键 + 网格必需）

通用能力键：`limit_orders(post_only)`、`order_query(by_id)`、`open_orders_snapshot`、`private_fills_stream`、`fills_backfill`、`stable_execution_key`、`funding_payments`、`native_triggers`、`reduce_only`、`isolated_margin`、`deadman_scope`、`client_id_semantics`、`risk_model`。

## Hyperliquid（HL 主永续，mainnet deployment=""；testnet 单独身份）

新网格实现：[hyperliquid.py](/Users/alex/Work/entropy/entropy_arb/grid/adapters/hyperliquid.py)。官方 SDK 固定 0.24.0；52 项原始格式响应/故障测试通过，公共元数据、行情、观察地址费用和账户快照已读取。只读 CLI 使用显式经济地址，不读取 HL_PRIVATE_KEY。下列 TEST_VERIFIED 均仅指离线合同，不指私有账户实盘认证。

| 能力 | 状态 | 证据 | 缺口 |
|---|---|---|---|
| limit_orders(post_only) | TEST_VERIFIED（离线） | _wire/submit 支持 ALO/GTC/IOC、原始 ACK 分类；[Exchange](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint) | 动态价格网格量化、账户权限和实际订单待认证 |
| order_query(by_id) | TEST_VERIFIED（离线） | lookup 以 cloid 查询，精确 OID、累计量、显式终局表；unknownOid 为 NOT_FOUND_TRANSIENT | 不保证查询不存在代表永久不存在；实际终局/时效待认证 |
| open_orders_snapshot | TEST_VERIFIED（离线） | open_orders 使用 frontendOpenOrders、校验部分成交及所属市场；[Info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) | 所有权和查询时点待认证 |
| private_fills_stream | TEST_VERIFIED（离线） | run_stream/ingest_message 订阅 userFills 和 orderUpdates，处理成交先于 ACK 的绑定 | 实际私有流、断线补账屏障未认证；重连仍 gap=true |
| fills_backfill | TEST_VERIFIED（离线） | fills/_history 时间窗二分、包含边界、同毫秒/10000 条上限阻断 | 默认 retention floor 未证明，不把短页当覆盖证明 |
| stable_execution_key | TEST_VERIFIED（离线） | network+经济地址+coin+oid+tid，REST/WS 相同；Actor/SQL 重放去重 | 实际唯一性及同毫秒成交顺序待认证 |
| funding_payments | TEST_VERIFIED（离线） | userFunding 时间窗、稳定键、支付符号转换、独立覆盖 floor；[Perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) | 实际覆盖与账单对账待认证 |
| native_triggers | UNKNOWN | 无实现 | trigger order 参数未核对 |
| reduce_only | TEST_VERIFIED（离线） | _wire 保留 reduceOnly，reduce_only_ioc 必须为 true；cancelByCloid 仅指定订单 | 实际平仓/细小残仓及最小名义额例外待认证 |
| client_id_semantics | TEST_VERIFIED（离线） | AccountExecutor 分配 128 位十六进制 cloid，账户+运行库命名空间+持久计数；跨恢复复用同意图绑定 | 关联标识不等于重发幂等承诺；UNKNOWN 仍先查询 |
| risk_model | UNKNOWN | 无维护保证金档位实现 | 不得杜撰公式 |
| deadman_scope | UNKNOWN | 无实现 | scheduledCancel 未核对 |

签名 scope：经济账户查询和 EVM signer 分开；HyperliquidSigner 使用 network+signer 地址租约和持久 high-water，SDK 签名恢复、同进程并发、不同库与时钟回退已测试。按最新模块分离决定，旧 venue_hl.py 保留独立签名流程，不接入该租约；使用者负责账户及签名凭证分离，不提供跨策略互斥保证。vault/network 不匹配会拒绝，agent 的实际账户授权仍待认证。

## Backpack（deployment="main"，BACKPACK_API_KEY/SECRET）

| 能力 | 状态 | 证据 | 缺口 |
|---|---|---|---|
| limit_orders(post_only) | IMPLEMENTED_UNVERIFIED | place_maker postOnly (:469)，maker_contract.py 鞭及 check_surface 通过（offline shim） | 网格新合同需外部 client_id 注入版 placeMaker |
| order_query(by_id) | UNKNOWN | 无实现 | orderStatus 端点未核对 |
| open_orders_snapshot | IMPLEMENTED_UNVERIFIED | 私有流内存视图 (:365-370) | 流断开时为空 —— 不是可用快照；REST open orders 未实现 |
| private_fills_stream | IMPLEMENTED_UNVERIFIED | account.orderUpdate (:652-744)，tradeId t 主键 + 累计量 z 兜底 | 幂等范围按 (order, tradeId) 内存去重，需库级键 |
| fills_backfill | DOCUMENTED | [官方 API](https://docs.backpack.exchange/) 包含 GET /wapi/v1/history/fills | 本地网格回补未接入；完整分页、时间边界、成交键待认证 |
| stable_execution_key | UNKNOWN | tradeId `t` 主去重 (:704-708) | 全局唯一性范围未认证 |
| funding_payments | DOCUMENTED | [官方 API](https://docs.backpack.exchange/) 包含 GET /wapi/v1/history/funding | 本地未接入；官方 payment quantity 正值为收到、负值为支付，须转换到引擎正值为支付的约定 |
| client_id_semantics | IMPLEMENTED_UNVERIFIED | clientId 参数发送 (:411, 487) 但 adapter 内生成 (:390-393)；fill 关联走 order id | clientId 复用/范围/查询语义未认证 |
| native_triggers | UNKNOWN | 无实现 | |
| reduce_only | UNKNOWN | 无实现 | |
| risk_model | UNKNOWN | 无实现 | |

签名 scope：Ed25519 headers X-API-Key/X-Signature/X-Timestamp/X-Window（:112-147），无 nonce。撤单：`cancel_orders(None)` = 市场级 orderCancelAll (:560) —— 网格只允许专用账户使用或显式兼容作用域（spec §11.2）。

## 其他 venue（网格资格默认关闭）

- Lighter / lighter-rh：taker 接入；nonce 21104 刷新重试一次（venue_lighter.py:60-82）；SDK 未固定 commit 且本地不可编译 → 网格适配 blocked（本地无法验证）。
- Katana/Bulk/Ondo/Arcus/Perpl：maker_capable=True 只证明旧 maker 合同（POST post-only + fill 流），网格所需的 open-orders 快照/回补/funding/风控证据同样缺失 → `grid_contract_version` 能力键未声明前不开网格资格。
- 结论（spec §10 一致）：首发 HL 主永续已实现并通过离线合同/公共数据观察验证，第二候选 Backpack 尚未实现新合同；两家 fixture 保留模拟验收，真实资格全部 `live_eligible=false`。

## M0 结论

- 本项目已有 HL 真实 GridVenue 实现与离线测试证据，尚不能证明实际账户恢复三件套（快照+回补+终局查询）完全满足合同；全部真实派发关闭。
- 模拟 adapter 是唯一可跑完整闭环的执行出口（fixture 账户、专用运行库、独立发送出口）。
- 每项能力后续认证路径：官方文档核对 → 隔离实现 → offline shim 合同测试 → 测试环境实盘（需单独授权）→ LIVE_VERIFIED。

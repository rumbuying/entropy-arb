# CONTRACTS — 实际合同记录（随实现更新）

本文记录**实际实现**的合同（非计划稿）。每节标注来源文件。

## 1. 值与序列化（entropy_arb/grid/models.py）

- DecimalText 正则 `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`；拒绝 bool/int/float/NaN/指数/空白。规范形去尾零、负零→`0`，全程不经过 float。
- 整数：`parse_strict_int` 拒 bool 与 float（含 `20.0`）。
- 时间：`parse_iso_ts` 要求带时区 ISO-8601；`utc_now_iso(clock)` 支持注入时钟。
- DTO：frozen dataclass + 显式 `from_dict/to_dict`；未知字段 → `GridModelError(UNKNOWN_FIELD)`。
- 错误：`GridModelError(reason, message, field)`；calc 层 `CalcError` 同形。API 拒绝封套 = `{validation_status: INVALID, code: VALIDATION_ERROR, reason: <具体码>|null, message}`。

## 2. hash（models.py）

- `config_hash` = SHA-256(canonical JSON of `GridConfig.to_dict()`)，canonical = sort_keys + separators(",", ":") + ensure_ascii=False + allow_nan=False。
- `plan_hash` 绑定 `PLAN_HASH_FIELDS`（精确字段表在 models.py，包含 calc/config/rules/capability revision、risk_snapshot_id、account/market、quote_snapshot、reference、normalized bounds、levels、cycles、qty、initial_position_qty、candidates、plan_kind、validation_status）。随机 request_id / 接收时间不进入。

## 3. market/account 身份

- `market_key = deployment|dex|native_instrument_id|product_type|settlement_asset`（"|" 为分隔符，组件禁含 "|"）。HL 主 dex dex=""（与任何命名 dex 严格不同）。
- `AccountIdentity.auth_key = venue|deployment|owner`；account_id 为 UUID；fixture 标志位不入 auth_key。
- `SignerScope(venue, deployment, scope)` 独立于 account_id。

## 4. grid-calc-v1（planner.py）

- Context(prec=50, ROUND_HALF_EVEN)；tick 映射 ROUND_HALF_UP，正中点取高 tick。N∈[2,60]（fixture）。
- 错误码：NORMALIZED_RANGE_EMPTY / PRICE_LEVEL_COLLISION / PRICE_LEVEL_ORDER / PRICE_LEVEL_OUTSIDE / PRECISION_RANGE / INTEGER_REQUIRED / DECIMAL_STRING_REQUIRED。
- 布局：中性=最近参考线留空（等距取高）→ center_index；买=below、卖=above。long entry<ref 候选挂买；entry≥ref：immediate→初始库存（short 按 exit 价降序），deferred→pending_cycles。参考价出 [L*,U*] → WAITING_RANGE 全空。
- post-only：买须 < ask，卖须 > bid；否则 WAIT_POST_ONLY（保价不改）。
- 初始成交分配：出口价离参考近者优先（多头同距取低出口，空头取高出口——通过 distance+bias 排序实现）；每份 ≤q；份额须达 min_qty 且 qty×exit≥min_notional 才成退出需求；费按量比例分摊、末份承担余数，合计=实际费；off-grid fill 拒绝。

## 5. sizing（sizing.py）

- `k_min` = min_qty 步数与 min_notional/最低使用价 步数的较大者；`k_max` 由 max_qty_base 封顶（有限枚举）。
- 选择最大可行 k：ordinary ≤ total−reserve ∧ adverse ≤ total ∧ account ≤ account_remaining。
- 无解 → INVALID BUDGET_EXCEEDED（qty_base=null）；模型缺失 → BLOCKED CAPABILITY_UNVERIFIED（qty=null）。TEST_TABLE_ONLY 为测试输入，不是保证金模型。

## 6. 运行库 schema（store.py，SCHEMA_VERSION=3）

- 迁移逐条显式事务；`PRAGMA user_version` 同事务更新；未知更高版本拒绝打开（SCHEMA_TOO_NEW）。语句切分不使用 `executescript`（会隐式提交）。
- 表：meta, accounts, instruments, capabilities, events(event_key UNIQUE, append-only), checkpoints, previews, request_dedup(PK(owner_scope,request_id)), counters, scope_claims, grids(partial UNIQUE(account_id,market_key) WHERE status!='CLOSED'), grid_versions, grid_levels(PK+UNIQUE price), order_intents(cause_key UNIQUE), exchange_orders(PK(account,client_id)), dispatch_attempts, executions(execution_key PK), inventory_lots, pair_allocations, funding_payments, capital_movements, budget_reservations, operations(partial UNIQUE(grid_id) active), runtime_state。
- 连接：WAL + synchronous=FULL + foreign_keys + busy_timeout=5000ms。StoreWriter 专用线程持写连接；`write()` 异步等待提交；`close()` 排空已接收写入；关闭后写入 → STORE_CLOSED。
- 幂等：同 (owner_scope, request_id)+同 hash → 原结果；不同 hash → IDEMPOTENCY_CONFLICT。
- 执行去重：同 execution_key 同经济事实 → duplicate；不同数量/价/方向/账户/市场 → DATA_CONFLICT。费用后补 = amend（qty 不变）。
- 备份：sqlite online backup；manifest 从完成的副本读 schema 版本 + max(events.seq) + integrity_check；恢复默认 RECOVERING（调用方语义）。

## 7. CLI（preview.py）

`python3 -m entropy_arb.grid.preview --fixture <vectors.json> --case <ID>`；只消费 input；支持 kind 见 SUPPORTED_KINDS；不支持 → 退出码 3；计算拒绝 → 退出码 1 + 拒绝封套；成功输出含 schema_version/calc_version/plan_kind/live_eligible（恒 false）。

## 8. 规划中的合同（对应批次细化，不预填）

- ~~HTTP/RPC 路由与错误码~~ → 已实现，见 §9-11。
- GridVenue adapter 合同 → 已实现（adapters/base.py），见 §12。
- intent 状态机 → 已实现，见 §13。

## 9. RPC 合同（grid/rpc.py，UDS JSON-line）

- 信封：`{protocol_version:1, method, params, request_id}`；响应 `{ok, result|error{code,message,status}, request_id, runtime_epoch}`。
- `hello` 握手返回 compatible/execution_mode/runtime_id；不兼容时 Console 禁写。
- 方法表：hello/status/preview/create/list/detail/start/pause/resume/stop_flat/stop_keep/recover/performance/levels/orders/fills/events/pairs/capabilities/metrics/sim_step（sim_step 仅 simulation）。
- 客户端超时按**原 request_id** 重查 status；`call_idempotent` 将 request_id 注入 params 供 handler 绑定。

## 10. HTTP 合同（console/grid.py → /api/grid/*）

路由：GET status|metrics|capabilities|requests/{rid}|list|{id}|{id}/levels|orders|fills|pairs|events|performance；POST previews|（创建）|{id}/operations|{id}/operations/previews|{id}/sim-step。
错误映射：RpcError.status → HTTP；RUNTIME_UNAVAILABLE=503+stale；PREVIEW_EXPIRED/STALE/CONSUMED、IDEMPOTENCY_CONFLICT、ACCOUNT_IN_USE、DATA_CONFLICT=409；PLAN_NOT_VALID/CAPABILITY_UNVERIFIED/BUDGET_EXCEEDED=422；VALIDATION_ERROR=400/422。
写保护：Authorization header token only（query token 拒绝 401 AUTH_REQUIRED）；无 token 时跨源 Origin 拒绝 403。
创建响应 202 `{grid_id}`；operations 响应 202 `{operation_id, status...}`。

## 11. 网格错误码

GridModelError/CalcError：UNKNOWN_FIELD、MISSING_FIELD、DECIMAL_STRING_REQUIRED、DECIMAL_TEXT_FORMAT、DECIMAL_NEGATIVE_FORBIDDEN、INTEGER_REQUIRED/INTEGER_RANGE、TIMESTAMP_FORMAT、UNSUPPORTED_PRODUCT、FEATURE_UNAVAILABLE、VALIDATION_ERROR、NORMALIZED_RANGE_EMPTY、PRICE_LEVEL_COLLISION、PRICE_LEVEL_ORDER、PRICE_LEVEL_OUTSIDE、PRECISION_RANGE、INTERNAL_FEE_MISMATCH、DEMAND_TABLE_INCOMPLETE。
Store：SCHEMA_TOO_NEW、SCHEMA_GAP、STORE_CLOSED、IDEMPOTENCY_CONFLICT、DATA_CONFLICT、NOT_FOUND、BACKUP_CORRUPT。
Lifecycle/Runtime：OPERATION_CONFLICT、BAD_STATE、NOT_FOUND、ACCOUNT_IN_USE、ACCOUNT_UNRESOLVED、PREVIEW_NOT_FOUND/EXPIRED、PLAN_NOT_VALID、BUDGET_EXCEEDED、CAPABILITY_UNVERIFIED、SIM_ONLY、MODE_STORAGE_CONFLICT、FIXTURE_IN_PRODUCTION。

## 12. GridVenue adapter 合同（adapters/base.py）

- `resolve_account/load_instrument/account_snapshot/market_snapshot/submit/cancel/lookup/open_orders/fills/funding_payments/poll`。
- SubmitResult ACCEPTED/REJECTED/UNKNOWN（HTTP 200 ≠ 已挂单）；LookupResult FOUND_OPEN/FOUND_TERMINAL/NOT_FOUND_TRANSIENT/ABSENT_FINAL/UNKNOWN（单次 404 ≠ ABSENT_FINAL）；CancelResult SUBMITTED/TERMINAL/UNKNOWN（成功 ≠ 未成交）；HistoryPage.records+next_cursor+complete+truncated（空 records ≠ 全覆盖）。
- OrderRequest.client_id 由引擎持久分配；venue 可声明 `client_id_kind = "numeric"`（引擎生成纯数字 id，如 fixture_b），adapter 永不重写。
- adapter 可声明 `risk_model`（仅测试离散表）与 `max_qty_base`；真实 venue 缺认证风险模型 → production preview BLOCKED。

## 13. 意图状态机（journal.py）

```
PLANNED → DISPATCHING → ACCEPTED → OPEN ⇄ PARTIAL → FILLED
DISPATCHING → REJECTED | UNKNOWN | EXPIRED_EPOCH
OPEN/PARTIAL → CANCEL_REQUESTED → CANCEL_PENDING → CANCELED/FILLED/UNKNOWN
UNKNOWN → OPEN/PARTIAL/FILLED/CANCELED/REJECTED（查实后）
PLANNED → BLOCKED_SLOT → PLANNED；EXPIRED_EPOCH 仅 PLANNED/DISPATCHING 可达且为终态
```
竞争规则：转移守卫拒绝弱化（FILLED 不回退）；cancel 撞 fill → mark_terminal FILLED 胜出。

## 14. 成交事务（actor.apply_fill，单事务）

insert_execution（dedup；duplicate 全路径无操作）→ exchange_orders.cumulative（Decimal Python 加法）→ intent PARTIAL（不覆盖 FILLED）→ FIFO consume 对手 lots → 残余开新 lot → pair_allocations（费用比例分摊）→ refill intent（cause_key=`refill:{execution_key}` SQL 唯一；门槛 min_qty/min_notional；exit-flat 单不补单）→ fill_applied 事件（含 source_seq）→ checkpoint。

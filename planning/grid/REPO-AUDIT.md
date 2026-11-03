# REPO-AUDIT — 网格开发代码基线审计

日期：2026-10-11。范围：本文件记录网格开发开始时的仓库/工作区基线与集成差距。所有结论来自实际读取的文件（路径:行号可核对）。

## 1. 工作区与 git 基线

- 分支 `codex/console-v2`，HEAD `c57de9a`（fix: own-session book feeds crashed at start）。
- 未提交改动：`GRID-TRADING-PLAN.zh-CN.md`（修改）、`GRID-TRADING-AI-HANDOFF.zh-CN.md`、`GRID-TRADING-AI-PROMPT.zh-CN.md`、`GRID-TRADING-DEVELOPMENT-SPEC.zh-CN.md`、`planning/`（新增）。均已保留，未覆盖。
- `data/` 仅含 `console-v2.sqlite3`(+shm/wal)。网格运行库 `data/grid-runtime.sqlite` 尚不存在，由本次开发新建。
- Python：`.venv` Python 3.14.4，pytest 9.1.1，aiohttp 3.14.3。测试命令 `.venv/bin/python -m pytest`。

## 2. 已读路径与关键事实

### 2.1 交易路径上的 float（网格必须绕开）
- `maker.py:44-63` `FillEvent(qty_delta, px, fee, ts)` 全 float；`qty_delta` 是增量不是累计量。
- `venue_hl.py:223` qty 为 float `round(qty, 8)`；`:261-262, 304-305` `filled_base/avg_px` 经 `float()`。
- `venue_backpack.py:65-67` `_f/fnum` 宽松 float 解析，fill 数量/价格/费用/累计量全走它（:450, 536, 700-717）。
- 结论：新 `ExecutionEvent`（grid/models.py）从原始字符串解析 Decimal，禁止复用 FillEvent 数值。旧引擎行为保留不动。

### 2.2 订单身份与查询缺口
- `venue_backpack.py:390-393` clientId 由 adapter 进程内计数器生成（重启不复用、不持久）；fill 事件用 venue order id + ws `c` 字段关联（:739）。
- 无任何 adapter 提供 REST open-orders 查询（HL 无；Backpack `open_orders()` :365-370 仅私有流内存视图，流断开即空/未知）。
- 无 fills 历史 REST 端点实现；无 funding 端点（Backpack）；HL `fundingHistory` 422（:65 注释，2026-10-01 实测）。
- HL 私有成交流不存在（无 userFills 订阅）；仅公开 `HLBookFeed`。
- 结论：恢复屏障依赖的「open orders 快照 + fills 回补 + funding 支付」在现有 adapter 层全部缺失 → 记入 VENUE-CAPABILITY-AUDIT，真实资格关闭；模拟 adapter 按同一合同实现全部端点。

### 2.3 可复用组件
- `venues_common.py:115-244` `OrdersFeedBase`：重连框架（1→30s 退避）、去重状态在内存（cap 2048/4096，进程重启丢失）→ 网格执行去重必须在数据库（已按规格设计）。
- `venues_common.py:249-487` `SeqBookFeedBase`：snapshot+seq 纪律，可复用。
- `venues_common.py:97-110` `classify_http`：429⇒RATE_LIMITED 确定性、5xx⇒unresolved —— 与网格重试表语义一致，可复用。
- `venues_common.py:50-90` `fnum/step_decimals/round_grid/grid_str/px_round_grid`：旧 float 量化助手，网格不用（grid-calc-v1 用 Decimal），保留给旧引擎。
- `filelock.py` `file_lock(path)`：fcntl advisory 锁，可作进程控制权锁的基础，但需按 signer/account 作用域扩展。
- `eventlog.py`：队列满丢记录、degraded 标记 —— 仅诊断用，交易事件走 store 事务（spec §2）。
- `console/storage.py`：迁移模式（逐条事务 + PRAGMA user_version，:29-300）、Decimal 字符串存储先例（inventory_lots.qty TEXT）。无 StoreWriter/backup helper —— grid store 新建。
- `console/operations.py`：OperationError(code,status)、preview TTL 30s、request_id 幂等（内存版）、锁 sorted 顺序 —— 语义可借，网格版需持久化。
- `console/server.py:48-61` auth 中间件：token Bearer header 或 `?token=` query；仅保护 `/api/`。新增网格写路由按 spec §13.3 不接受 query token（HTTP 网格路由走 header；本机 RPC 走 UDS）。
- `console/supervisor.py`：端口 8801-8899、/proc adopt、SIGTERM 25s→SIGKILL。网格 Runtime 独立进程，不进 supervisor（spec §15）。
- `tests/maker_contract.py`：venue 无关 maker 合同测试骨架（7 checks + per-venue shim）—— 网格合同测试按同一思路建 `tests/grid_contract.py`。

### 2.4 依赖事实（来自实际环境与 requirements）
- `requirements-live.txt`：lighter-sdk @ git+（未固定 commit）；hyperliquid-python-sdk>=0.9；eth-account>=0.10；cryptography>=42.0；bulk-keychain>=0.1.30。
- `.venv` 实际版本：aiohttp 3.14.3，pytest 9.1.1。lighter-sdk 在 macOS 本地编不过（memory: local-dev-env-quirks）→ 网格 Lighter 适配无本地验证路径，标记 blocked，不影响其它 venue。

### 2.5 V2 前端
- `webui/v2/app.js:12-41` ROUTES 数组 hash 路由；页面 `mount(container, ctx) → {refresh?, destroy?, beforeLeave?}`；`navigate()` 保证销毁。
- 网格新增路由 `#/grid`、`#/grid/create`、`#/grid/:id`、`#/grid/:id/adjust` 需注册在静态路由前（避免 `:id` 吞掉 create）。

## 3. 集成差距表（开发动作依据）

| 差距 | 影响 | 本次动作 |
|---|---|---|
| 成交值全 float | 网格账本不可用旧值 | 新 ExecutionEvent 原始字符串→Decimal；adapter normalize 契约 |
| 无 open-orders/fills/funding 查询 | 恢复屏障、UNKNOWN 查实 | 网格 GridVenue 合同要求实现；真实资格按证据关闭；模拟 adapter 全实现 |
| clientId 适配器内生成、不持久 | 补单幂等、UNKNOWN 关联 | OrderIntent.client_id 由 Runtime 持久分配；adapter 禁止替换 |
| 去重仅在内存 | 重启重复入账 | executions 表唯一键 + recovery 回补 |
| 旧 console 库是唯一写者约定 | 新运行库隔离 | grid-runtime.sqlite 独立，console 只读投影 |
| 账户占用仅内存锁 + `venue:dex:SYMBOL` 键 | 跨进程/真实账户维度缺失 | scope_claims 表 + 本机文件锁（G04） |
| lighter-sdk 本地不可编译 | Lighter 网格 blocked | 记录阻断，不阻塞其它 |

## 4. 基线测试状态

开发开始时运行基线回归（记录见 IMPLEMENTATION-REPORT.md）。命令：
`.venv/bin/python -m pytest tests/test_config.py tests/test_venue_registry.py tests/test_golden_legconf.py tests/test_strategy_identity.py tests/test_operations.py -q`

# IMPLEMENTATION-REPORT — 实现与验证报告

> 历史开发记录，保留用于对照。下文“全部实现并集成”和高级功能完成结论已被审查推翻，不能作为当前验收依据。当前交付见 [IMPROVEMENT-REPORT.zh-CN.md](/Users/alex/Work/entropy/planning/grid/IMPROVEMENT-REPORT.zh-CN.md)，逐项状态见 [IMPLEMENTATION-STATUS.md](/Users/alex/Work/entropy/planning/grid/IMPLEMENTATION-STATUS.md)。

日期：2026-10-11。本次开发交付了完整单所模拟系统 + 第二所合同验证 + 高级功能，全部真实实现并集成（无空模块、无演示假流程）。真实交易资格全部保持关闭。

## 1. 关键文件

### 核心引擎（entropy_arb/grid/）
| 文件 | 内容 | 测试 |
|---|---|---|
| models.py | 严格 DTO（DecimalText/严格 int/tz ISO/未知字段拒绝）、config_hash/plan_hash（PLAN_HASH_FIELDS）、身份（Account/Market/SignerScope）、Plan/ExecutionEvent/OrderIntent | test_grid_config, test_grid_identity |
| store.py | 独立运行库 SCHEMA_VERSION=4（迁移逐条显式事务，不用 executescript；user_version 同事务；未知更高版本拒绝）；单写线程（async 结果 call_soon_threadsafe 回送——修掉了跨线程 set_result 死锁）；event_key/execution_key/cause_key/(owner_scope,request_id) SQL 级唯一；insert_execution DATA_CONFLICT；fee 修订不增量；online backup + manifest 从完成副本读 | test_grid_store |
| planner.py | grid-calc-v1（prec=50、HALF_UP 中点取高 tick、碰撞/乱序/出界/空区间拒绝、中性中心空线等距取高、post-only 保价等待、WAITING_RANGE、初始成交近出口分配+费用守恒末份吸收余数、off-grid fill 拒绝） | test_grid_planner（25 个固定向量+独立边界） |
| sizing.py | RiskModel 协议 + 测试用离散表求解（k_min 双约束/k_max 封顶/最大可行 k/BUDGET_EXCEEDED/CAPABILITY_UNVERIFIED） | test_grid_planner |
| preview.py | 离线 CLI（只消费 input；拒绝封套 code=VALIDATION_ERROR+reason；不支持 kind 退出码 3） | 向量参数化 |
| scope.py | G04：scope_claims 经济占用（市场互斥含同 owner）+ fcntl 本机锁（排序获取/失败回滚）+ legacy 桥接键 | test_grid_engine, test_grid_api |
| journal.py | G06：意图状态机（单调表、FILLED 终态、EXPIRED_EPOCH 仅未发送、race loser 保强状态） | test_grid_store/engine |
| executor.py | G06：持久 client id（string/numeric 双形态按 venue 声明）、dispatch attempt append-only（request hash 先写）、UNKNOWN 查询回环（ABSENT_FINAL→PLANNED 合法重发）、撤单竞争 fill 胜出 | test_grid_engine |
| actor.py | G07：成交事务（dedup→cumulative→FIFO lots→pairing→refill→checkpoint 单事务）；补单=成交档位相邻退出（中性非 reduce-only，多空强制）；DUST 门槛 | test_grid_engine |
| inventory.py / pairing.py | FIFO lots、配对行构建（费用比例分摊、双向） | test_grid_ledger |
| ledger.py | G12：性能主账本（gross_realized FIFO+unrealized+资金费−费用）+ 配对解释层（不叠加）；E01/E02 精确 | test_grid_ledger |
| lifecycle.py | G08：pause/resume/stop_flat/stop_keep 持久 saga；CLOSED 仅仓位证明为零；残仓占市场；desired_mode 与恢复语义 | test_grid_engine |
| recovery.py | G09：回补（cursor=最后已应用 fill 的 source_seq）+UNKNOWN 查实+外来挂单漂移+仓位对账 → NEEDS_ATTENTION | test_grid_engine |
| runtime.py | G10：创建事务（preview 消费+claims+version+levels+预算）、派发/排水、recover 按 desired_mode 恢复、performance、metrics | test_grid_engine/api |
| rpc.py | G10：UDS JSON-line（协议握手/幂等 request_id/超时原 id 查询/错误码映射 StoreError/LifecycleError/GridModelError） | test_grid_api |
| operations.py | G16：adjust saga（新版本+旧 epoch 失效+库存保留+幂等重试）、add_buffer/reduce_capital（worst-case 守卫）、自动化政策（默认关/额度/触发键去重） | test_grid_operations |
| conditions.py | G17：价格条件（触发先持久化+去重；PnL/ROI=FEATURE_UNAVAILABLE）+ TrailingGrid（上限/冷却/失败不消耗） | test_grid_operations |
| recommend.py | G18：分位数带推荐（样本不足/成本不足拒绝）+ replay（与 E01 数字一致） | test_grid_operations |
| adapters/base.py | GridVenue 合同 + Submit/Lookup/Cancel/HistoryPage 语义类型 | grid_contract |
| adapters/fixture.py / fixture_b.py | 模拟所 A/B（不同 id 形态/执行键构成/历史窗口/保护语义）+ 故障注入 | grid_contract |

### 服务与前端
- `grid_main.py` — Runtime 服务入口（mode 决定 execution_mode；production 无认证 adapter 不注册；simulation 强制 sim 库路径）。
- `entropy_arb/console/grid.py` — HTTP 代理路由（/api/grid/*，错误码映射、写路由 header-token-only、Origin 防护、离线 503+stale）。
- `entropy_arb/console/server.py` — 仅 +2 行注册调用，旧路由零改动。
- `entropy_arb/webui/v2/grid/{common,list,create,detail}.js` — V2 页面（幂等 request_id、迟到预览 seqGuard、服务器计划直显、sim 驱动仅 simulation 可见、断连 stale 显示）。
- `entropy_arb/webui/v2/app.js` + `console-v2.html` + `strings.js` — 4 条网格路由 + 导航 + 双语键。
- `deploy/entropy-grid.service` — 独立 systemd 单元（GRID_REAL_DISPATCH=0 默认、RECOVERING 语义、与 console 服务模型隔离）。

## 2. 验证命令与结果（真实运行）

```
.venv/bin/python -m pytest tests/test_grid_config.py tests/test_grid_identity.py \
  tests/test_grid_planner.py tests/test_grid_store.py tests/test_grid_ledger.py \
  tests/test_grid_engine.py tests/test_grid_api.py tests/test_grid_conformance.py \
  tests/test_grid_operations.py -q
→ 144 passed

.venv/bin/python -m pytest tests/test_config.py tests/test_venue_registry.py \
  tests/test_golden_legconf.py tests/test_strategy_identity.py \
  tests/test_operations.py tests/test_console_api.py tests/test_console_v2.py -q
→ 45 passed（旧回归绿）

for c in P01..Q04: .venv/bin/python -m entropy_arb.grid.preview \
  --fixture planning/grid/acceptance-vectors.json --case $c  → 全部符合 expected
git diff --check → clean
```

## 3. 浏览器完整闭环（真实 HTTP→RPC→Runtime，无 mock 路径）

启动：
```
.venv/bin/python grid_main.py --mode simulation --db data/grid-runtime-sim.sqlite3
.venv/bin/python console.py --port 8799
```
浏览器 http://127.0.0.1:8799/console-v2#/grid：创建（预览 VALID，live_eligible=false）→ 202 创建（同 request_id 幂等）→ 启动（5 单挂出）→ 步进 104/108（成交/补单/配对 T1↔T2）→ performance（net 0.0116184 = gross 0.024 + unrealized −0.012 − fees 0.0003816，reconciled）→ 暂停（PAUSED/epoch 1/未发送意图 EXPIRED）→ 结束平仓（CLOSED、仓位 0、滑点 0.001944 如实入账）→ 列表/360px 无溢出。截图存 ZCode artifacts。

## 4. 兼容情况

- 旧 console server：仅追加注册行；旧回归 45 项全绿。
- 旧 maker 合同/黄金测试：零改动（venue_* 文件未动）。
- 全局 decimal context：grid-calc-v1 使用局部 Context，旧账本不受影响。
- 新依赖：零（stdlib + 现有 aiohttp）。

## 5. 部署与回滚

- 部署：`cp deploy/entropy-grid.service /etc/systemd/system/ && systemctl enable --now entropy-grid`（production 先保持 GRID_REAL_DISPATCH=0，只读观察）。
- 升级：暂停新建 → `store.backup()`（online backup+manifest）→ 迁移逐条事务 → Runtime 重启进恢复屏障 → 只读观察 → 放行市场。
- 回滚：旧 binary 打开 v4 库会拒绝（SCHEMA_TOO_NEW）→ 恢复旧备份（restore_from_backup）→ 默认 RECOVERING 重放回补，不丢升级期真实成交。新库首次部署即含 migration 1–4，无跨版本问题。

## 6. 未验证事项

见 LIVE-READINESS.md（真实账户路径全部未验证；HL/Backpack 真实 adapter 未实现；PnL/ROI/webhook 触发、原生保护、导出下载、SSE 推送为 P1 结构就位状态）。

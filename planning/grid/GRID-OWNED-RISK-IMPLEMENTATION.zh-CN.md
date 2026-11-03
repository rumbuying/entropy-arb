# 网格存量仓位风险与预算进度

更新：2026-10-11。标准全仓的本网格存量风险模型已接入恢复、调整与资金预算操作，全项目 **743 passed、2 skipped、99 warnings（48.58秒）**。这是离线实现与故障验证；**实盘认证仍为0，生产 write_enabled 与适配器默认 live_qualified 保持 false，真实资金体验目标未完成。** 用户尚未准备账户，后续凭证在独立账户页面填写。

本报告保留存量阶段743项证据。后续已接入发送前/签名后风险复验、周期核对和最新报价的减仓退出，最新764项验证与剩余实盘门槛见[收尾入口](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-CLOSEOUT.zh-CN.md)。

## 已实现的行为

- 新建网格仍要求无现有订单/仓位的专用账户。已有网格的恢复和调整使用服务端生成的库存上下文：账户、市场、epoch、原版本、成交/资金费覆盖、预算、真实成交与库存原始订单。普通创建 RPC 不能提供这个上下文来认领外部仓位。
- 核对实际账户的全部主永续仓位和挂单。实际仓位必须等于本网格库存与成交净数量；外来币种/挂单、价格/数量/身份不一致、已改变杠杆/逐仓模式、未决订单、缺覆盖或保证金证明，拒绝生成风险计划。当前范围仍是标准全仓，未放行统一、组合或逐仓保证金。
- 保留已经证明属于本网格的仓位初始保证金，并以实际全仓净值限制账户资金容量。未证明可释放的挂单冻结不回加。预算同时受系统预留和当前实际容量限制，避免将已有仓位保证金重复扣作新需求。
- 策略预算使用当前系统预留，加上显式规模修改的差额，减去已发生的负净损益。手续费、实际资金费和按当前 mark 的损益均来自本网格主账本；正净损益不自动扩大预算或数量。追加缓冲和减少预算保持原配置版本与每格量。
- 风险模型计算满网格库存、旧版本携带库存，以及重新选择中心后当前买/卖候选可能造成的最大净暴露；维护保证金继续使用连续档位。费用预算区分已有库存未来退出和未占用逻辑格的未来进出，已付费用在净损益中记录。
- 风险与发单共用库存重建代码，保留旧版本退出价。压力包络纳入当前 mark、新区间、存量退出价和仍在工作的旧版本减仓价格，即使原库存已被其他订单平掉。恢复核验原数量；显式调整可选择新数量，提交时再次对账和核验该数量，规则/价格线改变或新增亏损不能静默沿用旧预览。
- 在风险计划保存前，用原生 wire 规则验证实际重建候选，包括旧版本退出订单。小额余量不满足当前最小金额时，进入待处理状态，不先签发新增订单。原生小额平仓语义尚待实现与认证。
- 启动先验证存量风险，再做账户控制，并在控制确认后再次验证同一数量。已有杠杆正确时只回读确认，不发送新的设置动作。调整失败的清理同样受 epoch 约束。
- 新版本提交会清理已由实际库存重建的旧补单队列。旧版本库存退出使用 reduce-only；其平仓不再重启旧开仓循环。旧订单迟到成交仅为实际新增剩余库存生成退出需求，新的交易循环归当前版本管理。

实现入口：[库存映射与风险上下文](/Users/alex/Work/entropy/entropy_arb/grid/inventory_layout.py)、[全仓风险模型](/Users/alex/Work/entropy/entropy_arb/grid/hl_risk.py)、[Runtime](/Users/alex/Work/entropy/entropy_arb/grid/runtime.py)、[预算与调整](/Users/alex/Work/entropy/entropy_arb/grid/operations.py)、[成交与补单](/Users/alex/Work/entropy/entropy_arb/grid/actor.py)、[原生适配器](/Users/alex/Work/entropy/entropy_arb/grid/adapters/hyperliquid.py)。

官方基础：[初始保证金与包含浮盈的全仓净值](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining)、[维护保证金连续档位](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers)、[账户净值、占用保证金与仓位字段](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)。库存包络、资金容量约束和费用预留是本项目的保守模型推导，仍需实际账户对账与场景认证。

## 验证证据

```sh
.venv/bin/python -m pytest tests planning/grid/review/test_review_regressions.py -q --tb=short --disable-warnings
# 743 passed, 2 skipped, 99 warnings in 48.58s

.venv/bin/python -m pytest tests/test_grid_hl_owned_risk.py tests/test_grid_hl_risk.py tests/test_grid_operations.py tests/test_grid_engine.py planning/grid/review/test_review_regressions.py -q --tb=short
# 77 passed in 2.76s
```

新增[26项存量风险用例](/Users/alex/Work/entropy/tests/test_grid_hl_owned_risk.py)，包含保证金/费用/重选中心/旧退出价及工作中旧减仓价算例，实际形状的仓位与挂单证据，各类漂移拒绝，以及恢复、调整、亏损后阻断新单、预算追加/缩减/重放和操作抢占。

完整执行用例使用进程内模拟交易所传输和记录签名器；测试替身临时获得实例资格，所有 exchange 动作只写内存。证明带库存恢复保留数量、调整保留旧退出价、旧平仓不重开旧循环、迟到资金费使调整停止，以及小额退出在签名前被阻断。没有实际账户、真实私钥或真实 exchange 调用；这些测试不构成实盘认证。

Python 编译和已跟踪差异检查通过。两个环境跳过及99条 aiohttp 警告保持既有情况。本阶段没有修改网页；页面验收仍沿用账户与执行阶段的历史证据。

## 仍需完成

1. 持续运行中的预算/保证金变化、区间外与资金费压力的 Runtime 风险监控和保护动作，以及提交前对新的持仓/预算变化的校验。本轮完成的是启动/恢复/调整/预算操作校验，未宣称所有日常补单已接入持续风险模型。
2. 原生/本地保护、小额残仓、停止保护与平仓最终性。当前小额规则阻断可以避免先新增风险，仍不是可用的小额平仓能力。
3. 实际成交/资金费保留边界、经济排序与发布延迟证明，真实私有流及回补认证。生产认证 floor 仍为空，120秒资金费重查不是完整性证明。
4. 隔离测试环境/小额账户的授权、SDK签名、设置控制、成交/撤单竞争、断线与保护认证，Linux 部署演练及具备证据的资格放行。

网格与套利继续使用独立页面、配置和凭证，由使用者保证不同资金账户与签名凭证，不接入跨策略互斥或旧 worker 桥接。上一阶段记录：[资金费与首次对账](/Users/alex/Work/entropy/planning/grid/GRID-FUNDING-RECOVERY-IMPLEMENTATION.zh-CN.md)。

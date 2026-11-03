# 网格实现当前状态：G01–G18

2026-10-14 多币种推进：同一账户的ETH正式引擎已真实启动，完成0.0118 ETH建仓、两笔网格挂单、原生整仓止损，以及0.0001 ETH完整小额只减仓退出；全部撤净CLOSED，主网仓位/挂单0，USDC余额200.03119，本轮成本0.011237 USDC。修复小额退出误拒合法兄弟币种仓位，改为核对全账户准确归属及数量，外来/不支持/变化的持仓仍拒绝；全量1027 passed、2 skipped，空仓后部署。BTC普通网页入口继续可用；ETH须取得真实补单循环和恢复证据后再开放资格。详见[ETH主网验收](/Users/alex/Work/entropy/planning/grid/GRID-ETH-NATIVE.zh-CN.md)。继续多币种并行、其他方向和部署验证，目标active。

2026-10-14 最新：普通网站实盘创建已开放并验证。当前已保存账户的BTC主永续支持做多、GTC、立即/延迟建仓、1–3倍全仓，单网格授权上限200 USDC，实际资金/预留/风险仍逐次核对；其他币种/设置保持只读，不绑定账户币种。资格由第十四轮真实循环/恢复和第十六轮真实小额完整退出支持，加载时再次核对原生订单/成交/仓位，预览和创建事务绑定资格版本；总开关关闭或撤销资格禁止新增风险，但保留原范围退出。普通HTTP创建的 `g-99cbabbdd0c64200` 已完成真实建仓、挂单、保护和普通HTTP停止退出，独立主网确认仓位/挂单0，余额200.042427，本轮验收成本0.01579 USDC。网页预览等待采用独立连接，实测显示校验通过及实盘创建按钮；全量1022 passed、2 skipped。详见[普通网站实盘入口](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-RELEASE.zh-CN.md)。继续其他方向、多币种共享账户和部署验证，目标active；不能据此宣称完整币安/欧易能力已实现。

2026-10-12 正式引擎实盘更新：已在主网成功启动两侧中性网格，取得0.00034 BTC真实网格成交、手续费和资金费记账，并完成受控恢复后的正式只减仓退出及订单清理。第五轮合计成本0.077046 USDC，退出后余额199.688124、仓位/挂单0；补单虽自动生成，但持续页面读取导致额度等待，尚未实际提交。已修复主循环监督阻塞、停止续接、未发送退出恢复、查询优先级及页面共用观察；第六轮4格验收进行中。普通网页实盘仍关闭，目标active，不能视为完整网格循环认证。详细事实与最新测试见[正式引擎验收](/Users/alex/Work/entropy/planning/grid/GRID-ENGINE-TRIAL.zh-CN.md)。

2026-10-12 实时观察与中性保护更新：真实账户的11个永续DEX/共享资金已通过持续订阅接入，缺订阅/快照/连接代次或心跳时失效；分别提交的两侧整仓止损在主网同时存在并均撤销，零费用，余额199.76517、仓位/挂单0。保护触发自动退出、重启续接及UNKNOWN不重发、响应超时状态查询已接入。全量919 passed、2 skipped、108 warnings（61.86秒）。普通网页实盘仍关闭、验收/划转额度0，目标active。下一步直接用正式GridRuntime做受限原生网格闭环与资格认证，见[实时观察和下一步](/Users/alex/Work/entropy/planning/grid/GRID-CONTINUOUS-OBSERVATION.zh-CN.md)。

2026-10-12 正式保护接线更新：整仓止损已进入订单执行器、库存/FIFO、恢复和暂停/停止流程；新增启动前挂置及发单/周期保护核对，中性要求两侧完整保护，保护成交冻结补单。创建页已接入止损价格，订单表显示整个仓位。新增14项保护验证，全量886 passed、2 skipped、108 warnings（58.52秒）。余额199.76517、无实盘网格，本阶段未新增真实交易，普通实盘按钮仍关闭。已实测组合账户WebSocket数据接口；下一步实时观察、原生中性/触发/部分成交、自动收尾与完整网页实盘认证。见[正式保护接线](/Users/alex/Work/entropy/planning/grid/GRID-PROTECTION-RUNTIME.zh-CN.md)，目标仍 active。

2026-10-12 后续真实验收：空仓多头/空头整个仓位止损均可挂置、查询和撤销；新增一次先挂整个仓位止损、再0.00013 BTC真实入场及减仓退出，Native 429期间止损保留，按持久记录完成清理而未重复开仓。本次成本0.01098 USDC，累计成本0.02483，当前余额199.76517、实际仓位/挂单均0。新增保护恢复及共享REST限速，最新872 passed、2 skipped、108 warnings（58.36秒）；新增验收/划转额度均0，普通网格实盘仍未放行。下一步正式网格保护、消息/历史恢复和完整网页实盘闭环，见[整个仓位保护接入](/Users/alex/Work/entropy/planning/grid/GRID-NATIVE-POSITION-PROTECTION.zh-CN.md)。目标保持 active；后文较早余额和测试数为历史记录。

2026-10-12 实盘接入更新：真实主网账户已保存，代理地址已按原生授权关系修正为资金主账户；USDC 统一账户基础检查通过；一次0.00013 BTC真实入场、原生止损挂置、重启清理和只减仓退出已完成，总成本0.01385 USDC。当前可用保证金199.77615，实际仓位和挂单均为0。全量841 passed、2 skipped。已兼容统一模式的 USDC 主永续全仓读取与预算，普通网格真实开单仍关闭。新增独立限额原生挂撤单验收，详情与当前缺口以[实盘接入进展](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-COMMISSIONING.zh-CN.md)为准；下文保留前期检查记录。

2026-10-12 最新：账户页已取消币种绑定，创建网格时选择币种；同账户不同币种的订单／库存独立，签名、资金和组合恢复共享。新增21项多币种离线验证，全项目791 passed、2 skipped、108 warnings（52.49秒）；真实开单仍关闭。见[多币种账户报告](/Users/alex/Work/entropy/planning/grid/GRID-MULTI-MARKET.zh-CN.md)。以下770项为前一阶段记录。

更新：2026-10-12。本文件取代此前“G01–G18 全部完成”的结论。模拟交易核心和网站管理闭环已验证；Hyperliquid 主永续 GridVenue 已实现、离线合同测试通过且真实公共数据只读接入可用。真实适配器实现数量为 1，实盘认证数量为 0，高级自动化没有全部接入产品。

页面恢复：用户已准备账户，本次启动了固定本机8789端口的独立网格网站及持久生产运行时，配置不会随关闭页面删除；旧套利服务未重启。实际浏览器账户页面已加载，保存/关闭后恢复与访问保护新增6项回归。用户尚未提供实际保存/检查结果，真实开单保持关闭；见[持久网站入口](/Users/alex/Work/entropy/planning/grid/GRID-LOCAL-ENTRY.zh-CN.md)。

用户最新调整：[网格与套利模块分离](/Users/alex/Work/entropy/planning/grid/MODULE-SEPARATION.zh-CN.md)。使用者负责不同资金账户；跨策略互斥和旧 worker 治理不属于当前范围。独立账户/加密凭证、签名控制、恢复、资金费、存量模型已实现；最新接入发送前/签名后风险复验、周期核对与新报价退出，见[实盘收尾报告](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-CLOSEOUT.zh-CN.md)。用户要求优先首笔实盘、尽快收尾，真实资金体验目标仍未完成。

| 任务 | 当前状态 | 已有证据与剩余工作 |
|---|---|---|
| G01 账户与能力审计 | 部分完成 | 已纠正 HL / Backpack 无历史接口的错误结论；官方接口有文档，本地网格实现与终局/分页合同仍待认证 |
| G02 模型与运行库 | 离线验证 | 严格 DTO、Decimal、单写线程、schema v6；新增成交/资金费归属、补单需求、预览上下文与配置持久化 |
| G03 planner / sizing | 标准全仓空仓/存量模型已离线接入 | 归属/覆盖证明、已有保证金/实际净值、亏损扣预算、保留原数量、新旧库存与退出价压力；实际模型与连续/区间外保护仍待认证 |
| G04 网格内部账户占用 | 独立页面/加密凭证/执行接线已实现；真实控制待认证 | 进程排他、预算、独立签名锁/nonce；有效期与授权核对、凭证版本竞争与残仓删除阻断；持久杠杆控制与回读，真实执行和授权撤销待认证 |
| G05 精确成交合同 | 模拟验证 + HL 离线合同验证 | HL REST/WS 精确金额、大整数编号、稳定成交键和查询状态已实现；真实私有流、时序与保留边界待认证 |
| G06 journal / executor | 模拟与故障回归通过 | 在途意图不当作未发送失效；UNKNOWN 查询优先；每次原生发送重新观察风险、签名后复验账本/预算/其他订单/消息代次及5秒时效；持久杠杆 nonce/阶段/回读；有限超时 |
| G07 actor / reconciler | 模拟与故障回归通过 | 成交归属与累计量、部分成交聚合、持久补单需求、方向正确的 reduce-only、失败补单阻断新增风险 |
| G08 lifecycle / protection | 模拟与并发回归通过，周期风险接入 | 旧回调不能覆盖新流程；CLOSED 时间不可变；旧版本只退出不重开循环；5秒周期风险/失败暂停撤单、新报价减仓和拒绝/UNKNOWN不连续重发；小额残仓与交易所侧保护仍待做 |
| G09 recovery | 模拟/进程重启与 HL 离线故障验证通过 | 二分回补、截断阻断、持久服务端历史起点；订阅/心跳、后台恢复；首次启动先对账，重建校验连接代次与旧单终局；实际保留/排序/发布边界及认证待做 |
| G10 API / RPC | 离线+模拟验证 | 握手、原请求状态查询、幂等操作结果、单客户端并发响应绑定、写路由鉴权和 Origin、分页、管理接口 |
| G11 开单与详情 UI | 模拟 + HL 只读浏览器验证 | 原模拟管理闭环保留；真实行情/实际费率只读显示、价格参考、数量待校验和开单禁用；本轮 HL 1280/390 验收 |
| G12 账本与总览 | 模拟与数字回归通过 | FIFO、费用守恒；资金费按不可变生命周期归属，原生游标/节流/近期重查，入账与覆盖原子提交，失败显示待核对；真实所对账待做 |
| G13 首家真实所认证 | 实现/离线验证完成；实盘认证未完成 | HL ALO/GTC/IOC、cloid 查询/定向撤单、成交/资金费和官方 SDK 签名已实现，Runtime.add_venue 可只读注册；真实保证金模型、网格内部授权/控制与测试环境认证待完成 |
| G14 部署与稳定性 | 部分完成 | 独立入口、启动恢复、进程排他、可重复模拟重启、指标；systemd 为部署模板，未在 Linux 生产环境验收 |
| G15 第二家真实所认证 | 未完成 | fixture_b 仅离线合同样本，Backpack 真实接入与认证待做 |
| G16 热修改与资金任务 | 手动模拟闭环及原生格式离线验证，自动化部分完成 | 存量调整先撤旧单/对账/重新验证原预览量；保留旧退出价，清旧队列；原生全仓缓冲/减预算接入库存模型与幂等；实际认证和自动化未完成 |
| G17 移动与高级保护 | 辅助代码，产品未完成 | conditions / trailing 判断函数存在；未形成 Runtime 动作、调度、冷却、次数和 UI 完整闭环；收益/ROI/RSI/webhook 与原生保护待做 |
| G18 推荐与回放 | 辅助代码，产品未完成 | 分位数候选与成交重算存在；真实 OHLCV、行情驱动回测、滑点/资金费模型、推荐 UI 与验收未完成 |

## 本轮实际验证

```sh
.venv/bin/python -m pytest tests planning/grid/review/test_review_regressions.py -q --tb=short --disable-warnings
# 最新 770 passed, 2 skipped, 108 warnings in 50.24s

.venv/bin/python -m pytest tests/test_grid_hardening.py tests/test_grid_operations.py planning/grid/review/test_review_regressions.py -q --tb=short
# 40 passed
```

原审查15项失败场景已通过；历史硬化、HL合同、账户/风险、签名控制及恢复用例保留。资金费/首次对账阶段新增35项，存量阶段新增26项，最新阶段新增21项运行风险及原生形状退出验证。两个跳过分别为macOS缺少Linux /proc的worker收养测试和本地缺少bulk_keychain的原有集成测试。警告来自既有aiohttp application key用法。

Python 模块通过 compileall，已跟踪差异的 git diff --check 干净。风险与退出阶段无网页改动；本次增加独立网站入口与共享路由的网格模式，JavaScript 语法与真实浏览器账户页面加载通过。此前网格 JavaScript 与浏览器模拟管理闭环已验收，创建→成交补单→追加缓冲→调整版本→暂停→恢复→平仓结束走通，资金预留80→90→100→0；这些历史页面证据不代表真实资金验收。

最新交付与限制：[实盘收尾](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-CLOSEOUT.zh-CN.md)。前阶段：[存量风险](/Users/alex/Work/entropy/planning/grid/GRID-OWNED-RISK-IMPLEMENTATION.zh-CN.md)、[资金费与首次对账](/Users/alex/Work/entropy/planning/grid/GRID-FUNDING-RECOVERY-IMPLEMENTATION.zh-CN.md)、[执行与恢复](/Users/alex/Work/entropy/planning/grid/GRID-EXECUTION-IMPLEMENTATION.zh-CN.md)。历史接入：[HYPERLIQUID-IMPLEMENTATION.zh-CN.md](/Users/alex/Work/entropy/planning/grid/HYPERLIQUID-IMPLEMENTATION.zh-CN.md)。前轮模拟修复：[IMPROVEMENT-REPORT.zh-CN.md](/Users/alex/Work/entropy/planning/grid/IMPROVEMENT-REPORT.zh-CN.md)。原审查报告保留为修复前记录。

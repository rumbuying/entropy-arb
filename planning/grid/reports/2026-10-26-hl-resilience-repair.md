# HL 临时故障恢复修复与遗留仓位收尾

2026-10-26。用户要求处理，接受断线，要求已有挂单在受保护条件下继续运行。

## 实际资金收尾

原g-490d1620d25b421a已经要求停止，持仓0.0156 ETH、原stop2247。新用户授权只用于收掉旧仓；通过原线上Runtime唯一提交hl-v19-user-close-20261026-v1，未复用旧partial请求，不开新仓/转账/续期。提交前核对旧原请求已终态、无进行中操作/未知金融意图、实际持仓和唯一原止损。

operation1cb5df66-1c37-4b0e-8ce5-d5dea2dc0706 succeeded/remaining0，原grid CLOSED/e16。独立Native审计11个永续部署零仓零单，2笔真实成交/原保护取消匹配，备份通过。最终毛已实现+.05772、交易费.032576、资金费净-.016473，净+.008671 USDC，无网格配对，不算24h通过。

单次帮助CLI在收到成功回执后调用不存在的request_status方法而退出1；未重投、未改原报告。实际原operation及独立Native审计/备份证明闭合，以hl-v19-user-close-20261026-v1-final-audit.json为准。

## 修复行为

- 恢复所需的幂等读取，遇明确408/429/500/502/503/504/传输故障时最多3次退避，共享原90秒上限。查询原生保护也适用；不重放金融请求。覆盖不足、授权错误、漂移不归为临时网络故障。
- HL近期有效原生全仓止损、配置/身份/版本/原订单及看护均有效时，私有连接断线暂留旧挂单，冻结新风险。不更新虚假保护证明、不延长原30秒证明期限；保护失效、看护失联、重武装或超过期限仍走原保护退出。固定数量stop/未建模协议不套用。
- 旧挂单可以在离线时成交；服务离线不能自动补单。恢复先按原游标回补成交/资金费，查询原请求并核仓，之后才处理旧普通单并重建；止损仍按原证据流程保留。
- 恢复激活版本新增事务性recovery_activation_started事件。激活中再次合法断线沿用同一故障的原时钟起点/300秒上限，旧版本晚到报告只在已证明的superseded链上处理；漂移不忽略，用户控制仍获胜。
- stop_flat遇临时对账失败保留原partial记录，并持久进入reconcile队列。已有退出调度器必须完整重新核账、证明原退出请求终态后，才允许新减仓；不会留在一次502导致的partial。已STOPPED平仓允许完整REST核账，不再强依赖私有连接；活动策略仍有流屏障。

## 验证、部署和限制

实际Linux最终295pass/0fail/error/skip，108.17秒，XML SHA5ac66a4cf8d866d808879cf5c8c3edddf5569df5add4002eca0ae68bccb761c0。包含一次502/多次故障、永久覆盖不足、离线真实格式成交回补、保留旧单/保护失效、无新增开仓、退出续办、未知不重发、嵌套断线/旧报告/漂移/期限不重置，及Runtime/SQLite/HL协议集成。先前293是前一冻结版本，不相加。本机新增19项通过；Linux首预检误列3个不存在测试文件，未启动测试，修正后重新执行。

部署从旧线上funding-send底板仅安装runtime.py/lifecycle.py/recovery.py/protection_watch.py/stream_recovery.py五文件，保持未发布的多所Runtime/Executor/Bulk工作独立。66运行文件匹配新grid-hl-resilience-version-v2.json，旧funding-send proof保留但不再当前。监督模块另装data/run/ordinary_supervision-resilience-20261026-v2.py，旧v19私有helper及SHA不改；后续新轮必须引用新模块/新version并重新审核。

先清仓/Native审计/备份，停用仅v19期限任务、清空旧grants，在GRID0切换；有rollback。新HL runtime3823593、watchdog健康同instance，active/pending/unresolved均0。旧套利1223049、Bulkworker3802260/web3222058及InvocationID未变。网站匿名401、原认证页面/metrics/detail均200，CLOSED/net0/real_dispatch=false；第一次metrics503保留v2证据，随后无需重启即v3复核200，原因未唯一定位。

没有新开资金轮、主网带仓故障注入或新24h证据；不将离线模拟/本次清仓当正式实盘验收。当前新开仓关闭，无Mac writer、无AI定时。

证据：data/run/hl-v19-user-close-20261026-v1-final-audit.json、grid-hl-resilience-linux-v2.json/xml、grid-hl-resilience-final-review-v2.json、grid-hl-resilience-public-access-v3.json。

协议参考：[HL官方订单类型](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)、[官方交易接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)。GTC驻留与原生保护不代表离线软件仍能发送补单。

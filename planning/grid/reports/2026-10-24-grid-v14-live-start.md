# Linux新一轮实盘验证已开启（v14）

启动成功核查：北京时间 **2026-10-24 00:50:54**。本次用户明确要求“开启新的实盘验证”，新轮次已实际运行并通过独立Native正向核验，不只是提交任务。

## 当前唯一活动轮次

- 策略：`g-8678b5d46b724281`，请求根身份 `ordinary-observe-linux-20261024-v14`，RUNNING/ACTIVE/e1。
- 原start `afcf6d7d-2b8f-4f50-8cbd-748369ee2a78` succeeded。create/start均只提交一次，未知响应只能查询原身份。
- 严格观察起点 **2026-10-24 00:49:13**（1792774153053），最早满24小时 **2026-10-25 00:49:13**。建议北京时间 **10月25日01:00后**检查连续观察及终局证据。
- 独立25小时退出期限 **2026-10-25 01:42:10**。原合同SHA256 `5f2bff53d70c9df5d152ab5deabc87ef4a44516fb8a45d3c765a8dd57474af5e`，timer已active/waiting；不延长、不复用旧失败轮次。
- 配置：ETH永续做多、3倍全仓、5格、GTC、立即建仓，40USDC资金含20USDC预留，总敞口上限150USDC、原30秒保护门槛。止损价格以本轮原始配置为准。

入口：https://taoli.coinfetcher.xyz/grid/#/grid/g-8678b5d46b724281 ，原登录未改。服务器上的原controller、observer、finalizer和期限自行继续，没有新增或恢复AI定时跟进、没有本机网格writer。

## 已完成的实际核验

独立Native核验时间 **2026-10-24 00:49:14**：真实正仓 **0.0129 ETH**，cross3，**6张working orders**（5个网格订单和1张全仓原生stop），有非reduce-only开仓单，原生STOP查询状态OPEN。实际总敞口 **51.77845 USDC <=150**。SQL前后同一范围/代次且账本仓位精确匹配Native，原始订单身份和参数、实际runtime及guardian均通过。核验器本次sampling_attempts=1；没有声称本轮在线触发过重采样。

最后只读复核：实际runtime PID **2292364**、watchdog PID **2292366**，同instance/新鲜健康；源66项SHA与本轮12项private源码SHA逐一一致。旧套利PID **1223049** 保持原样，本次未改动或重启旧套利服务/目录。

严格soak当时最长 **101.049秒**、issues={}，**ordinary_24h_observed=false**，不能当成24小时验收或整个多交易所产品完成。后续任何断线导致的观察失效都必须保留，不拼接窗口。

网站匿名401，原账号认证页面200、metrics200/production/real_dispatch=true、详情200/RUNNING。初始化并行只GET负载：详情71次200，最大耗时约0.295秒；行情2次200、3次503、4次延后。不能将行情延后说成所有HTTP均成功；交易及保护优先读取时行情仍可能暂不可用。

## 本次处理的启动核验阻塞

先前本次尝试v13 `g-0c92b0053ec248bf` 的原start曾成功，但RUNNING边界发生正常网格成交，单次Native检查在仓位比较处失败。现场只有断言及成交时间，没有完整保存当时读到的两个数量，不能补造原始样本或声称排除了所有原因。原cleanup `2aef9e81-57a6-4034-ae95-cf6ff22144ad` succeeded，CLOSED/e2，Native-audit及backup均0，再fresh11DEXflat+backup后containment完成，清grants/GRID0并停仅v13timer。其净损益 **-0.024904USDC**，不是有效的24h观察或网格配对盈利。原失败证据、原期限保留，不复用身份。

仅修复新的私有只读Native检查器：正常成交跨读时丢弃未确认样本，在**原共享启动600秒/运行600秒窗口内**重采样。最终成功仍要求SQL前后完全一致、Native实际正仓精确匹配、全部Native订单归属参数、ETH/cross3、原生STOP OPEN、working entry、gross<=150、同scope/epoch/runtime/guardian。未变化账本与Native持续不符最多10秒失败；foreign订单/额外仓位/缺止损/控制变更等直接失败。重采样不下单、不改变账本、不续Native保护或25h期限，也不重置24h观察。

生产核心源码未再修改或重装，仍为1638项实际Linux全过的initial-exit-slots版本/66source/原v35起11delta/qualified=false。新v14实际Linux监督测试150项0skip；新增16采样/严格安全/共享期限用例后，工作流50项全过，另6HTTP、7guard、systemd及已安装执行器检查均通过。新审阅SHA256 `367b70130c738c64baa8a2991e06df9442f1720ed8ce3b825d4a60b356272f40`，新checker SHA `e8490ef6ce101cf2bee7545dc41d3497e6de5fa5bedbc23f0870175eb68b0dae`。旧shared监督模块字节未改，连续86400秒及所有退出门槛不弱化。

## 原始证据

本机data/run中保留原Linux同名STATE、Native-running-proof、soak、cutover-proof、install-proof、deadline合同、v14审阅、150监督XML和50工作流XML、网站只GET负载记录。新增只读汇总 `grid-v14-final-start-review.json`、`grid-v14-public-access-check.json`。原v13 STATE/终局Native/原finalizer/原失败cutover/containment也已复制。

后续先查这些原身份及实际进程/Native/账本，不再次运行dispatcher或重POST交易。上游CLOSE/1000原因仍未唯一确认，仍需真实持仓下的持续运行和恢复观察；未完成的时间不能记为成功。

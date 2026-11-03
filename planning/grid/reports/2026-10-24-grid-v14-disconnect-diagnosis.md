# v14断线与恢复超时定位

结论：已复现本机额度调度造成的分钟级恢复等待，并将第四次退出定位到恢复路径超过原300秒监督期限。原始连接为何被对端关闭，现有证据还不足以唯一确定，不能说全部根因已解决。本次未修改生产源码、未部署/重启服务、未重开资金验证、未恢复AI跟进。

## 线上时间与状态

原策略g-8678b5d46b724281，原请求ordinary-observe-linux-20261024-v14。CLOSED/STOPPED/e10，原cleanup 3a9ded66-2cae-4510-89ee-8da016428c56 succeeded/remaining0；不重复提交。旧套利PID1223049及网格runtime2292364未改变。

journal原始账户流记录：北京时间03:37:46.566、06:26:16.722、09:18:48.739、12:12:02.250，全为收到CLOSE/1000，generation1至4。相邻约2小时49–53分钟，不符合简单的每60秒静默断线模式；未见本轮HL_STREAM_HEARTBEAT_TIMEOUT或订阅错误日志。不能仅凭1000断言是服务端固定寿命、负载均衡或网络设备问题。

以SQLite事件received_ts拆分真实耗时（与监督采样耗时不同，二者不可混用）：

| 次数 | 冻结至首次核对完成 | 此后至重建完成 | 总耗时 |
|---|---:|---:|---:|
| 1 | 60.815秒 | 182.676秒 | 243.490秒 |
| 2 | 60.867秒 | 182.552秒 | 243.419秒 |
| 3 | 61.360秒 | 182.325秒 | 243.685秒 |
| 4 | 120.233秒 | 182.965秒 | 303.198秒 |

第四次5548在12:12:02.450977入账，5549于12:14:02.683499首次核对完成，5552于12:17:05.648802成功恢复e9；监督末样本12:17:06.831时距自身首次识别12:12:05.306已301.525秒。监督代码先核300秒，再接受恢复成功，故正确执行SUPERVISION_RECOVERY_TIMEOUT。它不是“永未恢复”或“重建又报最小名义金额错误”；这轮恢复报告oktrue/gaps[]/drift[]，未出现recovery_rebuild_failed。

实际签名订单SENT→ACCEPTED多为约0.7–1.3秒，但不同组订单发送之间接近60秒。第四轮恢复旧e7订单的终局更新时间包括12:12:59.899和12:13:03–05；新e9订单SENT分组为12:15:03、12:16:02–04、12:17:02–04。这显示瓶颈大部分在发送前与恢复准入，不能把整个303秒都叫交易所成交响应慢。

## 可复现的本机额度调度问题

线上8个关键源码SHA与本机一致，保存在grid-v14-cancellation-diagnosis.json。实际运行进程diagnostics读取预算limit900/window60/last_rate_limit=null；该进程自本轮启动未重启，last_rate_limit不会正常清零。这是没有记录到429冷却的证据；不是其他同IP程序完全没被限流的证明。

代码链：

- hl_rate.ReadBudget本机上限900/分钟，预留350，普通提交可用550。该值较[官方1200/IP/分钟及userRole60/其他info20/orderStatus2](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)保守；不能直接改为更大而忽略同IP旧套利。
- AccountExecutor.cancel_intent逐单申请200，再各自做新鲜授权校验、撤单和Native状态查询。named-agent每单读取userRole两次60+60、extraAgents20、orderStatus2，合计142；5单710。
- StreamRecoveryScheduler撤旧单后，GridRuntime.recover还要申请350的整段额度；710+350>900，即使后续实耗较小，也必须先等旧窗口释放。账户多市场路径会先做账户级核对，再进入单市场核对/激活；本轮每次确实留下两份成功recovery_checked。
- AccountExecutor._submission_reads为每笔普通订单申请350。Native参数风控、手续费/行情/账户快照、授权仍逐单新鲜检查；固定大额准入进一步使重建在分钟边界分组。RISK_EVENTS_PENDING还会触发已知未发送的重新准备，不能省略真实事件入账或把未知请求直接重发。

用原生产ReadBudget、check_authorization、HyperliquidGridVenue.cancel及离线Native格式回包，无网络、真实密钥、live DB：

1. 初始额度空：5次撤单查询共710，恢复350准入在虚拟第60秒才通过。
2. 初始额度800场景：撤单第60秒才开始，恢复准入第120秒通过。
3. 这两个场景复现线上60/120秒节奏；历史每次冻结时的预算明细没有保存，800是场景值，绝不是补造的第四次实测占用。
4. 另用真实生产executor+risk路径及原Native格式测试替身，单次普通订单实耗264。两笔总528<550，但固定350准入使第二笔多等60秒。仅在同一个离线场景把准入改为264，两笔零等待且风控、授权、逐单签名路径保留。该264只适用于该disabled-account测试向量，**不是本轮unified账户的实测成本、不是可直接上线的通用新常量**。

两个诊断脚本均在本机及真实Linux独立进程执行通过；没有真实网络/金融操作。它们复现调度放大机制，没有声称逐请求完整复原v14所有历史等待。

## 原始断线仍有证据缺口

实际账户流已每2秒发送JSON ping，并有接收超时检查。[官方心跳说明](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats)规定无消息60秒可能关闭连接；现有约3小时现象不能直接归于少发ping。

此前原独立只读探针（grid-linux-readonly-stream-probe-first-close-evidence.json，历史证据，非此次新运行）无signer/无订单API/金融0，连接10556.007秒也收到CLOSE1000，且正常收到pong。说明实盘签名/网格重建不是该关闭现象的必要条件。仍不能据此唯一认定Hyperliquid故障：探针沿用相同订阅程序与网络路径。

原诊断只记协议关闭码，没有原始close reason、精确末次心跳往返、连接地址及对应系统网络事件；这些历史数据不能事后恢复。本次未新开需数小时的探针或定时任务。

## 下一步修复应约束的范围

1. 优先拆分“撤单—核对—重建”的额度准入和安全观测阶段；按可证明的实际读取计划准入，减少重复重型读取与不必要的整段预留等待。保持每个动作的新鲜授权、逐单所有权、未知请求查询、历史/仓位对账、账本和原生保护。
2. 若批量撤单，必须保留各原client_id的发送/未知/终局状态及新的epoch/用户控制检查，不能只因批量ACK成功就当全部终局。
3. 加入有界、脱敏的分阶段耗时、额度等待/占用、连接年龄/心跳统计和关闭原因分类，之后才能验证外部关闭来源及修复后的实际持仓恢复速度。
4. 保留900/IP本机上限及关键保护预留、30秒Native保护、原300秒监督期限、25小时独立退出及连续86400秒门槛；不通过加长超时、扩大资金或拼接窗口掩盖问题。
5. 修复后先离线/真实Linux验证不同账户模式、正常/高占用/事件变化/持仓恢复，再按新身份另启实盘；本次仅定位，未执行这些部署或资金步骤。

## 证据

- data/run/grid-v14-diagnosis-raw.json：原事件、dispatch、日志及状态。
- data/run/grid-v14-cancellation-diagnosis.json：撤单终局时间与源SHA。
- data/run/grid-v14-runtime-diagnostic-read.json：原进程任务与预算状态。
- data/run/grid-v14-recovery-phase-timeline.json：四次阶段耗时。
- data/run/grid-v14-offline-budget-replay.json / grid-v14-offline-executor-profile.json：离线复现。
- data/run/grid-v14-linux-offline-diagnosis.json：相同诊断在真实Linux的执行结果。
- planning/grid/diagnostics/v14-recovery-budget-replay.py / v14-executor-budget-profile.py：可重跑的完全离线脚本。

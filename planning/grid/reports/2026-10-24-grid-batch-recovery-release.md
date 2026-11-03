# 恢复调度修复及Linux上线记录

## 修复与边界

本次修改7个生产文件：hyperliquid.py、executor.py、hl_rate.py、lifecycle.py、rpc.py、runtime.py、stream_recovery.py。另新增9个公开测试用例（2个测试模块）。不改旧套利服务/目录、账户凭据或资金上限；不改变900/分钟总额度、350关键预留、300秒监督期限、30秒Native保护、25小时退出、连续86400秒门槛。

普通已确认订单按同一网格/币种/代次成批撤销（最多20），共享一次新鲜授权和签名；所有原client_id先逐条记录CANCEL_REQUESTED，Native响应后逐张查询终局；ACK不当终局证明，异常/超时不盲重放批次。UNKNOWN及已有撤单不确定状态仍走原query-first路径，原生全仓止损始终保留到明确平仓收尾。单个冻结币种减少重复完整恢复核对，多币种仍全部核对完成才允许重建。所有权、填单对账、仓位风控及用户更新代次检查保留。

补充本机预算等待次数/累计等待/最后等待，CLOSE连接年龄、最近接收与pong间隔、ping/pong数量及静态关闭原因分类；不记录对端正文或凭据。记录撤单、核对、重建和总恢复耗时。累计等待可来自并发任务，不能直接当成单次恢复墙钟时间。

实际旧轮次连接关闭原因仍未唯一确定，不能说CLOSE1000已根治。修复后的快速恢复仍需真实持仓运行验证。

## 实际测试

- Linux完整1647项全部通过、0跳过。3个不重叠组件为1238网格、394其他回归及15独立审阅回归；合并XML保留每一测试结果，唯一用例数1647。
- Mac1645通过、2平台跳过。
- 新v15有限监督150项、启动/版本工作流50项、网站HTTP6项、诊断门槛7项、systemd及实际安装执行器优先级均通过。
- 补充离线测试：真实绑定的全仓Native stop在批量撤普通单后仍OPEN，保护lease未改，本机及Linux各1项通过。
- 生产executor+risk、原Native格式替身与真实额度调度的持仓5单复演，恢复虚拟等待120秒，高占用场景亦低于原300秒；这是离线复演，不冒充本轮实际Linux持仓故障或新实盘恢复证据。

## 包及安装

327文件包：data/grid-code-checkpoints/grid-code-20261024-batch-recovery-v1.tar.gz，SHA256 **59d3753bacb37b3ebaaf431eb47c80c1678873f174858039343f0366ec722559**。Linux完整合并报告grid-batch-recovery-linux-full.xml，SHA256 **bcd43e52492891c52318b27344d58a523e6a3357928c2482ad943874a1ec8084**。

原v14先完成fresh11DEX空仓/无挂单、备份、清实盘grant及GRID0、禁用仅v14的退出timer。之后安装时再次核对空仓，备份、停止独立网格writer后再次Native空仓，再只复制7生产文件+2测试+manifest；有可恢复回滚副本。原安装proof installedtrue/finishedtrue，legacy_unchangedtrue。安装后版本仍qualifiedfalse，66公共源SHA，从v35基线起明确审阅13处差异。

旧套利PID1223049保持不变。只切换独立网格runtime/watchdog/web；没有启动Macwriter，没有恢复AI定时跟进。

## 新v15有限实盘

本次原cutover唯一身份ordinary-observe-linux-20261024-v15；review SHA256 **60db2506600cdf3b8871eb28c125af8119da18755cb7394ec6f7d0f3c92001ae**。3份fresh历史Native审计、formal bundle、Linux preflight及独立GRID1安装已通过；原start只提交一次。

当前策略g-369b1ba6c19d4b2c，原start4eb7e575-4c5f-42ff-8c5f-12073b990256已succeeded，RUNNING/ACTIVE/e1。40USDC总资金含20预留，ETHlong/cross3/GTC/5格，gross150，原有限期限不延长。严格观察起点北京时间2026-10-24 18:18:18.433；这不是已通过24小时。

v15最终未通过独立Native启动核验。参考价恰在网格线，最后一格2365.4在整个原运行核验窗内仍是未触发GTC条件（PLANNED/client_idnull/无SENT/armedfalse），原检查器按既有600秒窗口报NO_STABLE_ORDINARY_RUNNING_WINDOW，没有放宽门槛或强行补单。

原cleanup **4e1d956b-553d-4812-b70f-8463f138d5dc succeeded/remaining0**，CLOSED/e2；原终局Native审计和备份均0。controller稍后SUPERVISION_SAFETY_EVIDENCE_FAILED是同次收尾后的记录，不据此虚构独立服务故障。v15净 **-0.01961 USDC**（价格损益-.00088/手续费.01873/资金费0/grid_pair0）。随后fresh11DEX空仓/无单+备份，清v15 grant/GRID0，禁用仅v15 timer；旧套利PID1223049未动。历史失败不复用为新观察。

### 新v16参考价居中

仅新有限controller选择lower=mark-50、upper=lower+100，使原参考价落在20USDC格子的内部，距最近网格线约10USDC；避免初始参考价本身成为待触发买入阈值。保持100USDC范围、5格、40capital/20reserve、cross3、150敞口、30秒保护、原600秒启动与600秒稳定核验、25小时退出和86400秒门槛。市场较大变动仍可阻止核验，不能保证成功。

新增明确居中断言；Linux监督151项（原150+1）/workflow50/HTTP6/guards7/systemd/installed executor全部通过；本机新controller/deadline及私有工作流96项通过。review SHA **7c39ee16768aca88c2cc9225fd391380213271dbf147e4db8ca81563454cffa3**，12private源码绑定；core未再改或重装，仍是1647实际Linux全过版本。

原请求 **ordinary-observe-linux-20261024-v16** 仅提交一次。实际策略 **g-75c77a49beb44fd3**，原start **5dd562e4-343c-46d2-9992-08e372dc7eb1 succeeded**。新cutover三个阶段fresh formal/installer/Native-running全0、finishedtrue；当前RUNNING/ACTIVE/e1，无待发或结果未知订单。

独立Native启动证明：真实 **0.0088 ETH/cross3**；**6张working orders（5网格+全仓Native STOP OPEN）**，存在真实非reduce-only入口，原订单身份/全部参数及SQL前后相同均通过，总敞口 **52.151880 USDC≤150**。本次sampling_attempts=1，未触发在线重采样，不把未确认样本算成功。未更改原Native断言或正式门槛。

**严格观察起点北京时间2026-10-24 18:40:05.516；最早24小时2026-10-25 18:40:05.516；建议用户10月25日19:00后检查**。独立25小时退出截止2026-10-25 19:33:02.799，原合同SHA256 **f7fd245fd6479460cfe3d628092dc1524c56ae8759c4f81d8e8bb52d330a78db**，原timer active/waiting，不重装延长。

18:41:54最终只读复核：严格最长105.757秒、issues={}、ordinary24false；公共66源SHA/12privateSHA逐个匹配；runtime2686694/watchdog2686696同身份健康新鲜；旧套利1223049未改动/未重启。HTTP匿名401、认证page200/metrics200/real_dispatchtrue/detail200-RUNNING-仓位.0088，原登录未改。此次未运行额外连续UI负载，不把单次HTTP成功冒充负载验收；此前核心HTTP/优先级回归已通过。

入口：https://taoli.coinfetcher.xyz/grid/#/grid/g-75c77a49beb44fd3 ，已请求app打开（queued）。服务器原controller/observer/finalizer/期限自行运行，AI自动跟进和goal继续paused，无Mac网格writer。本机只读watch52565已terminal0。

**本次是修复已部署、新有限实盘独立启动核验成功；仍需真实持仓断线恢复及24小时观察/终局审计，不能说正式验收或多交易所产品整体完成。**任何断线/代次改变依旧永久使当前严格窗口失效，不能拼接或放宽；上游周期性CLOSE1000根因仍未唯一确认，新脱敏诊断已上线留证。

## 证据

data/run/grid-batch-recovery-{build,install-proof,version-proof}.json；三个Linux组件及合并XML；grid-batch-retained-native-linux.xml；grid-recovery-supervision-v15-install-proof.json/XML；grid-v15-workflow-preparation-proof.json；grid-v15-cutover-dispatch-{attempt,proof}.json；grid-v15-latest-progress.json；grid-v15-interim-native-read.json；grid-v15-startup-diagnostics.json；grid-v15-public-access-check.json。原v14失败及收尾证据保留，不复用旧观察时间。

新v16证据：data/run/grid-ordinary-live-linux-v16.json、grid-ordinary-live-linux-v16-native-running-proof.json、grid-ordinary-live-linux-v16-soak.json、grid-ordinary-linux-v16-cutover-proof.json、grid-ordinary-linux-v16-install-proof.json、grid-ordinary-linux-deadline-v16-contract.json、grid-recovery-supervision-v16-install-proof.json/XML、grid-v16-workflow-linux.xml、grid-linux-recovery-supervision-review-v16.json、grid-v16-final-start-review.json、grid-v16-public-access-check.json。

# v14实盘运行检查：自动退出，连续24小时未通过

核查：北京时间2026-10-24 16:46–16:48。原策略g-8678b5d46b724281；原请求ordinary-observe-linux-20261024-v14。本次仅只读检查，金融操作0、服务变更0，无新轮次或AI定时跟进。

## 状态及时间

最终CLOSED/STOPPED/e10；活动网格0，4FILLED/26CANCELED，账本残仓空。原start afcf6d7d-2b8f-4f50-8cbd-748369ee2a78 succeeded，原cleanup 3a9ded66-2cae-4510-89ee-8da016428c56 succeeded/remaining0。STATE中的初始响应running属于旧回执，当前状态以原SQL operation为准，不能重新提交。

观察00:49:13起，12:19:03平仓完成，约11小时30分钟包含恢复。严格最长10144.986秒（2小时49分04.986秒）；ordinary_24h_observed=false，不能拼接窗口算成功。

四次stream_recovery_required。前三次监督确认恢复242.408/246.816/241.913秒。第四次12:12:02 Native CLOSE1000 generation4；事件5548 received 04:12:02.450977Z至5552恢复成功04:17:05.648802Z约303.198秒。监督始于04:12:05.306Z，末样本04:17:06.831Z已301.525秒。虽最终引擎恢复RUNNING/e9，监督五分钟期限已过，原controller SUPERVISION_RECOVERY_TIMEOUT执行退出。不能描述成永未恢复，也不能延长期限称24h通过。

## 资金与核验

本次fresh Hyperliquid所有11DEX均仓位0、挂单0。4笔原终局成交重新匹配当前Native历史tid/oid及sz/px/fee/side/coin/cloid/time；12条资金费按时间及反向成本符号逐条匹配。原全仓止损reduceOnlyCanceled与全平时间一致，无残留挂单。

净损益-0.441369USDC，40USDC收益率-1.1034225%；已实现价格损益-0.3999，手续费0.040072，资金费净支出0.001397，未实现0。grid_pair_qty/net均0；三次买入及最终全平共4笔成交，未完成网格获利闭环，不把FIFO平仓分配叫网格收益。

原finalizer Native0/backup0/finishedtrue；备份grid-20261024T041910Z-54b32461本次integrity_check=ok且原策略CLOSED。

## 服务及下一步

认证网站页面及详情HTTP200，CLOSED/仓位0/损益与原终局证明一致。runtime2292364、watchdog2292366仍active。旧套利entropy-console PID1223049保持不变，本次未修改或重启旧套利。

原v14deadline timer仍waiting，期限2026-10-25 01:42:10.955，policy仍1个原grant，不代表策略仍交易。本次只读未修改原期限或政策。若再新轮，先按原流程contain并检查旧grant/期限，不复用原create/start/cleanup。AI跟进保持paused。

待修：周期性CLOSE1000根因仍未唯一确认；恢复常耗约4分钟，第四次跨5分钟监督期限，需定位恢复耗时及调度/接口额度。详情保留RISK_API_BUDGET_WAIT，不能单凭它认定超时根因。未修改代码或降低验收门槛。

证据：data/run/grid-v14-user-audit.json、grid-v14-recovery-status-evidence.json、grid-v14-status-public-check.json、grid-v14-latest-progress.json。服务器原证明保留。

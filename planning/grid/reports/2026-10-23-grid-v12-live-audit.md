# v12 实盘检查：24小时验收未通过

检查时间：北京时间 2026-10-23 22:58:40。本次仅只读核验原轮次，未下单、未重复平仓、未部署、未重启服务、未重新启动实盘或AI自动跟进。

## 结论及当前资金状态

原轮次 `g-9b3b7681eded4dfa / ordinary-observe-linux-20261023-v12` 已 CLOSED/STOPPED/e6，无活动策略。原 start `74ba9c1b-9397-419b-9b9a-8945c508999e` 成功；原 cleanup `b4aa062a-9af4-4808-b84a-c887badc6a4d` 在数据库内成功，remaining=0。controller中的running回执只是提交时快照，不能覆盖数据库终态。

观察从 2026-10-23 02:00:02 至 2026-10-23 07:54:32 结束，约5小时54分30秒，包含恢复时间。严格最长连续正常运行 **10532.138秒（2小时55分32秒）**，不足86400秒。ordinary_24h_observed=false；观察存在 GRID_EPOCH_CHANGED/GRID_NOT_RUNNING，正式验收未通过。不拼接断线前后窗口，不以网站或服务在线时间替代策略连续运行。

本次新鲜交易所查询（2026-10-23 22:58:38）逐一核验11个永续DEX，均无非零仓位、无挂单；账本无剩余库存，意图终态4 FILLED/11 CANCELED，无UNKNOWN。成交5笔逐一与原终局证明核对数量、价格、手续费、方向、时间和订单身份；资金费6笔按时间和ETH市场逐一核对金额及收支符号。原finalizer已native-audit=0、backup=0、finished=true，原终局证明SHA256 `ac306eadca3fe0159f1a8213097543331d27f43b56729986ac1c1c7bfd60ecc1`。

## 断线及恢复证据

- 北京时间10月23日04:52:59，generation1收到WebSocket CLOSE/1000，事件5507进入e2恢复。
- 原生持仓对账无drift/gaps，第一次恢复在04:57:01完成，约242.407秒，进入e3并重建挂单。05:16:33有真实网格卖出成交。
- 07:52:39，generation2再次收到CLOSE/1000，事件5513进入e4恢复。5514/5515的完整账本/仓位对账均ok；随后进入e5 NEEDS_ATTENTION/PAUSED，未观察到成功重建或新e5订单。
- 原supervisor以SUPERVISION_GRID_STOPPED结束观察，07:53:46原cleanup仅提交一次，07:54:32正常平仓完成。

现有证据能定位到第二次断线后的重新激活/重建阶段，但不足以唯一确认上游断线原因或重建失败的具体异常。源码 `_recover` 在重建异常后只转换RECOVERY_REBUILD_UNCONFIRMED；epoch已变时 `_finish` 又可能直接返回，导致该失败未形成持久化stream_recovery_attempt事件。第二次没有保存activate预览，不能仅据此断言是余额、预算、行情或杠杆错误。后续应保留真实异常与新旧epoch，再修复和独立重验，不通过放宽连续24小时门槛绕过。

## 损益与备份

账本FIFO净收益 **+0.109088 USDC**，含手续费 **0.022036 USDC**、资金费净额 **-0.001526 USDC**；以40USDC分配金额计ROI 0.27272%。其中账本网格配对净收益 0.0717914461538461538461538461538461538461538461538462 USDC；总净收益还包含初始仓位及退出，不能将总数全部称为网格套利收益。

交易所closedPnl合计0.132704，账本FIFO gross_realized=0.13265，存在0.000054USDC的成本/舍入口径差。本次逐笔成交量价费及资金费完全核对，展示净收益明确使用账本FIFO口径，不宣称两种已实现盈亏数值精确相等。

原备份 `/root/code/entropy-grid/data/grid-backups/grid-20261022T235436Z-5e778e25` last_seq=5518。只读重新打开备份数据库，integrity_check=ok，原策略状态CLOSED。没有解密或输出密钥。

## 服务及范围

本次66项公有源码与12项private源码SHA均与原安装/审阅记录一致。runtime PID 1802603，外部watchdog健康且同instance；旧套利PID 1223049保持运行，本次未更新或重启任何服务。无本机grid writer。

原v12的25小时期限仍active/waiting，授权grant仍1条且不新增；当前无活动策略。未更换或重装原期限，未复用旧轮次。本次不改变生产开关或授权策略，不把空仓误写为持仓故障通过，不把阶段核查写成整个多交易所产品完成。

本机证据：`data/run/grid-v12-user-audit.json`、`grid-v12-readonly-diagnosis.json`、`grid-v12-activation-previews.json`、`grid-v12-source-and-health-audit.json`、`grid-v12-user-audit-summary.json`。原Linux证据仍在独立目录 `/root/code/entropy-grid/data/run/`。

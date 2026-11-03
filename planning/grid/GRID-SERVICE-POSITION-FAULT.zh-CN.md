# 第八轮：真实持仓进程挂起

2026-10-16。`g-a8486ece8deb43a9` 使用 ETH、做多立即建仓、GTC、2格、40 USDC 总投入/20预留、服务保护敞口150/时限30秒。新数量算法选每格 .0109 ETH。真实 .0109 ETH@2443.9 入场，普通 maker 买入 .0109@2441.9 后仍正常运行，未重现上一轮饱和预算的小额亏损暂停。没有完成真实双向 refill 或归零后的原生重建，因此不取得循环资格。

## 实际故障与原生核对

在 RUNNING、有 .0218 ETH 持仓、无增加风险挂单/未确定请求、只有本系统减仓挂单且整仓止损576069347230确认OPEN的窗口，实际 SIGSTOP 执行进程36810，ps确认T。原生实际成交时间重建进一步确认挂起时数量确为 .0218 ETH，不仅依赖挂起前的持仓快照。

独立看护13846保持运行，约6.269877秒启动替代执行进程38526。替代进程在加载账户前消费持久干预标记，记录目标实例和受影响网格，推进 PROTECTION_SERVICE_WATCHDOG_RECOVERY 退出。约74.455618秒后接口恢复。真实减仓成交在挂起后132.684秒完成，最终CLOSED/释放预留在190.233秒确认。

**本次脚本设置的180秒最终确认门槛未通过**：原始 fault proof 的 passed=false / closeout_not_confirmed_within_180s 保留。随后实际自动收尾完成，不改写成“在180秒内通过”，也不修改原脚本的失败结果。原runner连接被进程替换断开，120秒观察内未等到关闭，finished=false 也保留；后续独立最终证明记录实际CLOSED。

三笔 Native OID/TID/客户端标识/方向/数量/价格/费用/maker标识均与账本匹配。净成本 .050838 USDC，其中费用 .039938；最终11个永续DEX零仓零单，USDC199.28407。37 CLOSED/86成交/173事件，未终局/UNKNOWN/预留0。没有用户手动接管，也没有新增风险订单。

## 由记录定位的后续修复

本轮执行报价已经使用执行预留，但最后的 open_orders 与 account_snapshot 仍走普通查询。诊断实际显示：总额度只用533/900，最终20权重挂单核对因公共上限550而等待。该等待发生在真实减仓成交之后，是最终确认拖长的直接证据。

新增 critical_open_orders(20)，结束检查使用执行预留。最终数量检查使用严格的轻量原生 position_snapshot(2)，核对账户、原生时效、目标数量和本地库存；保留结束前及每次IOC后的完整归属/历史/资金费对账，新增风险的全账户核对不变。最终检查不再重复获取用于开仓的可用保证金；错误账户、非零数量或挂单不净仍不释放预算。

相关74项通过。完整检查与部署见 grid-final-close-position-full-tests.log 和 grid-final-close-position-deploy-proof.json。源码修复不代表本轮故障重新通过；下一轮须复验最终确认时长，同时继续真实归零重建与多个补单周期。

证据：grid-service-position-fault-v2.json（原始失败）、grid-service-position-fault-v2-final-proof.json（最终132.684/190.233秒事实）、grid-service-native-v8-proof.json（独立原生核对）、grid-service-native-v8-native-flight.jsonl、grid-service-native-v8-diagnostics.jsonl、grid-service-native-v8-health.jsonl。服务模式公开资格仍关闭。

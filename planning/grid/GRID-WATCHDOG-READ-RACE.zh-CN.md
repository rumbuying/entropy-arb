# 第五轮：看护健康读取竞争

2026-10-15。第五轮`g-ab2523c6a11e4cf7`已CLOSED，没有获得新模式资格。初始买入.005ETH@2375.6，随后因PROTECTION_SERVICE_WATCHDOG_UNAVAILABLE退出，卖出.005ETH@2370.4；两笔Native OID/TID/客户端标识/方向/量/价/费逐笔一致。手续费.010678 USDC、净收益-.036678 USDC，余额199.378621 USDC。独立11个永续DEX最终零仓零单；34 CLOSED/80成交/161事件，未终局/UNKNOWN/预留0。

14:21:46.554Z的operator_trial_refused记录了guardian healthy=false/WATCHDOG_UNAVAILABLE，随后持久退出。独立250ms采样1381次均为healthy、同实例，执行心跳最大1070.238ms、看护检查最大1198.470ms，未见持续超时。采样不能排除采样间的短暂变化；旧拒绝记录也没有具体reason，因此不能据此断言唯一根因。

可确定复现的代码问题是取时顺序：guardian_health先记录now，再读取另一进程可能刚更新的watchdog状态，新的checked_monotonic可能略晚于旧now，被误判为未来时间。现在先读完两个原子文件，再取monotonic时间；保留3秒过期阈值、真正未来时间拒绝、身份/权限/重启预算/冷却检查。RuntimeWatchdog.tick也改为先查询受管进程、再读最新心跳、最后取时，避免慢进程查询使旧心跳过时。

新增两个竞争测试分别在读取状态或查询受管进程期间发布新时间戳，确认健康状态不会误拒；真正未来时间仍拒绝。拒绝诊断新增reason和必要的时间龄数字，不暴露路径、凭证或私钥。LiveRelease.permits_config也增加独立看护健康条件：即使有已验证的服务保护资格缓存，失去看护时也不能给创建预览授予live_eligible；旧原生模式不受此条件影响。

看护/试验/覆盖专项98通过，资格/看护专项55通过；最终完整1249 passed、2 skipped、110 warnings（73.76秒），日志grid-watchdog-read-race-final-full-tests.log。此前1247的额度唤醒及重建进度已在第五轮加载，但该轮未进入service重建，不能把加载等同实盘重建通过。

全部网格结束后备份grid-20261015T143122Z-fc6fc57f（161事件，完整性ok），恢复GRID_REAL_DISPATCH=0、grants=[]、撤下私有试验政策，并重载runtime和独立watchdog以加载各自的修复。最新PID、只读健康检查及源码哈希见grid-watchdog-read-race-deploy-proof.json和RESUME.md。

证据：grid-service-native-v5-proof.json、grid-service-native-v5.json/log、grid-service-native-v5-health.jsonl。持仓SIGSTOP脚本仅等待“RUNNING、非零仓位且无增加风险的挂单”窗口，网格先退出；日志grid-service-position-fault-v1.log为campaign_closed_without_fault，未发出SIGSTOP，也没有持仓故障成功证明。

继续条件：新政策/新请求复验稳定补单、真实归零重建；必要时将只需要仓位一致性的保护检查改为轻量原生仓位读取，但所有新增风险仍必须完整核对全账户资金/归属/历史。之后完成持仓故障与独立原生证据审核。普通实盘继续关闭，不能复用历史原生模式或旧试验资格。

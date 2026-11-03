# 最新：第30已启动，修复已部署（2026-10-17）

用户要求加快，CLI/API不足200USDC独立账户授权仍有效；无CUA金融操作，无旧套利互斥变更。目标active，普通grants=[]。

- 第29 g-64c450b400694cdc 已 CLOSED/STOPPED/e4。start与pause成功，resume失败 PROTECTION_REPLACEMENT_UNRESOLVED；同一helper正常平仓成功。Native审计2fills/2stops/11DEX零仓零单，net -.003047USDC，余额198.855896。终局备份 data/grid-backups/grid-20261017T103707Z-aacf4c2f seq273。finalizer finished、review拒绝、未安装授权，不能再称29正在恢复。
- 根因改动：ACCEPTED撤单写入CANCEL_REQUESTED；查询确认OPEN/PARTIAL可解析CANCEL_PENDING/REQUESTED；保护单替换最多三轮同一ID先查后撤，仍UNKNOWN仅查不签；verify优先最新保护单，在取消旧单前绑定新证明。未放松Native有效期、费用、仓位/预算或完整上线门槛。
- 当前修复 protection.py/journal.py 已部署到runtime95565；完整1459passed/2skip/110warnings/82.65秒，相关73通过。log data/run/grid-protection-replacement-current-final-tests.log。第29关闭且fresh Native11DEX全零后切换；bootout0/首次bootstrap5且确认未加载后重试0，未双运行。外部看护健康，同instance；67源码hash已记录 grid-service-native-v30-version-proof.json。源码302文件归档 data/grid-code-checkpoints/grid-code-20261017T1047Z-protection-replacement.tar.gz sha b124a29e229853e4af90d560259da80d09c69d43fefa069330b0031b6d5918f9，manifest在全量结束前制作所以内部test状态pending，外部checkpoint proof已记录实际最终1459结果。
- 第30主helper72683已启动；finalizer4569/health95489/Native92348/只读stream取证87358/临时caffeinate17860。新RID service-loop-20261017-v30。GID g-25c06adff0dd434f 已RUNNING/e3，start905941fb-ea36-4940-80c6-fc28e15fed86成功5placed；pause7d8a7e1c-a3c7-4eb7-80e1-971e9e5aa4f5成功5撤净；resume4952d8f6-94c2-42a2-ba06-77e25e98ee20成功5placed/0pending。独立Native核对freshStopOPEN、ETH.0044与账本相等、初始IOCintent1，proof grid-service-native-v30-held-management-proof.json；此proof不代表断线恢复或完整qualification。后续真实状态看grid-service-native-v30.json和DB；不要因尚无gid或RPC等待重启。政策沿用29的18:26:32UTC到期时间，未延长。fresh Native预检 grid-service-native-v30-preflight-source-proof.json已通过（保留原29审计），processes已记录。
- 延续同一第30进程，检查真实start/pause/resume和成交后的补单/重建结果。保持40总20reserve/150敞口30秒/2.6正费用间距/六小时原期限；不重做small20/fault23，不做其他功能。普通入口准备已迁移30，ORDINARY-CUTOVER-AFTER-V30.md/current preflight/ordinary controller全绑定30；14预检+9控制器+6本地虚拟HTTP再次通过，均非实际24h。普通controller未启动，grants仍空，不能安装旧候选。
- 完整历史见 RESUME.history-through-v29-20261017.md。真实闭环>=4双向refill、自然保护重建和带仓pause/resume仍未在同一轮全部通过，普通24h尚未开始，不能标完成。

本轮六小时原观察deadline_ms=1792255983334（UTC 2026-10-17T16:53:03.334000+00:00），不延长。当前等待真实4双向refill+自然Native重建+再入场，不改配置、不重开。

带仓management独立Native已通过；只读trial soak session2426（scope operator-trial，不能算ordinary24h）。主helper72683仍存活等待真实成交。

第30首笔maker买 .0044ETH@2436.6（2026-10-17T11:17:10.751Z，Native OID577478505770/TID927062519409893）已实际成交，因果refill卖 .0044@2439.2 IID6aa0c215-68e1-4235-9aae-fc3b34ac4e69/CID...0197/OID577487114828已Native OPEN/Gtc/reduceOnly确认，买成交OID/TID/CID/price/qty/fee/maker全部独立匹配。proof grid-service-native-v30-first-refill-proof.json。Native接受距源成交 31309ms，独立取证距成交114152ms，两者不同；此时refill未成交，不能计入4个refill门槛，未完整qualification。

第30第二笔maker买 .0044@2434（UTC11:22:05.862）已实际成交；因果refill卖 .0044@2436.6 IIDaab15a3f-4ecd-43b4-a734-3bfed1d336c7/OID577489421604 已独立Native OPEN，证据grid-service-native-v30-second-refill-proof.json；源成交Native数量/价/费/maker匹配，系统继续RUNNING/e3。尚无refill成交，不能把两笔补挂当双向4成交。

第30活动备份 data/grid-backups/grid-20261017T112954Z-4f4d32ae seq278/integrityok；RUNNING/e3，3fills/净.0132ETH（活动副本，不是终局空仓证据）。opaque凭证与2政策逐字节匹配源且owner/0600检查通过，未解密/启动服务；proof grid-service-native-v30-midpoint-backup-proof.json。第29CLOSED备份仍单独保留。

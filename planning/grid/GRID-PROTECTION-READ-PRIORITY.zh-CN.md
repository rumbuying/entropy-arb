# 执行行情查询的额度优先级

后续21:58更新：初始保护核对与混合保护重建的账户读取也接入执行预留350，行情预留22；新增账户并行读取场景确认真实计费62、剩余预留退回、网页继续等待。保护等专项92通过，含后续快照竞争修复的完整回归1243 passed、2 skipped并已空仓部署。最新结果与限制见GRID-OWNED-SNAPSHOT-RACE.zh-CN.md；下文1239为中间版本。

2026-10-15。主网试验第二轮 `g-2601b6817741433d` 暴露等待行情读取的额度优先级问题：执行任务的调用链停在market_snapshot/public_read_sequence，当前已使用534、总额度900，而网页额度上限550；22权重行情读取因此等待，尽管执行预留仍有余量。等待入区间记录出现RANGE_QUERY_TIMEOUT。原生价格曾自然回到区间，触发epoch2/3的重新校验，但随后又离开2341.5–2343.5窄区间，最终未下单、未成交；已用正常stop_flat结束为CLOSED。

修复为明确的critical_market_snapshot：执行任务先申请22权重的执行读取预留，再由原有市场快照读取嵌套继承；metaAndAssetCtxs20+l2Book2只扣一次。等待启动行情、过渡保护guard和重建末尾核对使用该入口；普通网页继续走public额度。总额度、429冷却、超时、行情新鲜度和发单约束不放宽，不引入订单重试。

新增两项真实ReadBudget行为测试：网页额度几乎耗尽后，其读取继续等待；RangeStartScheduler仍能完成RANGE_OUTSIDE观察且零发单；ServiceCoverageManager.guard仍能返回PROTECTION_SERVICE_PENDING。每次实际只增加22权重，网页任务保持未完成。专项76通过，全量1239 passed、2 skipped、110 warnings（75.49秒）。日志grid-protection-priority-tests.log/grid-protection-priority-full-tests.log。

第二轮看护外部采样953次，最大执行心跳龄1038.394ms、看护检查龄1178.691ms，采样无跨实例失配；不据此证明第一轮提前退出的原因。原生再次独立查询11个永续DEX均零仓位/零挂单，余额199.640516 USDC，第二轮无成本；31 CLOSED/72成交/145事件/未终局与UNKNOWN与资金预留0。证据grid-service-native-v2.json、grid-service-native-v2.log、grid-service-health-watch-v2.jsonl、grid-protection-priority-deploy-proof.json。备份grid-20261015T134134Z-f8949a35。

本次修复已空仓部署，不等于混合保护资格。继续使用新请求及独立有限政策进行第三轮真实补单/归零重建验证，普通网站资格仍为空；当前试验是否运行应查RESUME.md及实际进程/数据库。

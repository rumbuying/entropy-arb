# 持续运行只读记录

2026-10-17新增独立CLI；不由当前交易引擎加载，不改变下单、保护或资格规则。启动后每5秒读取一次一致的只读SQLite快照及私有心跳/看护文件，不读取密钥，不连接交易所，不发送交易动作。

正式有限开放后，对实际普通入口创建的网格运行：

```sh
.venv/bin/python -m entropy_arb.grid.soak_observer \
  --db data/grid-runtime.sqlite3 --grid-id <实际普通实盘网格ID> \
  --scope ordinary --heartbeat-file data/run/grid-runtime-heartbeat.json \
  --output data/run/grid-ordinary-soak-observation.json
```

输出必须是新文件，父目录须本人所有且私有。已有记录不覆盖、不累计旧时间；重启观察器从头记录。--once仅试采样，永远不能凭一个样本完成24小时。默认最多运行24小时30秒；关闭、需人工介入或带仓停止会结束，退出码1代表未完成无异常连续观察，退出码0代表本工具记录的连续观察条件满足。输入错误/已有输出退出2。

ordinary要求真实live-grid上下文，operator-trial要求私有试验上下文，核对账户、市场和配置哈希；两者不能混用。本工具不重新授予或证明实时授权，发布仍必须通过原有完整证据审核及运行时授权检查。

记录要求至少86400秒，并同时核对墙上时钟与单调时钟，采样间隔最多15秒；时间跳变、漏采、作用域/配置变化、执行实例或网格epoch变化、非RUNNING状态、看护/心跳失配或过时均保留累计异常。原生保护使用引擎持久确认租约，核对配置/账户/市场/epoch/原始截止时间；补挂中检查原始过渡时限。两次采样之间的持久risk_paused/protection_gap_exit_requested也会被捕获，随后恢复不能抹去异常。

报告completed_without_observed_issues只有连续真实24小时、所有采样均合格且从未记录异常时为true；普通实盘还需ordinary_24h_observation=true。试验模式的记录不能替代普通实盘验收。release_qualified_by_observer和live_dispatch_enabled_by_observer始终false。UNKNOWN数量、成交数量保留供复核，不被擅自解释为无故障。

这是采样运行证据，不是每毫秒连续证明；原生保护来自引擎租约，不等于独立交易所审计。完整上线验收仍需最终Native订单/成交、资金及保护窗口核对。记录工具不会自动止损、重启或开启普通入口。

专项45项通过（观察工具、只读资格审核、看护相关检查），日志data/run/grid-soak-observer-tests.log。测试中的时间模拟只验证计时规则，不能作为实际24小时证据。当前第24轮主网试采样正常，1样本、0秒、completed=false、ordinary=false；持续试验观察已启动。当前交易核心仍是1398项通过的版本，新工具未被运行中引擎加载。

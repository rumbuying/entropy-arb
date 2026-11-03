# 网格 v6 实盘验收结果（2026-10-21，北京时间）

结论：未通过连续24小时验收，运行中独立核验未完成；未启动新一轮交易。

- 网格：g-961b88dff80248a4，原请求 ordinary-observe-linux-20261019-v6。
- 观察开始：2026-10-19 23:16:58.071；最长连续窗口9827.76699996秒，即2小时43分47.767秒。
- 2026-10-20 02:00:49.104记录账户流中断；脱敏服务日志为HL_ACCOUNT_STREAM_INTERRUPTED frame=CLOSE close_code=1000 generation=1。它证明收到关闭帧，不证明唯一上游根因或固定断线周期。
- 既定控制器发现epoch变化后结束失败观察，原cleanup请求ordinary-observe-linux-20261019-v6-cleanup / b737a240-eb12-4441-b4a0-2b610bb429c0成功；02:01:51.216控制器结束。未重新开单、未拼接失败窗口。
- 原独立终局审计和备份退出码均0。此次再做独立Native只读审计：11个DEX范围无仓位、无挂单；15笔本地归属成交逐笔匹配交易所数量、价格、手续费、订单与成交身份。账本净仓位0，当前无未决操作或订单。
- 本轮净收益-0.056565 USDC，含手续费0.066741、资金费净额-0.001704；原始已实现毛收益0.01188。盈利不是本次稳定性认证的判据。
- 本轮23:22:20.088发生零仓自动取消，23:22:22.391原生止损重挂确认，间隔2.303秒，小于原30秒上限；该实际场景通过，不能据此宣称所有故障路径或24h均通过。
- 此前Native-running检查仍为NO_STABLE_ORDINARY_RUNNING_WINDOW，未生成正仓运行期独立证明。终局空仓审计不能替代此证明。
- 验收后执行一次已准备的v6 containment：fresh Native审计、备份、SQL全关闭/无未决检查均通过；仅停用本轮25h timer，清空本轮授权、GRID_REAL_DISPATCH=0，只重启独立网格runtime/watchdog。原合同和全部交易证据保留。
- 当前grid runtime719771、watchdog719773同实例健康；旧套利693437在本次验收收尾前后未变，未修改其目录或服务。66个核心源文件与已测试1550项Linux版本全部一致。
- linux AI自动跟进保持暂停，没有开启新campaign；整体产品未标记完成。

下一步：处理账户流关闭后的安全恢复与持续运行，仍须保留权限、仓位一致性、保护期限、幂等和真正24h验收门槛；由后续任务明确推进，不在此次检查中自动重启测试。

证据：data/run/grid-ordinary-linux-v6-day-review-snapshot.json、grid-ordinary-linux-v6-day-fault-evidence.json、grid-ordinary-linux-v6-day-terminal-review.json、grid-ordinary-linux-v6-failure-containment.json、grid-ordinary-live-linux-v6-containment-audit-proof.json。

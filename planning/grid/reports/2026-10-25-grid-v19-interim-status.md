# v19 状态核查（2026-10-25 08:50:37 北京时间）

用户仅要求查看状态，本次只读，无部署、服务变更、重启、新资金请求或期限延长。

- 原策略 g-490d1620d25b421a / ordinary-observe-linux-20261025-v19 当前 RUNNING/ACTIVE/e7，原 start b43fac3e-d865-4546-b33b-6af1eec8a6f5 succeeded。
- 原观察起点 2026-10-25 01:23:26.068，本次已过 26831.154 秒，约7小时27分钟。3次受监督恢复分别121.082/181.352/125.983秒，累计428.417秒；每次 attempt_ok/reconciliation_ok，均小于原300秒，无当前恢复或stop_reason。恢复耗时不计为正常交易时间。
- 最新独立交易所查询：.0156 ETH/cross3，4张普通网格单+原全仓原生止损，止损OPEN/触发2247。SQL前后稳定，真实仓位精确匹配，全部当前挂单CID/OID一致。
- 用本次交易所仓位估值对应mark 2360.9进行只读账本估值：未实现0.66144，手续费.016275，资金费成本.003093，净盈亏0.642072 USDC。完整网格配对仍0，不能将浮盈称为已实现网格收益。首次detail缓存估值滞后，未用于当前盈亏。
- risk_watch仍RISK_API_BUDGET_WAIT，不宣称无风险/无等待。原runtime/watchdog/controller/finalizer运行、独立期限timer等待，旧套利PID1223049运行未变。
- 严格旧指标 observation_invalidated=true 与soak的GRID_EPOCH_CHANGED/GRID_NOT_RUNNING保留原貌；按用户允许安全恢复的新口径不单独否决。尚未24h，不授予正式资格。最早10月26日01:23:26.068满24h，建议01:30后查原状态及终局；原25h资金截止10月26日02:17:23.844不变。

证据：data/run/grid-v19-current-status.json（新Native及对应mark账本估值）；data/run/grid-v19-latest-progress.json（原请求/实际进程/监督恢复）。AI定时仍暂停，无Mac网格writer。本次本机只读会话2536/47076/13787均terminal0。

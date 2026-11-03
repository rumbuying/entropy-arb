# 断线恢复与验收控制修复

本次按用户“你先修复”处理控制脚本提前平仓问题。代码与 Linux 回归完成，替代脚本已准备在线上；**没有开启新实盘轮次，未恢复 AI 定时跟进，24 小时实盘认证仍未通过**。

## 原因与修正

v6 的直接退出原因已由原日志、事件与控制脚本交叉确认：上游发来 CLOSE/1000，运行时冻结订单并进入 RECOVERING，验收控制脚本发现 epoch 改变后立即调用原 stop_flat 清理，阻断后台恢复。上游关闭连接的具体原因仍未唯一确定，不能声称已消除上游断线。

新 v7 将两个结果分别记录：

- **验收结果**：发生断线，原连续 24 小时观察永久失败。保留原严格 observer，不清空问题、不更换观察器、不拼接时长，不授予实盘资格。
- **运行结果**：只有真实持久化 stream_recovery_required 事件可进入恢复等待；随后必须有同轮对账成功、无漂移、后台恢复成功以及新 epoch 的 RUNNING，才记为恢复成功。恢复期间继续检查运行时、看护和保护证据。

每次恢复从检测时起最多等待 5 分钟，重复重试不能续期；原账户权限、30 秒保护期限、资金上限、控制脚本的原观察截止时间和独立 25 小时清理期限均不改变。风险事件、对账漂移、健康过期、保护失效、未知 epoch 改变或恢复超时，仍进入原一次性清理。用户暂停、保留仓位和上下文变更优先交还控制权。

另修正 observer 初始化文件可能被当作非法报告的竞态：最多等待 10 秒；已生成报告须身份一致且不超过 15 秒。先读取 observer 报告，再读取当前数据库快照，避免异步 observer 比监督快照更新而误判。观察进程退出不会被悄悄替换。

## 验证与线上状态

本机与 Linux 均 **127 项通过，0 失败、0 跳过**。覆盖持仓模拟成交后的真实运行时冻结、撤单、历史对账、重建和数据库事件读取；另验证保护原期限到期仍由引擎停止、恢复超时、用户接管、未知清理响应只查询原请求、时钟/身份/保护/报告异常，以及即便恢复后再运行满一天也不能把失败观察变成成功。上述持仓恢复测试使用隔离的模拟交易所，并非新实盘持仓证明。

实际 Linux 安装证明：`data/run/grid-recovery-supervision-v7-install-proof.json`；测试 XML：`data/run/grid-recovery-supervision-v7-linux.xml`。线上只新增三个未启动的脚本：

- `data/run/grid-ordinary-live-linux-v7.py`
- `data/run/grid-ordinary-linux-deadline-v7.py`
- `data/run/ordinary_supervision.py`

可审阅源位于 `planning/grid/tools/`，相关新增回归位于 `tests/test_grid_ordinary_supervision.py`、`tests/test_grid_ordinary_controller_v7.py`、`tests/test_grid_ordinary_deadline_v7.py`。

原 v6 脚本、证据及失败身份保留。未改动已部署的 66 个核心源码，未重启任何服务。安装前后 grid runtime 为 719771、watchdog 为 719773、旧套利为 1223049，均保持 active 且身份不变。本次数据库核对 active grids / pending operations / unresolved intents 均为 0，grants 为空。没有新的交易所资金操作或新一轮 Native 持仓审计。

## 下一轮仍需完成

v7 新身份 `ordinary-observe-linux-20261021-v7` 未使用，没有 state、授权候选或已启用 timer。缺少新的 v7 审阅候选时，线上实际调用只读 local_guard 已验证拒绝启动。后续新实盘验证需要重新核验资格、独立运行中 Native 正仓/挂单/止损证据以及有限资金与期限，再使用全新身份。原 v6 缺失的运行中独立核验并未被本次离线测试替代。

这次修复解决“验收失败立即打断恢复”的控制问题；上游真实断线下的恢复表现和完整 24 小时认证仍需新一轮实际证据。

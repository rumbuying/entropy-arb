# v19 宽区间实盘已启动并独立核验

北京时间 2026-10-25 01:26 完成当前状态检查。用户本次授权继续启动实盘、扩大价格区间；采用其已确认的“允许受保护断线恢复”验收口径，不以零断线作为唯一上线门槛。

## 当前真实运行

- 策略 `g-490d1620d25b421a`，RID `ordinary-observe-linux-20261025-v19`，RUNNING / ACTIVE / epoch1。
- 原 start 操作 `b43fac3e-d865-4546-b33b-6af1eec8a6f5` succeeded，cutover 三阶段 fresh-formal / installer / native-running 全部返回0。
- 区间 **2266.1–2555.7 USDC**，宽289.6，约参考价格12.5%，接近上一轮固定100宽度的3倍；**4格**，ETH做多、3倍全仓、GTC、40 USDC配额含20 reserve，止损触发价 **2247 USDC**。主要向上扩展，保留原参考价约3%的下方止损，不扩展到止损之下。
- 初始真实持仓 **0.0156 ETH**；独立交易所核验 **4张普通网格单+1张原生全仓止损**，Native持仓与稳定SQL前后库存完全相符，所有活动单CID/OID与账本一致，原止损OPEN/触发2247。
- 原启动正向证明 gross_open_notional **48.2352 USDC≤150**、sampling_attempts1；本次没有把空仓、离线测试或重采样当真实持仓证明。
- 01:26只读复核仍RUNNING/e1，soak issues={}、无恢复/无停止原因。risk_watch保留 `RISK_API_BUDGET_WAIT` 请求配额等待提示；只说明本次真实仓位及挂单对账通过，不据此宣称不存在后续运行风险。
- runtime PID2839364 / watchdog2839366，同instance且健康新鲜；旧套利 PID1223049 未改变，未改动或重启旧套利服务。
- 实际公共网站匿名401、认证页面/metrics/detail均200；production / real_dispatch=true / 当前策略RUNNING，登录方式未改变。未运行额外连续UI负载，不将单次HTTP检查称为负载测试。

网址：https://taoli.coinfetcher.xyz/grid/#/grid/g-490d1620d25b421a

## 观察期限

观察实际起点 **2026-10-25 01:23:26.068 +08:00**。最早满24小时为 **2026-10-26 01:23:26.068 +08:00**；建议01:30后核验原状态及终局。

原独立25小时资金截止 **2026-10-26 02:17:23.844 +08:00**，合同SHA `9e7cb2f314be300f2c1946eb8793c844bde7f9e67a112c05726af5463e0bf13e`。不得因恢复或观察失败延长期限、重置计时或复用失败请求。原服务器controller/observer/finalizer/timer自行运行，AI定时跟进维持暂停。

旧严格 `ordinary_24h_observed` 保持原定义；断线后即使该字段false，也不单独否决新的恢复验收。新验收仍需实际24小时记录、每次受保护恢复≤300秒、账本及Native终局一致、原请求终态和备份完成。本轮尚未达到24小时、未授予正式资格，不代表整个多交易所产品目标完成。

## 本次修复与验证

1. 原v16已平仓但未进入CLOSED，本次通过正常生命周期的新唯一终态核验请求完成收尾，原partial错误保留；原finalizer Native审计及备份随后完成。
2. 过期WS盘口改为一次全新的REST完整观察，两个来源都过期仍拒绝，触及止损仍退出，范围及限流错误不绕过。
3. v17在create之前被真实预览拒绝：5格与扩大区间、最低下单额及原预算留白不能同时满足。没有开仓，没有复用该失败身份；4格真实预览通过，增加了空/无效preview显式拒绝处理。
4. v18曾真实建仓，但01:00:34发单前指纹变化触发整轮暂停。原cleanup `95aeb650-d88c-4b88-b727-8c7e2ee8e387` succeeded/remaining0，CLOSED/e3，11DEX平仓、原审计及备份完成，净损 **0.075094 USDC**。其具体历史变化未留部件诊断，不能唯一断言是资金费。
5. 已复现资金费入账与journal/签名后校验相撞的相同拒绝路径。新逻辑仅在funding表变化、其余语义指纹完全一致时，对证明尚未发送的订单保留原CID并重新收集全部风险观察；不允许用旧permit发送，不影响UNKNOWN/native rejection处理。预算、历史、保护、身份等变化仍按原规则拒绝；未知变化仅记录固定部件名，无敏感值。

当前不可变包327文件，SHA `2d8ae7b4b695fc535c6ad5bc03874ac17a4b7aea69205811400e79f38688b4b5`；安装仅2个运行文件、1个测试及manifest，保留rollback。核心版本66源SHA/相对原v35基线13项差异、qualified=false。

实际Linux新源1652项通过、1项deselected；该唯一性能case与20个console依赖SHA完全未变，复用其此前按原阈值独立通过的1项，组合1653项唯一case成功、0skip。组合XML SHA `7cfd9ee875cde48250d13a0c99725a21fca26cb36078088d105e5feafed804b4`，记录明确含重用依赖SHA及两原XML。此前全量性能首轮失败/单独复核通过的原证据均保留，不冒称1653项在一次新进程全部重跑通过。

本地94项风险/执行/保护回归、本地100项控制器/工作流、Linux155项控制器监督、Linux50项工作流、HTTP6项、诊断守卫7项、实际执行调度及systemd校验通过。上述测试不代替当前源码真实持仓故障、断线恢复及完整24小时终局证据。

## 证据

`data/run/grid-v19-final-start-review.json`、`grid-v19-current-status.json`、`grid-v19-public-access-check.json`、原`grid-ordinary-live-linux-v19-native-running-proof.json`、`grid-ordinary-linux-v19-cutover-proof.json`、`grid-ordinary-linux-deadline-v19-contract.json`。本次只读启动watch已结束，未留下本机资金执行进程。

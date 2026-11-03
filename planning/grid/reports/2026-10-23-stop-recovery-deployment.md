# 第一批保护单修复已部署；v10启动未通过并已安全收尾

本次按用户“修复、推送上线、继续实盘验证”的授权执行。网格独立目录为 `/root/code/entropy-grid`，旧套利目录与服务没有更新或重启。旧套利进程在源安装和新轮启动前后保持1223049。

## 修复与验证

- 原生全仓止损触发后，交易所会将同一订单转为市价子单，`isTrigger=false`、`triggerPx=0`，价格和原始数量也可能改变。适配器分别验证在挂触发单与已触发终态，保留币种、方向、CID/OID、reduce-only、全仓标识及数量上界约束。终态经济累计仅与实际历史成交比较，不据状态回报制造成交。
- 签名和授权等发送前准备有独立8秒限时，超时明确记为未发送。进入网络发送后的异常仍保持UNKNOWN，查原身份，不能重复下单。
- 恢复时保护容量不再重复累加常规库存。容量足够且独立查询仍有效的原生全仓止损可以复用，避免无必要重挂；超出容量仍需原有替换与确认流程。

旧版本已实际复现三个正向用例失败，新版本对应通过。新增20项核心回归及24项运维证据校验。Mac隔离完整测试1607通过、2项平台跳过；实际Linux完整测试1609通过、0跳过，包含原有15项planning review测试。Linux XML SHA256：`11421d698748bd4a8b68aec4a04813e980b21462b94069fa7056705772c35938`。

干净包从之前已部署的归档构建，仅替换上述两个运行时文件并加入三个回归/核验文件，322文件，不混入本地其他套利改动。新归档SHA256：`d9b7cfa66d61a2a56bb29aaae257808daec69718fed5c58dc6074ecdd383599b`。实际安装、66项源文件摘要、Linux报告和版本证明均已绑定；正式资格仍为false。

## 原v9的证据收尾

原策略 `g-fecf202d3d044c32` 已通过正常保护退出流程达到CLOSED；原独立finalizer的Native审计与备份均成功。原失败请求和partial历史保留，没有重放。

已成交止损按真实Native成交、账本数量及手续费逐笔一致的证据记为FILLED，没有新增执行记录。另一笔旧UNKNOWN并非凭unknownOid清除：检查了未截断1181条历史订单，时间覆盖提交前后、完整成交历史、原CID前后查询和两次11DEX空仓无单；结合原安装源码、原10秒提交调用及默认10秒签名期限，证明原包络到期上界1792607651096已过且未被接受。独占GRID0运维写入期间，按既有ABSENT_FINAL状态路径恢复PLANNED后立即使旧epoch过期，没有重发许可、新订单、库存/资金费修改或强制CLOSED。

原资金损益仍是净亏损0.972565 USDC。v9没有通过24小时，空仓与上述收尾不改变其失败结论。

部署首次在只读证据文件超过通用watchdog JSON的64KB限制时停止，尚无服务或账本修改；核对后以独立不可变证据文件恢复同一安装流程。实际安装证明保留首次失败原因和恢复记录。

官方接口与语义参照：[订单类型](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)、[查询接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)、[签名交易期限](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)。成交后字段变化的直接依据为本账户原始Native响应。

## 新轮范围与当前状态

唯一新RID为 `ordinary-observe-linux-20261023-v10`，策略 `g-912a518184e846b5`。启动请求 `9ce8437f-572f-4762-86d1-f2b8e61afa7d` 已提交一次，最终failed/initialize。本轮未到RUNNING、未开始24小时观察，不能用创建时间代替运行证明。

新私有控制流程实际Linux135项通过；另34项工作流、6项网页HTTP、7项诊断守卫及systemd校验通过。新review绑定当前源和实际报告，SHA256：`f20427830cd28f44a579837364692749158c2bcfb2773e31ac91e960fb643ac0`。

配置保持40 USDC总额（含20预留）、ETH long、3倍cross、5格、最高150 USDC敞口、原30秒服务保护期限；下单前安装独立25小时到期退出。新轮须重新观察连续24小时并完成终局审计。上游CLOSE/1000的唯一原因尚未确定，本次不承诺已消除所有断线风险，不拼接失败轮次，不恢复耗token的AI定期跟进。

网站：[已收尾的v10策略](https://taoli.coinfetcher.xyz/grid/#/grid/g-912a518184e846b5)。公网认证页面和metrics均已实测200，匿名401，原登录保持。

主要证据位于 `data/run/`：`grid-stop-recovery-install-proof.json`、`grid-stop-recovery-version-proof.json`、`grid-stop-recovery-linux-complete.xml`、`grid-v10-expired-absence-native-r2.json`、`grid-ordinary-live-linux-v9-finalization.json`、`grid-v10-workflow-preparation-proof.json`。新轮实际Native-running、观察与终局证明应在原进程产出后审阅，不能提前生成通过结论。

## v10实际终局与后续修复

独立Native-running检查在原600秒启动限时内未找到RUNNING窗口。原cleanup `0145f678-755d-4332-b603-bee8e51c30ac` succeeded，策略CLOSED/STOPPED/e2；原finalizer Native审计和备份均0。再次fresh Native11DEX空仓无单及备份后，已清grants、恢复GRID0、停用仅本轮timer并保留原合同，旧套利1223049不变。净损益为 **-0.246006 USDC**（实现-0.2178、手续费0.028206，资金费0），没有普通网格配对循环。

新增阻塞已定位至普通提交的350读取预留：实际等待时rolling used376/public ceiling550，无429、cooldown或锁占用，页面行情等小读取反复先行，使大预留条件不能满足。修复增加普通执行读取优先调度，在原public550内暂缓新背景读取；已进行的读取可以完成，Native保护/紧急平仓的critical900/保留350不变。原故障已离线复现，新8项调度回归及真实执行器离线检查通过。新323文件干净包SHA `3a3cbdd77bea8994672655b21feedd3fda0200d6d75ac1d9161ce47939bcbd10`，Mac完整1615通过/2平台跳过；原Linux1617全量和随后GRID0安装链正在独立运行。新v11尚未启动，需待实际部署与审阅成功，使用全新身份，不能复用本轮。

# v12断线重建修复及线上部署

完成核验时间：北京时间 2026-10-23 23:50:20。已部署到独立目录 `/root/code/entropy-grid`；旧套利目录、代码、服务未改动或重启。修复完成，**未创建新资金轮次，未重启实盘，普通实盘24小时验收仍未通过**。

## 已复现的问题和修复

使用原v12的真实执行、FIFO配对、初始成交offset和原始档位，在离线数据库内重建最终平仓前的状态。原始建仓0.0088 ETH被拆为0.0065及0.0023两笔同时间戳成交。FIFO成本批次按UUID排序，先消耗0.0023，再消耗0.0021。旧逻辑逐笔推进各自offset，把剩余完整0.0044初始库存拆成2419.6价0.0023和2439.6价0.0021；两单金额各不足10 USDC，真实Hyperliquid订单格式校验器返回HL_MIN_NOTIONAL。另有普通网格库存0.0044，合计持仓0.0088不变。

现在按**同一个原始IOC的全部真实成交及剩余总量**推进保存的初始退出档位：剩余初始退出档位为2439.6价0.0044，普通库存仍为2399.6价0.0044。相同原生校验器通过，持仓总量仍0.0088。没有修改金融FIFO成本批次、成交价格、配对记录、费用或收益；没有合并不同价格或不同原始IOC。每笔包括已消耗成本批次的成交映射、连续offset、真实累计成交及库存上界都必须完整核对。真实不足最小金额的残额仍拒绝重建。

原v12现场异常没有落盘，因此离线证明是**复现并修复一个与现场一致的明确缺陷**，不冒称补回了现场异常或唯一排除了其他原因。上游CLOSE/1000原因仍未唯一确认。

## 异常记录修复

`runtime.py`对重建失败单独记录 recovery_rebuild_failed，保存原恢复代次、激活代次、实际状态及安全错误代码；可保留RESTING_PLAN_INVALID及其HL_MIN_NOTIONAL原因，不记录异常正文、签名或密钥。

`stream_recovery.py`在代次变化或用户新控制已接管时也保留尝试结果，标记state_update_applied=false/retry=false，不覆盖新暂停、停止，不为过期任务安排重试。后续成功清理不覆盖原失败事件。

本次生产源码仅4项：inventory_layout.py、runtime.py、recovery.py、stream_recovery.py。测试新增9项退出碎片/完整归属/真实残额用例，另增加4项重建失败记录及控制并发用例。没有改变止损、读预算、资金限额、实盘授权或24小时判定规则。

## 验证及实际部署

- 新测试在旧源码明确复现2失败/2通过；修复后相关48项全通过。
- Mac干净源包完整回归 **1636通过、2项平台跳过，115.56秒**。
- Linux独立stage完整回归 **1638通过、0跳过，520.28秒**。XML SHA256 `948ff8525f9056cbc5266d6afdff00ef40df8b479af3346613c0ca8433a1a670`。
- 干净325文件源包SHA256 `867fdf0588040ddab783f3cb29a14949c48adc1f7220f776f42135dcff74cf25`；相对上一包仅4项源码、1新增测试、1修改测试变化。生产只替换这6项文件及manifest。
- 原v12已CLOSED且原终局Native+备份完成，再fresh11DEX空仓核验及备份后清理原grant、停用仅原v12期限、设置GRID_REAL_DISPATCH=0。原合同和失败观察证据保留。
- 初次源安装被private_json权限检查拦下：新stage manifest为0644，**尚未替换任何源码**。仅改为0600，内容SHA不变；在核对66旧源码未变、无安装proof/rollback目录后，由独立resume安装unit续行。保留原失败链和日志，成功安装proof记录resume_attempt=2；没有重跑完整测试、重放资金或覆盖失败链为成功。
- 实际安装proof finished/installed均true，Linux完整报告、原始空仓及备份、源文件SHA、守护实例和旧套利身份都有记录。新版本qualified=false，原v35以来的11项源码差异保留，后续实盘需审阅新版本，不沿用旧版本资格。

## 部署后现场复核

2026-10-23 23:50:20，新runtime PID **2273027**，外部守护健康且同instance，心跳及检查均新鲜。66项公有源码与325项manifest逐一SHA一致。活动策略0、未结束操作0、未决意图0、授权grant0，真实派单关闭。旧套利PID **1223049** 不变且正常运行。

2026-10-23 23:49:39新鲜交易所查询：11个永续DEX均无仓位/无挂单，原5笔成交再次核对。策略CLOSED，账本净收益仍+0.109088 USDC。没有使用空仓状态冒充本次修改后的持仓恢复或24小时验证。

网站 https://taoli.coinfetcher.xyz/grid/ ：匿名401、原账号认证页面200、metrics200（production/real_dispatch=false）、原策略详情200/CLOSED。登录信息保持原样。未恢复AI自动跟进或本机网格writer。

## 证据

本机及原Linux同名：grid-initial-exit-slots-install-proof.json、grid-initial-exit-slots-version-proof.json、grid-initial-exit-slots-linux-complete.xml、grid-ordinary-linux-v12-failure-containment.json、grid-initial-exit-slots-repair-chain.json。本机补充data/run/grid-v12-offline-inventory-replay-proof.json、grid-initial-exit-slots-final-review.json、grid-initial-exit-slots-public-access-proof.json、grid-v12-post-repair-native-audit.json。

下一阶段是基于新源码重新审阅、真实持仓断线恢复及独立有期限的新24小时验收；所有旧失败窗不复用。本次不宣称整个多交易所产品完成。

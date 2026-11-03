# Bulk 持续公共风险输入与原请求等待恢复

本阶段服务于完整 Bulk 普通网格上线，尚未开放实盘。原生初始/范围模型仍未实现，不能用公共数据“已齐”推导资金容量或资格。公共风险流开发阶段无金融动作；随后用户单独批准的小额买卖已完成，详情见BULK-ROUNDTRIP-PROBE.zh-CN.md。该金融验证使用冻结候选和单文件解析修复，未部署这里的公共风险流/共用引擎桥。

## 实现

`entropy_arb/grid/adapters/bulk_risk_feed.py` 提供一个公共订阅客户端和有界缓存，支持同一部署一条连接的最多16个配置市场，每个市场订阅ticker/risk/impact。没有account、signer、订单接口或自动开仓能力；会拒绝含认证头、Cookie、默认认证或环境代理的会话。调用方显式管理start/close，协议worker共享公共feed，单个资金账户关闭私有流不会关掉其他账户共享的公共流。

每次连接必须确认完整且无重复的订阅主题，并重新取得全部市场三类数据。断线、坏帧、同时间戳冲突及同regime计数回退使当前缓存失效；新连接从空状态开始。旧连接晚到回调在解析前拒绝，不能覆盖或清空已就绪的新代。原始数据同步解析，无无限队列；最多2MB帧，所有结构/精确数量校验复用原生组件。仅正常旧时间戳消息丢弃，重复消息不刷新本地接收时效。

- ticker保留原生nanosecond、mark、regime、regimeDt；regimeDt保留交易所10秒interval计数，不改成墙钟秒。
- impact沿用实际主网full/book四条250点曲线与nanosecond，保留与旧文档buyBps/sellBps毫秒示例的差异，不猜单位/补零。
- risk解析全部9个regime、原生notional杠杆曲线及相关系数；与ticker实时regime不一致时等待。组合相关性要求每个参与市场都有同一pair且相互一致，矩阵非PSD不准就绪，不制造对冲优惠。
- ticker上限5秒、impact上限15秒，同时验证交易所时间、本地墙钟接收时间与单调时钟；允许最多1秒时钟前偏。跨源时间差上限15秒。它是有年龄限制的输入集合，**不是原子组合快照**。
- risk配置本身没有原生时间戳，属于事件驱动更新；不以5秒ticker期限让未变化配置自动失效，也不把本地接收时间冒充交易所修订时间。配置只在同一已确认、存活的连接代有效，重连必须重新接收。
- 快照为深层不可变的数据类，带network/generation/revision/fingerprint；`assert_current`重新检查全部时效及内容指纹。任一已观察输入变化都会使旧令牌失效，不把心跳、公开流或该指纹当成交历史覆盖/账户交易权。

`BulkAccountVenue` 显式接收risk_feed，按部署检查、在多个账户市场view之间共享。opening gate新增公共输入前提，在队列等待后的最终发送回调读取并将输入交给原有必需的execution_gate，返回后再次校验同一指纹。risk/native容量/保护/账户授权仍须由该必需gate真正验证；仅拥有feed不能开启GRID1或live_qualified。减仓和撤单不新增该公共流前提。

## 等待恢复时不换原身份

`BulkGridVenue.retryable_not_sent` 只对白名单的流未齐、过期、变更及版本等待给出重试资格，并独立读取原始Bulk日志：准备阶段必须没有发出证据；最终阶段必须PREPARED且last_send_known_not_sent=true，原request_hash/账户/市场须一致。MAY_HAVE_SENT/ACK与未知、原生拒单、缺配置或坏模式均不适用。

共用Executor新增适配器可选的已证明未发送等待接口；满足上述条件后，同事务检查当前grid epoch/status/mode，保留原需求为PLANNED。恢复时继续相同intent/CID/request/nonce，而非转成永久REJECTED或造新补单。UNKNOWN仍走原身份查询，不能借“断线”重新开单。HL等未实现该可选接口的适配器走原行为；本轮只改本机和隔离候选，不替换运行中的HL共用源码。

## 验证及范围

- `public-risk-ws-mainnet-20261026.json.gz` 为本轮Linux主网匿名实际BTC/ETH六主题响应；`grid-bulk-risk-feed-capture-v1.json` 记录捕获已关闭，无账户/密钥/金融请求。gzip SHA256 e7adfec79741f3f360c525e8d81147279aa377a272bc96eb1f2977b5b631d5b5。
- `grid-bulk-risk-feed-native-v1.json` 用实际客户端连接主网，主动关闭后重新收齐全部公共风险输入，代1→2，实测2389ms；断线时不准使用缓存、旧代令牌拒绝，后续revision继续增加。两次GET/WS握手，没有交易连接；结束时task/socket关闭、cache清空。**这是无持仓公共风险流验证，不是用户账户带仓故障、普通策略恢复或24h观察。**
- 原生公共feed源码SHA256 2d5bd5b190746d793dda4529cba712591ca7be8909b4542b7e8a18305efa2b1c。后续只追加了adapter等待恢复接口/Executor桥，feed文件未改；不把早期Native证明改写成后续共享引擎主网实测。
- 本机扩展回归565项通过：全部Bulk476项加89项共用engine/原价等待/暂停/锁竞争/响应超时/资金费/原生保护/hardening/operations。包含真实BulkExecution+SQLite中最终检查变化/限流等待后断线、未发出日志保持、UNKNOWN不准伪装成未发送；共用Runtime验证恢复时同一intent/CID且实际模拟提交仅一次。
- Linux v1的474项为等待恢复桥增加前，XML SHA865a79c5dc75c5543e7397b31489c011abad4217ea8f9c56c385988f19ffd54b；不能冒称覆盖后续代码或与565相加。Linux v2两次75秒上限中断未形成通过结论；最后在新冻结bulk-final-regression-v1（含真实成交解析修复及其3项样本测试）完成568pass/0fail/error/skip，107.62s，XML SHA9bdbcc176f20f58c6faad0f2b27399dd1c785a7f1c67ac2e2ef5cfd77b802046。此前超时证据保留，不能把它们称为已通过或相加。
- `grid-bulk-risk-feed-isolation-v1.json`：Bulk/HL runtime/web/watchdog四个服务保持原PID，HL66源码无变化；旧套利PID1223049实际存活且cwd仍/root/code/entropy。Bulk grids/order_intents/operations=0，该公共开发快照当时没有单次买卖grant/approved/run，现已被随后获批实际执行覆盖，不能当当前资金状态。公共feed未部署普通worker；随后仅Bulk观察服务为有限买卖暂停/恢复，HL/旧套利未动，无本机金融writer或AI定时。

## 仍需完成

公共feed已开发并完成有限主网连接/重建验证，尚未作为生产daemon持续运行。账户私有快照与完整原生容量/范围模型、正式risk_prepare/fence、固定数量保护manager/lease和触发子执行、IP共享优先限流、完整历史、网站生命周期、普通网格闭环/带仓故障/残仓与24h/独立终局仍未完成。一次受保护微额买卖与其独立终局已完成，不等于完整网格验收。数量求解仍是枚举，不能凭组合对冲的复杂模型直接假定单调性加二分，也不能加大数量步长或降低资金上限掩盖性能问题。

来源：[Bulk公共流](https://docs.bulk.trade/api-reference/ws-market-data)、[Bulk组合保证金](https://docs.bulk.trade/bulk-exchange/Margin)、[官方计算器说明](https://docs.bulk.trade/misc/margin-calculator)。实际impact格式差异以捕获的主网数据为证据；未据此放宽正式资格。

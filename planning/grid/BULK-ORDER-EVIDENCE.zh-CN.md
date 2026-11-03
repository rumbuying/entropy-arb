# Bulk 原订单证据持久化

本模块补齐完整普通网格的原订单恢复链路，不授予交易资格。新增 bulk_order_evidence.py，BulkIntentJournal.record_lookup 由 BulkExecution.lookup 和 BulkGridVenue.lookup 调用；不依赖签名器，不触碰旧套利适配器或正在运行的 HL 服务。

## 行为

- 原 account/owner/network、client ID、native ID、request hash、历史起始 slot 和持久绑定必须一致。ALLOCATED/PREPARED 未发送状态不接受原生成交证据，身份不符即拒绝。
- 原 receipt/state/nonce/公开签名字节完整保留；查询得到的正面状态另存 native_evidence 和仅变化时追加的 native_evidence_history，不把查询伪装成新的发送回执。
- 原 UNKNOWN 继续只查原单，不换 CID/nonce、不重新签名、不重发。普通执行器重启后沿用相同证据；查询临时失败仍返回UNKNOWN，保留历史证据。
- 成交量不得超过原请求。已观察部分成交及 ACK 中的执行量构成下界；确定数量不得回退。数量未知的撤单仅携带已有下界，不能把默认0当成未成交。后续完整历史可以细化数量，已知终态数量不能变。
- 正面终态不能变回挂单或改为另一经济终态。撤单与成交竞争、旧挂单快照晚到、互相矛盾的终态均拒绝，不静默覆盖。
- 空历史页始终为 NOT_FOUND_TRANSIENT，不据此认定未发送、归零或最终消失。正面单个终态不提供整段成交/资金费历史覆盖，不推进账本游标或释放共享预算。
- 原有六字段stop root身份修复单独保留，不用通用查询按参数猜测另一订单身份。

## 验证范围

本机针对实际SQLite日志、BulkExecution及BulkGridVenue进行重启、乱序、ACK执行下界、元数据缺失、终态冲突、未发送拒绝和无签名器查询验证。Linux最新完整扩展591项通过（0fail/error/skip，96.82秒，1个既有aiohttp warning）；本机针对执行/查询/venue共85项通过。Linux冻结候选及最终计数见 data/run/grid-bulk-order-evidence-linux-v2.json/xml；v1是此前冻结阶段，不能累加为当前结果。

真实验证在 Linux 隔离目录 bulk-order-evidence-v2/source 运行，先核验已审计备份 SHA，再复制到独立数据库。只调用公开、无签名 /account 查询此前一次受保护微额买卖的原entry/exit/stop，保存并重开副本核验。交易所分别确认 FILLED/FILLED/CANCELED；stop成交数量仍未知，不伪造。原生产数据库、原备份及回执不改；没有加载私钥或构造金融请求。证据 grid-bulk-order-evidence-native-v2.json。

## 未完成范围

候选未安装到普通worker；旧通用完整范围风险模型、固定数量原生保护manager/lease及触发子执行、完整历史覆盖、IP共享限流、真实网格循环/带仓故障/小残仓、服务器24小时与终局审计仍待完成。一次微额买卖授权已使用，不能借本模块启动新的Bulk金融轮次。普通ACK实际响应为何未识别仍未定位，本模块只解决独立查询证据持久化，不推测回执格式。

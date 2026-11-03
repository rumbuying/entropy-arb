# Bulk 官方风险文档核对

本记录是对公开官方文档的只读核对，不代表已经验证生产实现、主网配置或用户账户，也不能作为放行实盘的依据。

- [Margin](https://docs.bulk.trade/bulk-exchange/Margin) 将下单校验描述为可用权益、包含挂单的累计杠杆及风险降低交易检查；文档同时说明这不是简单的初始保证金公式。该概述没有提供足以复现订单准入判定的完整比较式、所有挂单成交组合算法或具体边界。
- 同一文档的相关性、风险曲面和清算准备金公式主要描述组合维持保证金及清算风险；它们不能直接证明开放新仓的容量。公开公式不能代替主网实际风险参数、交易所原子快照或下单结果。
- [Real-Time Risk Engine](https://docs.bulk.trade/architecture/risk-engine) 指出风险随订单及持仓变化实时计算，并涉及市场影响和级联调整；本地规划必须将行情时效、账户全量持仓和其他挂单纳入证据边界。
- [官方 SDK HTTP 文档](https://github.com/Bulk-trade/bulk-client/blob/main/docs/rust-http-api.md) 说明账户历史分页的 coverage、min_available_slot 与 cursor 用法。当前 bulk_read 已要求完整覆盖与留存边界，并在后续页仅发送 cursor；仍须主网实际记录证明，不能把 unknown 视作完整。

当前继续保留 `BULK_RANGE_RISK_UNVERIFIED`。后续需要官方可追溯的订单准入细节、风险参数及边界以下行为的证据，并用无资金 oracle 或获得明确授权的小额实际样本交叉验证；真实 stop 触发子单、退出并发与历史覆盖仍是独立条件。

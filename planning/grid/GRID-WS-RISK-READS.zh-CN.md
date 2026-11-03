# 周期风控输入实时读取提速

2026-10-17额度修正：以下REST0或“本地900已满仍WS读取”的旧样本仅反映当时HTTP计数；旧实现漏计WS信息查询，不能推导无查询成本或持续可用。现1398版将HTTP/WS Info统一保守计量，不能越过原900权重额度；[本轮限流及修复](GRID-WS-WEIGHTED-LIMITS.zh-CN.md)优先于旧传输优化描述。原始测量保留为历史证据。

当前待部署，运行中第20轮仍为1391版本。该轮周期风险观测仍反复消耗规则与行情的REST权重，造成350预留等待；观察到62/86等完成预留消费记录。周期风控每5秒一次，不能依靠减少核对次数来提速。

修复：critical控制上下文内，最新meta和userFees经原严格账户只读WS一批读取；绑定已有grid_id的内部风险预览使用现有critical_market_snapshot（实时metaAndAssetCtxs/l2Book），普通网页创建预览仍原HTTP。规则/费用/报价均每次重新获取，风险、账本、资金费、保护、epoch和签名前后复验保持；没有缓存放行或延长期限。账户通道白名单从6项增为8项，meta不得携带user，userFees仅允许严格user地址，签名/订单仍拒绝。

只读Native完整输入实测：规则/账户费率+行情+完整11DEX账户，两次3.381024/2.667076秒，REST消费0、0签名，规则revision一致；数据为当次新读。证明grid-ws-risk-observation-native-proof.json，不构成完整派发耗时或实盘循环资格。130专项通过，增加费用变化必须刷新、严字段拒绝，以及正式风险预览全过程没有回落HTTP读取规则/行情的离线执行验证；最终完整检查见grid-ws-risk-full-tests.log。

部署必须等第20轮正常CLOSED、Native零仓零单审计与备份；当前sourcehash在grid-ws-risk-pending.json。完整1394 passed / 2 skipped / 110 warnings，78.95秒；待部署源码哈希已固定。

进一步主网只读验证：从运行库取一致账本副本，在独立数据库以无签名器、禁止/exchange的Transport运行正式rt.preview(dispatch-risk)，按当前真实0.0132ETH库存及原0.0044数量，存量/组合风险模型返回VALID；live_eligible仍False、guardian未配置（隔离副本）、耗时3.872195秒/REST0。该结果只支持数量与风险计算兼容，不证明完整实时派发、看护或服务资格。proof grid-ws-risk-shadow-preview-v2-proof.json；首版帮助脚本因未加载actor调用grid_snapshot失败，日志和原副本保留，修正后直接读取存储plan，未触及实盘数据库或凭证。

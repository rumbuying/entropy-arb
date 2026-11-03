# WS信息查询统一保守限速

第21 g-03e9205505bc4c00触发RISK_HL_WS_MARKET_RATE_LIMITED，冻结补单后正常退出。Native audit21三笔成交匹配、一止损撤净、11DEX零仓零单，净成本0.022579 USDC，余额198.865059。旧诊断未记录原始限流来源，不能断言最初是HTTP429还是WS429，也不能从本地HTTP used331推断交易所总用量低。

已确认本地旧代码仅计HTTP权重而未计WS info。历史REST0证明表示HTTP通道本地计数，不能表示查询无成本、无限吞吐或可以越过交易所限制；单次Native耗时测量保留，不能当持续运行资格。

[官方限制](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)列明REST1200权重/分钟、每IP10个WS连接、30新连接/分钟、2000条发送消息/分钟与100个同时在途post。文档将REST权重和WS消息限制分列；本次修复不假定服务端内部实现，而是主动对所有Info（HTTP/WS）共用原本地900权重/60秒、350保护预留及429退避。userRole60、轻量2、其余20，逐条在发送前扣减；连接失败等未发送部分退款。原WS消息/连接上限另保留，不靠传输切换取得额外查询额度。

本地额度等待和Native429分开：容量等待不建立连接或发送信息，也不会被HTTP回退绕过；风险观测及签名前授权若本地等待，未发送intent保持PLANNED与原身份，下一次必须重新完整核对，不创建风险许可。Native429仍阻断/冻结且记录来源（HTTP类型或WS客户端/Info类型），诊断只含符号和单调时间，不记录地址或响应正文。

111项专项通过，包括WS/HTTP共享计数、未发连接失败退还、900耗尽不得发送、Native429来源和原生风险/授权阶段本地等待均不签名、不暂停或增加订单。旧“HTTP额度900用满仍WS可读”的测试改为验证剩余真实统一额度，新增耗尽时零I/O检查。完整检查见grid-ws-weighted-full-tests.log，当前尚待部署。

已部署完整1398通过版本（2skip/110warnings76.98秒），执行69743，网站89365/看护13846健康。proof grid-ws-weighted-deploy-proof.json，Native最终撤净proof v21，备份last_seq231/integrityok，代码快照284文件全部SHA核验。第22在受限政策下验证，普通入口仍关闭。

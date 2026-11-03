# 实时授权查询传输优化

2026-10-17额度修正：以下REST0或“本地900已满仍WS读取”的旧样本仅反映当时HTTP计数；旧实现漏计WS信息查询，不能推导无查询成本或持续可用。现1398版将HTTP/WS Info统一保守计量，不能越过原900权重额度；[本轮限流及修复](GRID-WS-WEIGHTED-LIMITS.zh-CN.md)优先于旧传输优化描述。原始测量保留为历史证据。

2026-10-16，完整检查1363 passed / 2 skipped / 110 warnings，78.76秒；专项140 passed。此实现不缓存账户或代理授权。

原每次签名前都查询资金账户userRole、签名地址userRole及extraAgents，常规代理合计140 REST权重。签名后原有账户、仓位、订单、组合风险、epoch和派发开关检查保持。现使用[官方WS info post协议](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/post-requests)传输这两类只读请求；私钥、签名、action均不能进入该通道。实际下单仍走原REST签名路径。

账户与签名地址的两条userRole请求在一批中发送，逐个ID验证并按原请求顺序返回；即使回包乱序也不能交换身份。之后extraAgents再次查询Native。主账户/子账户/master绑定、命名代理、地址匹配和到期时间逻辑不变，每次都会执行。没有从旧结果放行。

行情与授权各自复用同一HTTP session/API端点下的只读socket；每类本地上限300消息/分钟、10次新连接/分钟，单批最多4条info且总超时4秒。仅授权类允许userRole/extraAgents与严格user地址，行情类仍仅metaAndAssetCtxs/l2Book。所有调用先校验整批参数，不能传签名或订单。响应ID重复/未知、kind不匹配或action回包会拒绝并丢弃连接；45秒无应用读取后先重连，避免复用Native已关闭的空闲socket。

临时传输不可用可回退原HTTP查询与原额度；429同时触发HTTP退避，scope/ID不匹配不回退。冷连接有两次实测超时，分别保留在grid-ws-authorization-native-proof.json和-v2.json，不能说该路径总成功。独立直接角色/代理查询曾约1.82秒完成。复用已建立连接后，再次完整实时核验约0.892644秒：重新发送3条info、REST本地已用900/900仍保持900，没有签名动作，绑定与代理有效性通过。证明grid-ws-authorization-native-proof-v3.json；记录warmup仅说明连接已建立，随后角色/代理数据仍重新请求。

测试覆盖每次刷新后代理撤销拒绝、无签名入口、响应乱序按ID绑定/重复ID拒绝、scope及429不回退、传输失败合法HTTP回退、旧空闲连接替换。第17轮实际CLOSED、Native零仓零单审计及备份后已部署，执行PID46835，独立网站/看护健康；部署证明grid-ws-authorization-deploy-proof.json。当前回到普通新增风险关闭且无私有试验政策的基线。此优化不代替成功保护重建/连续补单及完整资格审核。

# 全账户实时读取提速

2026-10-17额度修正：以下REST0或“本地900已满仍WS读取”的旧样本仅反映当时HTTP计数；旧实现漏计WS信息查询，不能推导无查询成本或持续可用。现1398版将HTTP/WS Info统一保守计量，不能越过原900权重额度；[本轮限流及修复](GRID-WS-WEIGHTED-LIMITS.zh-CN.md)优先于旧传输优化描述。原始测量保留为历史证据。

该实现1391检查版本已在第19正常结束及Native撤净后部署，执行63211，proof grid-ws-account-deploy-proof.json；当前第20运行此版本。后续meta/userFees和风险行情优化另见GRID-WS-RISK-READS.zh-CN.md，尚未部署。沿用官方WS info post协议：https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/post-requests 。现有订单签名/REST发送、账户归属、组合风险、账本fingerprint、epoch与保护期限保持。

只在已有critical读取预留内使用账户WS通道；网页查询仍原HTTP路径。允许clearinghouseState、frontendOpenOrders、spotClearinghouseState、userAbstraction、activeAssetData与perpDexs，严格检查字段、user地址、币种和DEX；不接受任何action、签名、历史分页或授权类型。整批所有请求先校验，再按最多4条一个批次发送；唯一ID及响应kind/type一一匹配，结果按请求顺序返回。每socket300条/分钟、10次新连接/分钟，单批4秒含排队；空闲45秒重建，无账户结果缓存。

完整快照先并发读取主账户仓位/模式/交易限额/订单，再按原逻辑核对现货资金及全部永续DEX；其他DEX的仓位和订单按4条分批读取，原不支持敞口与非USDC资产拒绝保持。实时流可用时继续使用原代次/布局匹配检查，不跨读取缓存。只改变传输，不删减风险所需数据。临时不可用或Native info error可回退原HTTP读取和额度；错误ID/kind/action及429不能回退，HTTP冷却期间拒绝此通道。

主网只读实测grid-ws-account-snapshot-native-proof.json：无实时流辅助时，两次完整读取11DEX、现货资金和目标ETH，耗时3.057182秒和2.285396秒；主仓位数据返回时年龄2328/2269毫秒，REST已用0、没有签名动作。仅证明该两次读取；不能视为开单耗时、补单或保护重建资格。此前分项探针也保留。

新增24项检查包括资金变化后必须新读、外来DEX订单/仓位、其他资产、保证金hold、重复余额、模式变化、错币种、整批后段非法输入不得先I/O、错误响应/429/HTTP冷却拒绝、合法暂时失败HTTP回退、网页继续HTTP。另用正式LiveRiskScheduler/AccountExecutor与原生格式模拟WS验证健康补单可执行、实际仓位不匹配和外来订单均冻结且不新增风险；签名器和交易所均为离线fixture，非真实密钥/交易。

完整检查1391 passed / 2 skipped / 110 warnings，77.12秒；最终专项118通过。证明日志grid-ws-account-final-full-tests.log、grid-ws-account-final-focused-tests.log，待部署sourcehash记录grid-ws-account-pending.json。第19尚RUNNING、仅初始1成交，当前没有更改其运行版本。

部署时48网格全部CLOSED，Nativeaudit19两笔成交匹配/11DEX撤净，完整备份last_seq218。第20启动和后续运行状态需查当前DB，不能沿用部署前零仓结论。

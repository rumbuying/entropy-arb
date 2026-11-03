# 保护行情与退出核对提速

2026-10-17额度修正：以下REST0或“本地900已满仍WS读取”的旧样本仅反映当时HTTP计数；旧实现漏计WS信息查询，不能推导无查询成本或持续可用。现1398版将HTTP/WS Info统一保守计量，不能越过原900权重额度；[本轮限流及修复](GRID-WS-WEIGHTED-LIMITS.zh-CN.md)优先于旧传输优化描述。原始测量保留为历史证据。

2026-10-16。最新部署证明 `data/run/grid-ws-exit-deploy-proof.json`，完整检查1350 passed / 2 skipped。当前只运行受限私有验收，普通实盘入口仍关闭。

## 真实问题

第十三轮在0.0107 ETH实仓、整仓原生止损及已归属的正常网格挂单条件下执行SIGSTOP。故障前按现有仓位加全部开仓单原始数量计算敞口52.62902 USDC，低于原定150；未净额抵消卖单或仅计算剩余量。看护6.804秒替代进程，81.981秒接口恢复，139.843秒原生减仓成交，196.582秒CLOSED。180秒门槛未通过，原失败记录不改写。最终3笔原生成交匹配，11DEX零仓零单，净收益0.006118，USDC199.195136。证据 `grid-service-position-fault-v5.json`、`-final-proof.json` 与 `grid-service-native-v13-proof.json`。

第十二轮观察程序较早到时，未注入故障，随后网格自动正常收尾；5笔原生成交匹配、净成本0.069605，不能当成故障通过。第十三轮初次宽度超过试验2%限制，创建前拒绝、零下单；拒绝配置与结果单独保存在 `grid-service-native-v13-rejected-range.json`。确认无已分配网格后，修正范围并重试，未重复创建。

## 行情读取

[官方WS post协议](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/post-requests)支持通过WebSocket发送info请求并按请求ID返回原始结果；[官方限额](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)区分REST权重与WS消息/连接/在途请求限制。

新增MarketInfoSocket仅接受metaAndAssetCtxs和l2Book两个公共读取，严格请求ID/响应类型匹配，不提供action/签名接口、不缓存结果。相同HTTP session和API端点共享一条连接、同一时刻一条info请求；本地最多300消息/分钟、10次新连接/分钟，单次含排队最多4秒。超时/中断/不匹配会丢弃旧连接，迟到响应不能用于下一次请求。429会停止WS读取并触发REST退避，不能借回退绕过限流。

仅执行与保护用途的critical_market_snapshot采用此路径；网站普通行情读取仍走原REST额度。WS暂不可用时尝试原有受限REST读取，错误账户/币种/响应类型不接受；原标记价与盘口校验、最多5秒时效和保护原始期限不变。

独立只读实测：在本地REST额度已用900/900时，2条WS请求约2.153秒取得ETH标记价及盘口，盘口年龄527毫秒，未新增REST读取或签名动作。首次测试发生超时并等待REST回退，未把失败隐藏；后续成功证明保留该次失败说明，见 `grid-ws-market-implementation-native-proof.json`。该证明只证实公共读取路径，不等于真实保护补挂通过。

## 退出恢复

RecoveryBarrier在desired_mode=STOPPED的HL网格才改用严格目标币种position_snapshot；接收账户、原生时效与账本数量必须一致。继续完整回补成交、校验未知单/所有归属订单累计成交和外部订单、同步资金费。该分支不计算可用开仓资金，恢复预留由350调整为200，实际读取仍逐项计费，不提高总上限。ACTIVE/PAUSED及新增风险仍完整账户与组合风险核对。普通恢复不能请求退出专用分支。

新增测试覆盖STOPPED与PAUSED两条真实适配器路径、错误账户/过时原生数量拒绝、额度耗尽下WS行情、无签名接口、响应错配、迟到超时、429不回退，以及持仓故障中已归属工作开仓单的完整gross敞口核对。带工作单的演练覆盖正常活动网格状态，不能借只检查当前净仓位略过在途风险。

接续以第十四轮同参数实盘故障复验和真实止损重建结果为准。旧第八轮190.233秒和第十三轮196.582秒失败均保留；没有正式普通grant。


## 第十四轮复验通过

GID `g-6f1ffa9da0754a3a`，Native故障前持仓0.0107 ETH及完整工作单gross 52.700924。实际SIGSTOP34205后独立看护6.902957秒替代进程，81.099783秒RPC恢复，131.128秒原生退出成交，135.435秒数据库CLOSED、135.672311秒观察确认，180秒门槛通过。原始proof `data/run/grid-service-position-fault-v7.json`，汇总 `-final-proof.json`，Native审计 `grid-service-native-v14-proof.json`。两笔原生成交匹配、11DEX零仓零单、净成本0.040829/余额199.154307；服务资格审核组件的fault与native_audit均独立读取真实文件及数据库通过。此前失败不覆盖。

第十五轮只验证连续网格与止损重建，不再重复故障注入。新受限政策仍不等于普通实盘grant。

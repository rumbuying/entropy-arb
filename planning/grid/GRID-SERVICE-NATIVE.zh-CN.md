# 混合保护第一轮主网试验

2026-10-15。试验 `g-a83789bc1ef34faf` 已 CLOSED，未取得新模式实盘资格。

使用显式独立试验政策：ETH、多头、GTC、5格、2369–2379、下方原生止损2302；创建投入30 USDC，追加缓冲10 USDC，过渡期最大名义敞口150 USDC、最长30秒。普通网站资格始终为空。政策只允许两个固定请求标识，单格资金上限50、累计100 USDC，期限45分钟。

正式引擎走过真实挂单和成交：5笔买入各0.005 ETH，分别2377、2375、2373、2371、2369，均为真实maker；退出卖出0.025 ETH，成交2366.9。共6笔原生OID/TID/客户端标识/方向/数量/价格/手续费与本地记录逐笔一致。手续费0.035523 USDC，交易净收益-0.188023 USDC，余额从199.828539变为199.640516 USDC，差额完全一致。原生整仓止损574906329900在最终清仓成交的同一原生毫秒被reduceOnlyCanceled，符合归零取消行为。

没有真实refill成交，也没有进入service_coverage重建阶段；这轮不能证明归零后重新建仓的保护连续性，更不能证明持仓故障恢复。私有政策和公开新开单均已撤下，独立原生检查当前11个永续DEX零仓位/零挂单。当前30 CLOSED、executions72/events142、未终局/UNKNOWN/资金预留均0。

本轮约03:04Z提前请求退出，持久原因却统一记作OPERATOR_TRIAL_EXPIRED，早于政策03:37Z期限。旧实现把看护、规则、配置、政策变化等全部映射成该原因，证据不足以追溯具体丢失条件。不能推断为真正到期，也不能根据当前看护健康倒推当时健康。

修复后的OperatorTrial.refusal将范围撤销、政策到期、政策替换、模式撤销、原生规则改变、独立看护失效分别编码；退出前持久记录operator_trial_refused事件，看护故障保存同一次健康判定结果。已结束/已请求停止不重复干预，原范围清仓权限保留。专项94通过，全量1237 passed、2 skipped、110 warnings（73.27秒）。后续试验须使用新政策/新请求标识，不能重放本轮的start或刷新原期限。

证据：`data/run/grid-service-native-v1.json`、`grid-service-native-v1-start.log`、`grid-service-native-v1-proof.json`。全量日志`grid-trial-refusal-full-tests.log`，追加专项日志`grid-trial-refusal-tests.log`。备份`data/grid-backups/grid-20261015T133019Z-ea447e32`完整性ok、事件142。早期预览截图`data/run/grid-coverage-preview-ui.jpg`仅证明界面与只读预览。

下一步：带准确原因审计重新验证实际补单及归零重建，再做受限持仓故障演练，完成新模式原生证据审核器后才考虑恢复普通资格。不能把关闭开关、测试数量或单次清仓作为整个产品上线完成的证明。

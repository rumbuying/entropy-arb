# 第四轮：真实归零与过渡保护超时

2026-10-15。`g-44fc282ffca54040` 已完整结束，未取得新模式资格。

ETH多头GTC、2335.5–2355.5/5格，总投入40/缓冲20 USDC；原生整仓止损、过渡敞口150 USDC/最长30秒。初始买入.005ETH@2352.2（14:05:15.973Z），普通卖出.005ETH@2357.7（14:06:09.647Z）将仓位归零。交易所原生整仓止损575535009149在该卖出成交同一毫秒reduceOnlyCanceled。

系统14:06:12.731Z记录service_coverage_started，绑定旧止损OID及Native取消时间。期限固定为14:06:39.647Z，约3.084秒检测延迟没有刷新起点。14:06:39.874Z请求PROTECTION_SERVICE_TIMEOUT退出，约在期限后227ms推进退出epoch；剩余普通初始/补单意图均未发送并作废。记录中的gross_qty/open_notional均0，没有已接受的再入场订单；这证明的是实际归零后的超时路径，不能证明持仓覆盖或重建成功。

原生成交OID/TID/客户端标识/量/价/费独立匹配。手续费.010596 USDC，净收益+.016904 USDC，余额199.415299 USDC；11个永续DEX最终零仓零单。33 CLOSED、78成交/156事件，未终局/UNKNOWN/预留0。自动保护退出与外部限时流程的stop_flat出现OPERATION_CONFLICT，实际保护退出已成功；旧runner记录的冲突保留，随后用原生与SQLite已结束状态证明收尾。后续runner在冲突后只观察既有收尾，不重放启动或另行发单。

未产生新的service保护意图，旧版本也未记录具体等待步骤，因此不能断言超时的唯一原因。额度调度存在可确定复现的问题：一个读取序列结束并退回未使用预留后，另一等待者仍按原先计算的滚动窗口休眠，最长可达60秒，超过30秒重建期限。新增事件唤醒：等待者同时等待容量变化及原窗口定时器，退回预留后立即重查；总额度、逐请求计费、网页预留隔离和429冷却均不放宽。取消会清理等待任务，不产生额外预留或请求。

同时记录rearm_step/rearm_step_ms：guard_before_account、account_snapshot、guard_before_order、resolve_unknown、guard_before_submit、submit_native_stop、guard_before_confirmation、native_stop_lookup、confirmation_quote。每次进度写入均检查原generation/epoch/ACTIVE和原期限；旧生成代次/新暂停/超时不能改写或延长期限。该记录是后续定位依据，不是资格证明。

专项71通过；完整1247 passed、2 skipped、110 warnings（74.65秒），日志grid-reservation-wakeup-full-tests.log。备份grid-20261015T141359Z-8700855b（156事件，完整性ok）后空仓部署，并用新有限政策开始第五轮。当前是否运行以实际句柄、SQLite和RESUME.md为准，旧v4启动请求不得重放。

证据：grid-service-native-v4-proof.json、grid-service-native-v4.json/log、grid-service-native-v4-health.jsonl。公开网格资格仍为空，仍须重建成功、真实补单和持仓故障恢复证据及审核器。

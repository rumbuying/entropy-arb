# 原生保护无法确认时的风险收尾

2026-10-15。该修改处理保护缺口出现后的自动收尾，不能证明空仓重入的保护连续性已经修复，也不开放普通实盘。

PROTECTION_ORDER_UNVERIFIED与PROTECTION_REPLACEMENT_UNRESOLVED不再仅暂停并保留库存，而是使用既有持久化保护退出队列。先原子提高epoch、设置STOPPING/STOPPED、使未发送补单失效，记录不可变的protection_gap_exit_requested事件；此阶段不调用交易所，避免在发送锁内等待撤单。退出调度再撤销旧单、核对迟到成交、查询不确定订单，并只减仓退出全部已归属库存。异常或未知退出继续对账，不盲目重发。

较新的用户暂停/结束不会被旧风险观察覆盖。资格审核同时读取历史risk_paused与保护缺口退出事件；哪怕已有完整双向refill、maker证据、最终CLOSED、原生审核缓存，保护无法确认仍使试验资格失败。

新增场景验证：两种保护丢失原因的持久化退出、冻结时无交易所I/O、重复请求不重复退出、已经接受的开仓单在冻结后成交并纳入清仓、较新用户暂停优先，以及两个事件类别的缓存资格拒绝。旧发单验证已同步新行为，并检查旧补单失效及不可变事件。完整回归1140 passed、2 skipped（68.12秒）；记录grid-gap-containment-full-tests.log。

已备份至data/grid-backups/grid-20261014T180258Z-31ecce85（完整性ok、事件132），并在全部网格CLOSED时优雅重载执行服务。执行PID3263、网站89365；重新RPC和独立原生查询：29 CLOSED、未终局/UNKNOWN0、预留0、Native仓位0/挂单0、USDC199.842651，GRID_REAL_DISPATCH=0和grants=[]保持。证据data/run/grid-gap-containment-deploy-proof.json。一次catalog读取超时后没有重启服务；metrics和原生核对另行成功。

后续页面补充：detail只读返回保护退出的原因、阶段与版本，不暴露操作请求内容。保护异常时明确正在撤单/对账/平仓，未完成时提醒可能仍有仓位和挂单；普通保留仓位结束不会错误承诺平仓。此界面增量的完整回归及重载结果见本轮后续记录。

最终记录：界面/状态增量全量1140 passed、2 skipped（68.27秒），grid-gap-containment-ui-full-tests.log；JS语法与diff检查通过。随后真实RPC发现重启后已结束网格的详情仍等待首次行情，引发超时：已改为CLOSED直接用账本，不发行情读取。新增无行情验证，相关API/详情/保护退出17项通过（该最后增量未重复全量）。最终优雅重载到执行PID3667，网站89365；RPC实际已结束ETH详情返回成功、market_observation=null、protection_exit字段存在。历史手动收尾没有新增队列所以其值为null，不伪造保护退出记录。最终metrics仍29 CLOSED、executions66/events132、未终局/UNKNOWN/预留0、real_dispatch=false。证据同一部署proof中的final_metrics/final_jobs与source_sha256。

实盘保护问题仍需更完整设计，见GRID-FLAT-PROTECTION-GAP.zh-CN.md。服务器风险收尾不能回溯阻止已在交易所的订单，也不等于原生保护无缝覆盖。

# Bulk 固定数量保护阶段验收

固定 stop 已接入共用日志、唯一执行器、保护管理及独立看护。新止损确认后才撤旧单；未知只查询原身份；暂停保留保护。新增风险发送前核对完整原生数量、原请求、共享引擎权限、私有流、公共风险输入和独立看护。原生观察最多5秒，同时使用墙钟与单调时钟。

30秒数量证明绑定配置、凭证、策略epoch及进程会话；卡住的查询或晚到回执不能延期。明确保护危险、证明过期、看护丢失及启动时账户未加载，都在本地排队退出；排队不等于实际清仓。有限恢复epoch携带保留原证明期限。完整行为及后续缺口见 BULK-FIXED-PROTECTION.zh-CN.md。

最终冻结 Linux v6：742通过，0失败/错误/跳过，155.55秒，1个既有aiohttp warning；XML SHA 9e539e04feeb1d75401bae72f6c4b9045f1938ddc7259354134aa10d737029a7。14项本机变更源码/测试与最终manifest一致。本机最新相关组合69通过。之前v5为740、v4为738、v3为735、v2为717，均为各自冻结阶段，不相加；v1因旧测试仍假设通用通道拒绝stop而失败，已保留失败证据并按新通用能力更新测试，有限stop-only工具白名单不变。

Linux SDK v2实签36组，通过两个官方Rust oracle、原生签名字节/root及发送格式/每组6类篡改拒绝。连接被系统拦截，只使用已知未入金临时key，无用户key或账户访问。3项SDK相关SHA与最终候选一致。SDK v1脚本占位符替换语法错误未执行任何case，不作为通过证据。

本阶段没有金融发送，没有部署生产Bulk或修改生产账本；普通grids/operations/order_intents均0。HL66运行文件摘要未变，HL runtime3835010/watchdog3835012、Bulk worker3802260/web3222058、旧套利1223049保持原进程；旧套利cwd为/root/code/entropy。

收尾只读核对发现HL v20于恢复重建报错后自动结束，CLOSED/STOPPED/e18。原cleanup582b36ab-2ea6-4739-9194-cfdfc0154aab已succeeded/remaining0，原生11DEX零仓零单，stop已reduceOnlyCanceled，独立audit与备份exit0。7次受保护恢复成功，第8次重建UNCLASSIFIED_REBUILD_ERROR；不足24h，普通与受保护24h均false。该结束不是本次Bulk部署造成，本阶段没有HL金融动作。净-0.358265 USDC；原日志/收据保留，不重投或自动开启新轮。

Bulk普通实盘仍未开放：初始/范围风险模型、IP共享优先限流、完整历史/触发子执行、归零取消后的自动重挂及小残仓实际退出、网站生命周期、真实网格与带仓故障/24h终局都待完成。特别是recovery.py现有unknown-cumulative分支仍仅认可whole_position，固定parked stop与触发执行需补齐可靠语义，不能直接跳过缺口或伪造cumulative_known。隔离用例不代替主网持仓证明，Bulk旧微额资金许可不能复用。

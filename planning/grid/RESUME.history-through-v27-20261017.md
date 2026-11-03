# 最新优先状态：第27已终局（2026-10-17约17:00北京时间）

第27 `g-a5c8e6d025a8474f` **CLOSED/STOPPED/e6**，不能继续旧helper或重放start。真实带仓pause/resume先成功，随后WS断线freeze进e4，保护lease仍e3；恢复短暂发布RUNNING/e4后等待freshproof，被Native看护正确按缺失确认请求退出（实际是版本交接缺陷）。普通资格因此拒绝，不可复用27当loop。Native最终2fills/2stops/11DEX零仓零单，净-.011393，余额198.851561。备份 `data/grid-backups/grid-20261017T085108Z-d53c310c` seq262。主31853/收尾91490本轮已terminal0，soak53153终局1，最长约642秒；有真实退出/epoch/状态问题，不可宣称全程0异常或24h。

已在隔离副本 `data/run/grid-resume-fix-s6zaq8fi` 复现真实 on_stream_loss→recover→watch 时序，proof `grid-stream-gap-epoch-race-offline-proof.json`，无真实网络。当前主工作区core仍1427版、runtime75791/site89365/watch13846且空仓。

准备修复（尚未部署）：stream freeze原子迁移尚有效且scope/stop身份完全匹配的旧lease，仅epoch变化、不改时间/期限；Native轻量看护覆盖RECOVERING/NEEDS_ATTENTION ACTIVE；只有新Native查询才续期。recover重建前保持RECOVERING，freshproof与STARTING新epoch原子提交，避免短暂RUNNING放开派发。新14项故障边界覆盖deadline/失配/卡住/真实freeze时序；相关61测试待查结果。最终全量handle38468是专项、全量新handle见实际工具输出，最终log `grid-stream-recovery-handoff-final-tests.log`。先前1439那次不含最后NEEDS_ATTENTION两项与白名单，不能冒充最终整包。

第28准备文件/政策已生成，但**尚未部署、未启动、无新网格**。RID service-loop-20261017-v28；资金40/20、150/30、long immediate/GTC/5格/3cross，政策仍UTC15:50到期。fresh只读Native预检源 `grid-service-native-v28-preflight-source.json`（复制旧27终局记录），审计90496终局0，proof对应`...-source-proof.json`；不覆盖原27审计。

下一步：最终全量通过→再次核对全库空仓与Native预检新鲜→launchd部署28→绑定版本/心跳→启动一次helper及原生/健康/自动最终审计。优先真实重验断线恢复（自然断线也算，记录实际event/Native）；继续原4双向refill+归零重建资格门槛。不要引入无需的调试签名入口或修改金融门禁。

已准备的ordinary-after-v27配置、预检、文档绑定旧失败27，**不能用于上线**；应迁移到最终有效loop轮次后再重验。普通grants仍[]。目标active。

以下为第27退出前及更早的历史，不作为当前运行状态：

# 当前续接状态（2026-10-17 16:40 北京时间）

目标仍 active：独立通用网格网站尽快可靠实盘可用，用户已授权 CLI/API 使用不到 200 USDC 的账户验证。不通过 CUA 执行金融操作。旧套利 8788 不改；普通 grants=[]。

## 当前部署和进程
- runtime 75791 / web 89365 / watchdog 13846，launchd管理。网站 http://127.0.0.1:8789/ 。外部心跳/看护同instance且健康，普通grants=[]。
- v27 RID `service-loop-20261017-v27`，政策 `data/run/grid-service-operator-policy-v27.json`，中国23:50到期；40总/20reserve、150/30、long immediate/GTC/5格/3cross。相同边界，不延长旧政策。
- 主helper **31853** 已启动（最近poll待续核对），已create并提交start，GID `g-a5c8e6d025a8474f`，最新RUNNING/ACTIVE/e3，不能重复start；health30062 / Native15684 / diag47397 / finalizer91490 / 临时防idle2527（绑定75791最多8h）；只读soak53153已实际采样（operator-trial，不算ordinary24h），早期11样本/50秒0issues。
- 文件 `grid-service-native-v27.json` / `...-v27-version-proof.json` / `...-v27-finalization.json`。**带仓pause/resume已真实成功**：pause bc84931d-da92-40a8-bd80-ff33f64920a9 5撤净/0剩余；resume47a91b4b-9bf1-4a32-8bd4-70d9fb102a91 5placed/0pending。独立Native持仓与账本均.0044ETH，新stop577415771413 OPEN、3cross，initial IOC仅1，0保护异常。proof `grid-service-native-v27-held-management-proof.json`。正在等原>=4双向refill+本轮归零重建/入场，完整资格未过。原deadline_ms=1792247665217（UTC 2026-10-17T14:34:25.217000+00:00），不延长。
- **v26已CLOSED** `g-e9ed13ca0a2f46d7`：启动254.986s成功，但pause返回partial（4撤净/1仍开放），helper未resume且转正常stop成功；不是原resume修复失败，因为没有执行resume。Native2fill/1stop/11DEX零仓零单，净-.012718，余额198.862954。Native原stop在PAUSING独立OPEN。proof `grid-service-native-v26-pause-partial-proof.json`。主27304/收尾79653本轮已poll终局0，soak等待14508终局1（未启动观察）。
- 最新关闭备份 **data/grid-backups/grid-20261017T082419Z-ef9ff02d**，seq256。v26自动Native审计/备份成功，资格拒绝正确、未安装grant。
- v27前补上暂停有限最多3轮撤单核对/重试（同operation/epoch）；CANCEL_PENDING/CANCEL_REQUESTED先Native查询，仍UNKNOWN零重复签名，确认OPEN才可再cancel。较新stop打断旧pause；保留原生保护。隔离40专项通过；完整 **1427 passed /2skip/110warnings/81.07s**，log `grid-pause-retry-isolated-tests.log`。v27空仓部署proof `grid-pause-retry-fix-deploy-proof.json`，bootstrap一次0，健康已核对。
- 最新299文件源码快照 `data/grid-code-checkpoints/grid-code-20261017T0829Z-pause-resume.tar.gz`，manifest所有archive+当前源sha/size重读匹配；含公开.env.example和tools/star_probe，无私有env/vault/data。proof `grid-source-checkpoint-pause-resume-proof.json`。不是不可变安装目录。

## 此前resume版本切换修复（已包含在当前1427中）
离线确认恢复版本切换导致保护 lease epoch 不符，watch 错误退出（`grid-resume-epoch-race-offline-proof.json`）。修复 lifecycle/protection_watch/runtime：恢复前新鲜 Native OPEN 查询，和新 epoch 原子提交，不继承旧确认、不延长原查询期限。覆盖手动 resume 与自动 recovery rebuild；保留 STARTING 保护看护与所有金融门禁。
隔离全量 **1421 passed / 2 skipped / 110 warnings / 78.55s**：`grid-resume-fix-isolated-final-tests.log`；新恢复专项7通过（其中6已包含全量，不相加）。部署 `grid-resume-protection-fix-deploy-proof.json`；首次 bootstrap 5，待旧 job 退出后重试0且健康，记录保留。
原始1398 AST 连续证明只描述修复前，不能再声称当前核心未变化。旧 fault23/small20路径无语义变化，不无故重跑；当前受影响恢复路径必须实际复验。

## 已完成证据与资金
v25 `g-af49a651f1344072` 已正常 CLOSED/STOPPED/e2，cleanup eb992ab4-b9b0-43b9-aa0f-3bfbdb0e425d 成功。Native 2 fills /1 stop /11 DEX 零仓零单，净成本 .007045 USDC，余额198.875672。不要续用旧RID。自动审计/备份均成功，完整资格仍未过（无4 refill/pause/resume）。最新关闭备份 `data/grid-backups/grid-20261017T081105Z-6709674d`，last_seq253。
v24 loop `g-97f4ea0d005e4cb2` 真实重建、重建后再入场、因果买卖及全 protection/native/fault 组件通过，但只有2refill，完整loop缺管理操作；6fills净+.020593。small20 `g-c28822e3563f4019` 小残仓 Native IOC成功；fault23 `g-c9172805f2df4e0c` 真持仓看护故障/Native退出<180秒成功，artifact `grid-service-position-fault-v8.json`。v26审核组合复用small20/fault23。
所有细节和旧状态在 `RESUME.history-before-resume-fix-20261017.md`，仅需要时按关键词查，避免整篇重读。核心 baseline 源快照 `data/grid-code-checkpoints/grid-code-20261017T015726Z-ui-resilience.tar.gz`，本次回退源 `data/run/grid-resume-fix-rollback-source`。

## 普通入口发布准备（当前未执行切换）
- 已准备但未加载 `data/run/grid-runtime-ordinary-after-v27.plist`，移除私有trial，仍要求正式政策；现政策grants=[]。
- 新只读预检 `data/run/grid-ordinary-v1-preflight.py`；实际当前返回 REVIEWED_CANDIDATE_NOT_READY/未开启派发。14项隔离拒绝/通过边界及文件不变场景已过，正式审核正例使用桩，仅证明运维门禁，不算真实资格或当前核心1427。
- 后续按 `ORDINARY-CUTOVER-AFTER-V27.md` 切换；现有审核/运行配置已准备，不重复写另一套工具。候选不存在时不要反复跑预检或新开试验，继续31853同一自然成交观察。

## 接下来只走关键路径
1. 延续 v27 同一 helper，已通过真实带仓pause/resume，继续当前保护和真实补单/归零重建循环；不重复已通过的无关试验。
2. CLOSED 后自动 Native audit / review bundle / backup；候选只是待审，不安装 grant。完整验证通过才能有限普通入口；不能把试验运行当上线。
3. 正常入口有限实盘 + 实际24小时观察。已开发只读 `soak_observer`/`release_audit`，trial 不等于 ordinary24h。新交易所和高级功能暂后排。

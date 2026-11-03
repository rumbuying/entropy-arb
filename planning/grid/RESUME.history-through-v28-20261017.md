# 当前续接：第28轮（2026-10-17 17:04 北京时间）

目标active。用户授权CLI/API在不到200USDC独立账户验证并自主推进实盘上线；不可通过CUA执行金融操作。旧套利8788不改，用户负责不同资金账户。普通grants仍[]、尚未正式上线。

## 现在正在运行
- 执行 **80570** /网站89365/看护13846，launchd管理，外部心跳/看护同instance健康。网站 http://127.0.0.1:8789/ 。
- **GID g-5c7d113dc62e4c3a**，RID **service-loop-20261017-v28**，已create并提交start，最新RUNNING/ACTIVE/e3，真实带仓pause/resume已成功，5撤净/5恢复0pending，Native持仓与账本.0044ETH且stop OPEN；proof `grid-service-native-v28-held-management-proof.json`。**自动断线恢复尚无新真实事件，不标通过**。主helper **4811**；health40353 / Native94485 / diag90858 / 自动收尾61143 / 临时防idle63960（绑定80570最多8h）/ 只读soak23067已实际采样，operator-trial不算ordinary24h；只读断线取证97280在运行，已保留旧84749终止前记录v1。
- 政策 `data/run/grid-service-operator-policy-v28.json`，中国23:50到期；40总/20reserve、150敞口/30秒、long immediate/GTC/5格/3cross。间距2.6，fresh mark-9.75到+13，创建前费用正价差检查。原6h deadline_ms=1792249578752（UTC 2026-10-17T15:06:18.752000+00:00），不延长。不要重复start/IOC、改配置或延长旧deadline。
- 文件 `grid-service-native-v28.json`、`...-v28-version-proof.json`、`...-v28-finalization.json`、`...-v28-processes.json`。普通/私有上下文严格分开。

## 当前代码和最后修复
- 核心 **1441passed/2skip/110warnings/79.23s**，log `grid-stream-recovery-handoff-final-tests.log`；61相关专项通过，有重叠不相加。空仓部署proof `grid-stream-recovery-handoff-deploy-proof.json`（bootstrap一次0，外部健康已核对）。
- 新断线修复：on_stream_loss立即fence旧epoch，同时只能把仍有效、scope/stop完全匹配的旧Native lease迁移epoch，原时间/期限完全保留；RECOVERING和NEEDS_ATTENTION的ACTIVE网格继续轻量Native看护，必须新查询才续期；到期/失配照常退出。恢复重建在freshproof与STARTING新epoch原子提交前保持RECOVERING，不短暂暴露RUNNING。当前绑定扩为67个网格Python/适配器/入口/路由文件，与1441归档及当前源逐项hash相等；仍不是不可变运行镜像。真实freeze→recover→watch离线复现旧bug：`grid-stream-gap-epoch-race-offline-proof.json`。
- 已包含此前手动resume保护epoch原子提交、暂停最多三轮核对撤单、CANCEL_PENDING/REQUESTED先查后决定重试；unknown不盲重签、较新stop能打断旧pause。没有移除STARTING保护检查或新风险门禁。
- **当前断线恢复还需真实复验**。第28正常运行中自然断线也可作为证据，须核对stream_recovery_attempt成功/新epoch/真实Native/未重复初始IOC。Native flight本轮新增grid_epoch和同时读到的engine_native_lease，仍只读。新增 `grid-service-stream-recovery-observer.py` 等实际成功stream_recovery_attempt且当前epoch精确等于该恢复新epoch，再独立Native核对Stop/持仓/未重复初始意图；输出v28-stream-recovery-proof.json。不签名、不改DB或授予资格；5历史Native响应+合成时钟边界检查单独通过，不算1441，不当真实断线已过。
- 最新源码归档 **data/grid-code-checkpoints/grid-code-20261017T0903Z-stream-recovery.tar.gz**，300文件，sha a41fd54475ab0f518e45b3e3e77297f7b05c511d8b37d29fbb617ed13d11aef0；所有archive/current size/hash重读核对，包版本未变。proof `grid-source-checkpoint-stream-recovery-proof.json`。含公开.env.example/tools，排除私有env/vault/data；不是不可变安装目录。回退源 `grid-stream-recovery-rollback-source`。

## 第28新增实盘事实
09:27:46.521 UTC initial maker buy.0044ETH@2433.1真实成交（总2fills，尚无refill实成交）。对应refill sell.0044@2435.7，IID1cb538ce-e0bc-4b5e-bdbd-9e233f65e4af/OID577437953582/CID末尾17f，Native OPEN/Gtc/reduceOnly，源成交OID/TID/CID/side/qty/price/fee/maker与因果key逐项匹配。Native接受距源成交6082ms，独立观测184243ms；两种时长不能混淆。proof `grid-service-native-v28-first-refill-proof.json`。只是补单已挂置，不是补单成交/完整资格。

## 第28活动快照备份
`data/grid-backups/grid-20261017T094252Z-22569d4d`，seq266/integrityok，RUNNING/e3、2fills、两buy库存各.0044ETH（合.0088）、reserved40。Opaque凭证/两政策所有size/hash/owner/private权限与源核对一致，未解密/未启动任何恢复服务。proof `grid-service-native-v28-midpoint-backup-proof.json`，DB sha f2ce68e589dde6a221ec1f7575be2b198b062d0d477b98a6d9ba317f58f0076a。是active snapshot，不是关闭/资格证据；恢复只能隔离只读并先Native重新核对，不能重放旧RID/IOC。最后真正CLOSED备份仍085108Z-d53c310c。

## 已结束试验，不能重开
- 第27 **g-a5c8e6d025a8474f CLOSED/STOPPED/e6**：手动带仓pause/resume真实成功（5撤净/5恢复，Native持仓/账本.0044ETH，Native stop OPEN，initial IOC1）。随后WS断线freeze进e4但旧lease仍e3，短暂RUNNING/e4时触发PROTECTION_SERVICE_NATIVE_PROOF_REQUIRED退出；因此不能当完整loop资格。主31853和收尾91490本轮terminal0，soak53153 terminal1，最长约642秒且有退出/epoch/状态异常，不是全程0异常或24h。
- 最终Native2fills/2stops/11DEX零仓零单，净-.011393，余额198.851561。原审计 `grid-service-native-v27-proof.json`；**最新关闭备份 data/grid-backups/grid-20261017T085108Z-d53c310c seq262**。
- 第28前fresh只读Native预检 `grid-service-native-v28-preflight-source-proof.json`（UTC08:56:53），同样11DEX零仓零单，保留27原历史未覆盖。28创建前56CLOSED/135executions/262events/open UNKNOWN预留0。
- small20 **g-c28822e3563f4019** 真0.0001ETH原生IOC完整残仓退出通过；fault23 **g-c9172805f2df4e0c** 真持仓+工作单、挂起后约6.977秒替换/138.769秒Native退出/142.039秒观察关闭通过，`grid-service-position-fault-v8.json`。正式组件范围匹配可复用，不因无关工具重复演练。
- 第24 g-97f4ea0d005e4cb2 Native归零重建/再入场/补单及全保护组件通过，但仅2refill、无管理，不是完整loop。旧环节详见历史，不全文重读。

## 下一步，只走关键路径
1. 继续第28同一4811：真实带仓pause/resume已过，继续等待原>=4双向真实因果refill、本轮Native归零重建/后续入场；核对新断线恢复实际表现。达到条件正常收尾，否则原deadline正常结束，不降低门槛。
2. CLOSED后61143自动Native audit→正式review bundle→备份，候选只保存不安装；任何保护异常都会拒绝资格。不能复用失败27为loop。
3. 普通发布准备已迁移第28：`grid-runtime-ordinary-after-v28.plist`未加载；`grid-ordinary-v1-preflight.py`只读，当前应拒绝CANDIDATE_NOT_READY；14合成场景通过（正式审核正例是桩，仅证明运维门禁，未算1441），不改政策。按 **ORDINARY-CUTOVER-AFTER-V28.md** 切换，不再使用旧27配置/文档。完整证据+新鲜Native+空仓+源码一致后才安装精确40额度授权，启动时仍需fresh credentials/native history/guardian。
4. 已准备但未启动普通网站HTTP观察控制器 `grid-ordinary-live-v1.py`，9控制器+6本地HTTP场景分开通过（都是替身/合成时间，不算实盘24h/1441）；源码SHA与未启动证据 `grid-ordinary-live-v1-prepared-proof.json`。未授权不能运行，既有state不能重放；正式资格通过后按cutover文档启动一次。普通入口新grid走正常preview/create/start，实际24小时 `soak_observer --scope ordinary`；私有时间不计普通24h。原完整产品目标（方向/第二所/Linux/高级功能）未完成，不能标goal complete。

历史：`RESUME.history-through-v27-20261017.md` 与 `RESUME.history-before-resume-fix-20261017.md`。实时状态以前述进程/DB为准。

# 最新优先：第29轮（2026-10-17 18:31 北京时间）

目标active，用户已授权CLI/API使用不到200USDC独立账户验证、自主推进；不能通过CUA执行金融操作。普通grants=[]，旧套利8788不改，不引入跨套利账户互斥。

## 当前部署与真实状态
- runtime **92862** /web89365/watchdog13846，launchd管理，外部心跳/看护同instance健康。网站 http://127.0.0.1:8789/ 。
- GID **g-64c450b400694cdc**，RID **service-loop-20261017-v29**，start1493a38e-0294-40b9-b3ac-71b402c99fd4已成功5placed/0pending；pause b7e8404b-8911-421c-ab9e-83b326c15563已成功5撤净/0剩余，最新PAUSED/e2且正在resume核对。尚未宣称新恢复/完整循环通过。主helper **22089**；health49854 /Native79280 /diag44471 /finalizer83522 /只读断线取证43382 /临时防idle46487（绑定92862最多8h）/恢复成功后才exec只读soak等待51089。
- 政策 `grid-service-operator-policy-v29.json`：40总/20reserve、150敞口30秒、long immediate/GTC/5格/3cross、2.6间距、fresh市场及费用正价差检查。**新29政策**expires UTC2026-10-17T18:26:32.954413（中国次日02:26）；没有修改已结束28的政策或原deadline。原观察deadline_ms=1792254833660（UTC 2026-10-17T16:33:53.660000+00:00），最长6h，不延长，不重放start或IOC。
- 状态 `grid-service-native-v29.json`，版本/进程 `...-v29-version-proof.json`、`...-v29-processes.json`。先poll上述同handle和权威DB，不因观察超时/文件不变就重启。

## 本次修复与验证
第28真实refill卖成交时，unified账户读取期间被更新作废（HL_UNIFIED_OBSERVATION_CHANGED），旧riskGuard把它当永久风险暂停。现在只把该精确错误映射到既有RISK_OBSERVATION_CHANGED_WAIT：丢弃快照、零permit/零签名，同一未发送intent保持PLANNED，必须下次全新采样匹配才能发。没有修改账号快照matcher，也不缓存或放松Native证明；模式变化、外来DEX/非USDC、真实仓位不符和预算不足继续硬拒绝。
真实matcher并发失效离线重现旧版3fail/3pass：`grid-unified-observation-reproduction-tests.log`；修复后相关62通过，新测试6；完整 **1447passed/2skip/110warnings/79.40s**，log `grid-unified-observation-retry-final-tests.log`。空仓部署proof `grid-unified-observation-retry-deploy-proof.json`，bootstrap0且健康。
67个网格Python/适配器/入口/路由的source sha与当前源及1447归档逐项相等。最新301文件归档 `data/grid-code-checkpoints/grid-code-20261017T1030Z-unified-view-retry.tar.gz`，sha **2927c8406a9eca62b80fa07e09e362bc38d8b53e8b4c0a0917bf9bdfaf259d22**；archive/current size/hash与包版本核对通过。排除私有env/vault/data，含公开.env.example/tools；不是不可变运行镜像。proof `grid-source-checkpoint-unified-view-retry-proof.json`。

## 第28已结束，不能续用
**g-5c7d113dc62e4c3a CLOSED/STOPPED/e5**。手动带仓pause/resume成功；真实maker buy.0044@2433.1→refill maker sell.0044@2435.7（UTC09:52:11.985）已完成一次因果买卖，后者触发refill buy，但紧接RISK_HL_UNIFIED_OBSERVATION_CHANGED在09:52:12.336导致暂停，helper按既有规则正常平仓。没有service coverage generation或真实断线恢复；refill实成交仅1卖，不是4双向，不能做完整loop。
Native最终4fills/2stops/11DEX零仓零单、net **+.007382 USDC**，余额 **198.858943**。其中grid_pair_net-.003348为FIFO口径含较早IOC仓成本，不得改写成理论价差收益。原审计 `grid-service-native-v28-proof.json`，最后关闭备份 **data/grid-backups/grid-20261017T095334Z-9b3070e1 seq269**。当前29前57CLOSED/139executions/269events/open UNKNOWN reserved0。fresh Native预检 `grid-service-native-v29-preflight-source-proof.json`（复制28终局源，保留原28审计），已成功。
28主4811/收尾61143/取证97280均本轮terminal0；soak最长2449秒但含真实risk/epoch/状态异常，不是全程0异常或24h。stream-recovery-proof文件虽存在，内容 **actual_successful_stream_recovery=false**（closed_before_native_verified_recovery），绝不可凭存在就算通过。09:42活动快照22569d4d仍是活动副本，不能替代上面的CLOSED备份。

## 仍适用的证据与下一步
- 当前包含此前resume原子freshproof、pause最多三轮先查后撤、断线freeze有效lease只迁epoch不续期、RECOVERING/NEEDS_ATTENTION ACTIVE持续Native看护、freshproof前不临时RUNNING等修复。断线恢复仍须实盘复验；当前核心以1447为准，不重复为私有工具跑全量。
- small20 `g-c28822e3563f4019` .0001ETH Native IOC全残仓成功；fault23 `g-c9172805f2df4e0c` 真持仓+工作单，约6.977秒替换/138.769秒Native退出/142.039秒观察关闭，proof `grid-service-position-fault-v8.json`。范围匹配可复用，不能把它们当完整loop；旧27保护版本误退出不可复用loop。
1. 延续29同一22089：首次RUNNING且持仓即pause/resume，再等原>=4双向真实refill、本轮归零重建+再入场；尤其核对成交后新鲜采样继续补单而非误暂停。保留原费用/保护门槛，不通过改配置或主动撮合伪造。
2. CLOSED后83522自动Native audit→正式bundle审核→备份；候选只保存，不自动安装。严格检查actual成功内容而不是文件存在。
3. ordinary准备已迁移29，**ORDINARY-CUTOVER-AFTER-V29.md**。只读preflight当前应拒绝候选未就绪，旧28/27不能上线。`grid-runtime-ordinary-after-v29.plist`未加载；普通40授权须完整证据、fresh Native、源码一致及空仓，启动还核对credentials/native history/guardian。
4. 已准备 `grid-ordinary-live-v1.py`（未启动/没有其state）：经网站实际HTTP正常preview/create/start，固定请求，未知只查询原请求，独立普通scope实际24h双时钟检查，尊重用户pause/保留仓/改配置；9控制器替身+6本地虚拟HTTP场景单独通过，不算实盘/1447。授权安装后才启动一次，不能删旧state盲重跑；结束仍须独立Native审计和备份。

以下是至第28过程的历史，实时以此段和DB/实际handle为准。其他方向/第二所/Linux/高级产品目标仍未完成，不能标goal complete。


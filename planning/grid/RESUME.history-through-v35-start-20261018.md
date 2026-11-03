最新覆盖：第35原pause a70c1a23-d581-4457-8739-425efc076f7a succeeded（epoch2、cancelled4、leftover_open0、expired_intents1），暂停后对账ok=true/gaps=[]/drift=[]。原resume已提交且running，当前STARTING/ACTIVE/e3；不得重发resume或中途重启。343健康样本max heartbeat1113.228ms，guardian正常。held-management只读proof尚待恢复完成后正持仓Native核对，不能提前声称管理验证完成。原deadline1792315059392/政策到期10:12:34UTC不变。

最新覆盖（第35启动完成）：原start a8aa1bf1-f3ad-4e5b-9e5c-7f17ac601cc1 succeeded，placed5/intents5/pending0。初始IOC仅1个，实际filled .0044（plan .0132，允许按真实部分成交重建），建仓后recovery无缺口/漂移。原helper已发pause，最近PAUSING/PAUSED/e2，pause仍running；尚未确认resume/管理Nativeproof。原观察deadline_ms=1792315059392，不得延长或重复start/pause。main/finalizer仍独立active，当前无新故障事件。以下STARTING为历史。

最新补充：第35原start仍running，初始建仓前、初始IOC后两次recovery_checked均ok=true（后者backfilled1/gaps=[]/drift=[]），正在后续原挂单流程，尚不能宣称RUNNING或带仓管理成功。健康120样本max heartbeat1113.228ms。不要重发原start。

# 当前覆盖：第35诊断复验正在启动（2026-10-18 UTC03:14后）

第35 g-10ced5cc7fbb4c35，RID service-loop-20261018-v35，最近STARTING/ACTIVE/e1，原start请求处理中；不能重复下单或重启helper。entropy-grid-trial-v35.service、health/native-flight/stream/held-management/finalize-v35均已独立启动。精确政策data/run/grid-service-operator-policy-v35.json到期UTC2026-10-18T10:12:34.461963+00:00；仍40total/20reserve/3cross/ETH long/immediate/GTC/5格/2.6间隔/150-30，50累计上限，helper成功后6h原观察期限，不延长。若失败先查recovery_checked事件的精确gap/drift，再查原cleanup，不做无证据循环试单。

新诊断改动：每次recover在状态事务中写独立recovery_checked事件（原epoch、之前状态、是否被新epoch替代、ok、gap/drift代码、允许的HL符号原因），不保存HTTP正文/签名/密钥，不改变任何放行、恢复或风控门槛。即使后续cleanup对账成功，也不会覆盖原失败证据。新增2测试验证脱敏和跨失败/成功的留存。36相关测试通过；本机完整1501passed/2skip；Linux完整单次1488passed/0skip/113warnings，129.74s，报告grid-recovery-audit-linux-full.xml已复制本机。

新318文件归档data/grid-code-checkpoints/grid-code-20261018T0310Z-recovery-audit.tar.gz，SHA9996a6468ad2ad5c4b1617af1dcc5610669d8d340b988438a57d6c7f34018a02；空仓fresh11DEX核对+备份后已部署，证据grid-recovery-audit-install-proof.json。仍66个runtime文件，hash绑定grid-service-native-v35-version-proof.json；当前仅recovery.py/runtime.py增加诊断，未修复或猜测第34具体根因。第35用于有界取证，若自然全门槛通过则继续原loop，否则自动收尾，不给普通资格。

独立held-management-v35只读observer已要求当前owned_qty>0、Native持仓精确匹配、保护OPEN和原pause/resume成功，才可写held_management_verified=true；不能空仓冒充带仓。曾停止重建仅这个只读临时unit以加入正持仓约束，未动交易unit或重复RPC。更早空仓SIGSTOP仍不当持仓资格。普通grants=[]；公网https://taoli.coinfetcher.xyz/grid/已上线、42资源/认证/证书/旧根站不变验证通过；密码仅在本机data/run/网格网站登录.txt（0600），不要打印。

下面第34全关/余额是启动第35前的历史快照；当前资金/订单以第35账本和Native为准。

# 最新覆盖：第34已关闭并审计，暂无活动实盘（UTC2026-10-18 03:06后）

第34 g-ddbfe3ca7fed47a6 CLOSED/STOPPED/e2；原start 29205630-857d-429d-844e-fbe6b1a74cb8 failed INITIAL_POSITION_UNRESOLVED: initial position history is not reconciled。已实际买入0.0132ETH@2416.7，再由原cleanup 741817b5-b2ed-4a3b-90d7-64177c961924 succeeded/remaining0，卖出0.0132@2418.2。Native独立2fills/11DEX零仓零单，USDC198.709458，net-.008919（gross.0198/fees.028719）。main/finalizer已终态；Native审计与备份exit0。证据grid-service-native-v34.json/-proof.json/-finalization.json已复制本机。未通过资格；不要重启原v34或重试建仓。普通grants仍空。

这轮心跳始终约1秒以内，当前未见第33式guardian故障。新问题是初始IOC后的recover报告ok=false，但启动只抛通用错误，具体gaps/drift未持久化。不能把猜测的HL_UNIFIED_OBSERVATION_CHANGED当确证。下一步先补全原子、脱敏、逐次的初始对账诊断及离线复现；弄清具体缺口再允许下一次有界实盘，不在未知原因下重复试单。公网独立网格入口已正常提供认证访问，登录见本机private文件，旧套利未改。

下方是第34启动过程和已验证部署状态，STARTING描述仅为历史。

# Linux网格续接（2026-10-18，第34轮）

## 当前权威状态
- root@taoli.coinfetcher.xyz，独立目录/root/code/entropy-grid，项目Pendle_Proxy.pem；SSH始终IdentitiesOnly/BatchMode/StrictHostKeyChecking=yes。旧套利/root/code/entropy与entropy-console.service保留；本轮核对旧PID3343163，升级/发布过程中未更改其进程。
- 新第34 g-ddbfe3ca7fed47a6，RID service-loop-20261018-v34，最新STARTING/ACTIVE/e1，原start仍在处理。主任务entropy-grid-trial-v34.service；health/native-flight/stream/finalize-v34均独立systemd任务。请求结果未知先查原RID/账本/任务，不重复提交。
- 40USDC总额/20reserve，ETH long/immediate/3cross/GTC/5格/2.6间隔；服务保护150USDC/30s。新政策data/run/grid-service-operator-policy-v34.json仅批准本RID，50累计上限，到期UTC2026-10-18T10:02:11.839500+00:00；helper启动成功后原观察期限6h，禁止延长。若初始IOC未真正成交会原cleanup收尾。
- 普通GRID_REAL_DISPATCH=0，grants=[]。本轮私有测试不是普通实盘开放。正式同轮完整loop/持仓故障/小残仓/断线恢复/24h均未全过，不标整个产品完成。
- 本机runtime/watchdog/web仍disable+bootout，只有SSH tunnel；禁止本机启动造成双写。

## 已完成并取证
- 第33 CLOSED/STOPPED/e5，原main/finalizer终态success，Native审计和备份exit0；11DEX零仓零单，余额198.718377 USDC，净-.05605。证据grid-service-native-v33-proof.json/-finalization.json已复制本机。第34已新开，旧flat快照不能当当前资金状态。
- 第33的guardian HEARTBEAT_STALE age5305.967ms在保护错误前触发退出。五scheduler cancel后等待外部异步清理可以阻塞主监督，7个离线测试已复现并修复；尚无现场栈，不宣称唯一根因。旧worker保留槽位、只cancel一次、不与新epoch重叠，最多8个合作清理事件循环后返回。没有修改guardian3s/stale5s/Native30s门槛，没有伪造独立心跳。grid_main仅加慢阶段名称/耗时日志。
- 本机全量明确tests+review：1499passed/2skipped，113warnings；85针对性通过。Linux当前1486节点分批全通过/0skip：原全量1485pass，安全UMask0077把权限测试mkdir0755变为0700，故该fixture增加显式chmod0755后原预期拒绝验证通过，运行代码不变。聚合proof grid-supervision-linux-tests-proof.json，不冒充单次全量成功。
- 新317文件包data/grid-code-checkpoints/grid-code-20261018T0200Z-supervision.tar.gz SHA 0f8ee7d6353d9c00a1fd7abe1ba461575a965ab308dc82381c45ff4afed2ae34。服务全部停后安装，安装前fresh11DEXflat+正式backup通过；安装证据grid-supervision-install-proof.json，当前66个runtime文件hash绑定第34version-proof。服务均active，旧套利未动。
- 新版真实空仓SIGSTOP后11.411820s替换并消费恢复标记；grid-supervision-linux-watchdog-proof.json明确empty_account_only，不可充当持仓故障资格。第34之前已过冷却，预算有效。
- 公网独立网格站https://taoli.coinfetcher.xyz/grid/已发布。先独立nginx临时master验证，再增加单独map/location和Basic认证。HTTPS证书验证、匿名401/正确登录200/同源API200/异源403、42个JS/CSS资源改写均通过；旧根站响应hash及旧套利PID不变。第一次检查早于reload新worker就绪导致404，已自动回滚，再加有限就绪检查后通过。证据grid-public-release-proof.json已复制本机。登录仅存在本机data/run/网格网站登录.txt及grid-public-login.json，0600，不打印密码；服务器只有哈希文件root:nginx0640。

## 下步
1. 查询第34原start直到终态；独立Native确认持仓、保护OPEN、初始IOC仅一次，跟踪原带仓pause/resume。若故障，先查原账本/原cleanup，不重复订单。健康日志1s采样；慢监督阶段日志可定位后续心跳问题。
2. 同轮自然双向>=4refill、自然归零后的原生保护重建及再次入场、真实断线恢复均按证据判断。不要主动撮合/改区间/延长本轮补证。原helper终态正常stop_flat，finalizer只Native审计与备份，不自动给资格。
3. Linux真实持仓+开仓单的故障替换/退出与小残仓尚待当前代码/精确范围验证；不得用Mac旧秒数或空仓SIGSTOP替代。审核后才可按精确账户/凭证版本/ETH/方向/GTC/金额/150-30开普通网站新网格，实际连续24h及终局Native审计备份后停用linux跟进。
4. 新站可读和管理授权测试；普通开单仍需资格，不弱化门槛。不动旧套利。多市场/第二真实交易所/回测推荐及高级功能仍是产品范围。
5. 只在关键变化、故障、资金风险或需用户参与通知；无变化保持安静。已有15分钟heartbeat linux，不再创建。goal active，勿将整个项目标complete。

更早记录见RESUME.history-through-v33-20261018.md、RESUME.history-linux-bootstrap-20261017.md与RESUME.history-through-v31-20261017.md；服务器实际账本和原请求始终优先。

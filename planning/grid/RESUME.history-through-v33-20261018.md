# 最新：第33已关闭；调度清理阻塞修复回归中（2026-10-18 UTC01:59核对）

以上方本段为准，下面第33 STARTING记录是历史。服务器62个grid全部CLOSED；第33 g-6093f7663dcc4669 CLOSED/STOPPED/e5，原main与finalizer单位均终态success，Native审计与备份exit0。新只读原生核对11DEX零仓零单，USDC198.718377，proof远端data/run/grid-supervision-fix-preflight-flat.json。第33net-.05605，不能算资格通过；没有启动第34，普通grants仍空。

第33pause/resume期间先发生guardian HEARTBEAT_STALE（age5305.967ms），之后保护退出并平仓。五类scheduler在tick里cancel后await worker清理可卡住主监督心跳；7项离线测试已复现并修复。尚没有现场栈，不能宣称这是唯一根因。修复保留退役worker/account槽位、不重复cancel、不与新epoch重叠，仅给合作清理最多8轮事件循环；不等待外部I/O，不改guardian/Native保护期限。grid_main增加慢阶段名称/耗时日志，没有独立伪造心跳。

本机85针对性测试通过；全量明确收集tests及planning/grid/review/test_review_regressions.py，进程session74266运行中，log data/run/grid-supervision-retirement-full-local.log。上一全量错误来自不带目录收集到data/run的历史源码副本，并非测试断言失败，已改正确收集路径。新代码尚未部署Linux（当前runtime3336759仍旧65文件版本）；必须全量通过、核对flat和备份后再升级独立grid目录，Linux回归通过再实盘。旧套利当前PID3343163，已与历史不同，非本轮操作；禁止为网格更改/重启旧服务。

公网/grid/模板仍未安装，旧根站保留。后续先完成当前修复和Linux回归，再新RID/新有限期政策推进真实闭环、持仓故障、小残仓、断线恢复及普通24h。不得重用第31/33或以空仓故障替持仓证明。

# 最新：第32收尾通过，第33正在启动（以上为准）

第32 g-b84d8d60d70e48f7 已CLOSED/STOPPED/e4；正常stop_flat操作dd9e60cc-66ef-484e-b870-0554bc8d7de8 succeeded/remaining0，原主任务和finalizer单位均终态0。Native独立匹配3fills/2stop/11DEX零仓零单，USDC198.774427，net-.019946（gross-.00088/fees.019066），Native审计+备份均0。不是保护故障：create plan ref2409.2/initial .0044，activation ref2410.5/initial0，实时行情按规则没有触发初始IOC。后续真实GTC买成交0.0044*2、带仓pause/resume成功+新stop NativeOPEN已独立核对。初始IOC intents=0，因此不把32当完整immediate资格；取证grid-service-native-v32-held-management-and-eligibility-proof.json；原审计/收尾见grid-service-native-v32-proof.json及-finalization.json。

新第33 g-6093f7663dcc4669，RID service-loop-20261017-v33；runtime PID3336759，主任务entropy-grid-trial-v33.service PID3337399，另health/native-flight/stream/finalize-v33都在独立unit。最近STARTING/ACTIVE/e1，原start请求仍在执行，不能重复启动或重投POST。新政策到期UTC2026-10-17T23:31:20.896645+00:00，50累计上限/实际40总额20预留/3cross/GTC/5格/2.6间隔/150-30，普通grants仍空。harness改lo=mark-3.9（原-9.75）、宽度13不变，并在start前检查创建plan需预建仓、start后检查实际一个IOC且filled_qty>0；否则原cleanup收尾，不无效等待6h。用真实planner复现旧activation2410.5原qty0/新qty .0132，离线proof已存；交易核心65文件hash与32一致、未改引擎。33进程/version/preparation proof及私有helpers已在远端（重要证明已复制本机）。

下一项独立工作：新域名grid.coinfetcher.xyz与grid.taoli...均无DNS，现有证书仅覆盖taoli。已核实nginx1.26.3支持sub_filter，准备将独立站经HTTPS /grid/发布；deploy/nginx-grid-origin-map.conf + nginx-grid-location.inc刚创建，尚未安装/生效。方案有独立Basic认证、严格公开Origin、前缀资产/API改写、独立sessionStorage token键，后台仍loopback8789且原始交易代码/网站源码不改。先用独立临时nginx进程实际验证鉴权/跨源/前端模块图，再安装单独map/location并仅给原HTTPS server增加include，nginx -t通过才reload，不动旧套利服务或原root路径。登录密码只存本机private文件并经已验证SSH传hash生成，不打印聊天/日志。公共312包尚应补golden以及新nginx模板；后续最终包记录分批1486测试，不冒充单次全量。

以下是第32过程历史，当前活动任务以本段为准。

# 当前续接：Linux第32轮已启动（2026-10-18 北京时间凌晨）

## 资金与活动任务：必须先查真实状态
- SSH root@taoli.coinfetcher.xyz；项目 Pendle_Proxy.pem。参数 IdentitiesOnly/BatchMode/StrictHostKeyChecking 均 yes。独立目录 /root/code/entropy-grid。旧套利/root/code/entropy及entropy-console PID1198962保持原状。
- 实际Linux runtime PID3330715；3个网格systemd单位active/enabled，GRID_REAL_DISPATCH=0、普通grants=[]。私有第32政策已单独安装，不表示普通实盘开通。
- 已创建第32 g-b84d8d60d70e48f7，RID service-loop-20261017-v32。最近真实状态STARTING/ACTIVE/e1，保护intent ACCEPTED、另5笔PLANNED，start RPC仍在原请求执行中。不能因观察未结束再提交start或重启helper。
- 服务器主任务 entropy-grid-trial-v32.service；独立 health/native-flight/stream/finalize-v32 服务同时active。句柄见远端data/run/grid-service-native-v32-processes.json。主状态data/run/grid-service-native-v32.json，日志entropy-grid-trial-v32.log。直接查systemctl+SQLite+源状态，不凭state文件宣称进程仍活着。
- 新政策 data/run/grid-service-operator-policy-v32.json：独立RID，50USDC累计上限，实际配置40总额/20reserve，ETH long/immediate/3cross/GTC/5格/间隔2.6；服务保护150USDC/30s，止损按新报价约97%，未弱化任何门槛。政策原到期UTC 2026-10-17T23:16:41.189965+00:00；helper在start完成后设置原6h观察deadline，不延长。到期/异常按原request stop_flat收尾。
- fresh Native启动前独立确认11DEX零仓零单，USDC198.794373，证据远端data/run/grid-service-native-v32-linux-start-preflight-source-proof.json；之后已有新私有任务，不能继续把此启动前快照当当前资金状态。
- 本机runtime/watchdog/web均disable+bootout；只有com.entropy.grid.tunnel把127.0.0.1:8789安全转发远端8789。严禁本机恢复执行造成双写；不改旧套利8788。

## 已通过的真实Linux验证
- 完整当前1486个节点分批全部通过、0skip/0fail，data/run/grid-linux-complete-tests-proof.json以逐节点覆盖审核三份JUnit，不是一次完整进程1486通过。1406项原先通过；缺失的原始tests/golden/load_config.json补传后通过，余79项完成；低优先级10万事件性能compute11.418s超原10s，随后按生产默认优先级单项重测8.774s通过，未改阈值/代码。临时测试DB放/dev/shm，真实交易DB仍持久磁盘。日志/三份XML均远端保存、关键证据已复制本机。不要重复整套测试消耗服务器。
- 实机60关键测试通过；空账户真实SIGSTOP后11.384625秒替换并正确读取介入标记。不是持仓故障资格。
- 三服务模板Umask→UMask已修复，实际UMask0077；root UID匹配、实际pidfd当前内核打开/关闭验证通过。
- 主代码仍原protection critical_market_snapshot/重试/恢复修复。当前runtime65文件hash、实际包版本、3unit hash与心跳实例见data/run/grid-service-native-v32-version-proof.json；已复制本机。未来不能拿旧Mac67hash或新工作区改动冒充当前加载版本。
- 公共312包data/grid-code-checkpoints/grid-code-20261017T1604Z-linux-complete.tar.gz SHA 7bf89283d06f97af86c84bee0587c5d91bff02ae175bbb91e093cc06ef6f0818，远端逐项校验；随后仅补传原始tests/golden/load_config.json（最终公共包应补入此第313文件）。交易代码未因回归改动。

## 接下来实际动作
1. 跟踪第32原启动请求直到终态，独立验证Native保护/持仓/账本；已准备helper会自动带仓pause/resume，再等同轮自然双向>=4refill成交及自然Native重建/再次入场。任何保护缺口都使本轮不合格；不得主动撮合或延长本轮。
2. health 1s、Native-flight与stream observer都在独立unit。stream成功要真实成功恢复事件+新epoch+Native复核，文件存在或连接成功不等于断线恢复合格。finalizer只Native审计+备份，不复用Mac small20/fault23组合，不创建/安装候选。
3. 本轮终态后，仍需Linux真实持仓+挂单故障恢复/退出、tiny residual及完整正式审核；不把Mac秒数当Linux证明。失败31不可复活。精确范围审核通过才开普通网站新网格并实际24h，最终Native/备份。
4. 公网taoli根入口仍旧套利，nginx未修改；独立网格当前通过SSH转发访问。长期公网入口尚待实现独立认证与Host/Origin/TLS处理，不能裸露8789。可在自然成交等待期间推进网站交付，但不能在活动资金任务中改变其加载交易代码/政策/保护门槛或打断runtime。
5. 保留完整多币种/多交易所/高级功能范围，当前Linux基础与私有验收进展不代表整个产品完成。goal API已重新active；自动goal继续推进，不报旧SSH阻塞。另有当前聊天15分钟heartbeat id=linux（网格 Linux 实盘验收跟进），勿重复创建；只在关键变化/故障/需用户时通知，普通24h与终局审计完成后停用。

历史过程见 [Linux迁移过程](RESUME.history-linux-bootstrap-20261017.md) 与 [31及以前](RESUME.history-through-v31-20261017.md)。本机迁移前CLOSED备份data/grid-backups/grid-20261017T154718Z-37d8ce68 seq288/integrityok；当前资金状态以服务器真实快照为准。

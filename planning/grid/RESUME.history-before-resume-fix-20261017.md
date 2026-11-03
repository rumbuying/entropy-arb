2026-10-17 16:00（北京时间）第25已RUNNING/ACTIVE/e1，start58ac052a-3f37-4e78-aef1-24f05ed2cf8e succeeded，5placed/0pending，当前1初始成交。原本轮观察deadline_ms=1792245461633（UTC 2026-10-17T13:57:41.633000+00:00，中国约21:57:41），政策中国23:50到期，不延长旧试验时限。主6565/自动收尾96383/soak6704本轮30秒wait均live；soak已实际进入operator-trial采样（最近23samples/110.4sec/0issues），不算普通24h。
其余handles health82540、Nativeflight77216、diag23277、临时防idle87227，执行70319/site89365/watch13846，具体policy与新流程/spacing2.6/资金40-20/profile150-30见下文。下一步持续同一helper，首次真实归零重建后带仓refill即早做pause/resume，再等>=4双向refill正常收尾/自动Nativeaudit/review/backup；不能重新start/重复IOC或为了green降低门槛。

2026-10-17 15:57（北京时间）第25已实际部署并开单启动中：GID g-af49a651f1344072，RID service-loop-20261017-v25，STARTING/ACTIVE/e1，已1初始真实成交，尚未宣称RUNNING/全部挂单完成。执行70319/site89365/watch13846，version-proof25外部心跳/看护同instance且新鲜，53历史CLOSED/129executions/250events/open UNKNOWN预留0为本轮创建前基线，source9hash未变、108Python AST连续证明有效，普通grants=[]。
实际live句柄：主6565（40秒wait确认live）、health82540、Nativeflight77216、diag23277、自动收尾96383、soak等待器6704（等实际RUNNING才exec独立operator-trial观察，避免把启动期当运行异常）、临时防idle87227（-i -w70319 -t28800，系统断言已核对，proof v25-idle-sleep）。都不应凭statefile就重复start；先poll同handle+权威DB。新helper有费用正价差保护，当前perbase最差双taker净.40805，实际新创建使用fresh市场而非早先preview旧价格。
观察从start返回起算最长6小时，deadline持久化；政策UTC15:50:19.576644（中国23:50）到期，观察器8h，达到原4双向refill+已管理验证+Native确认+正仓条件提前收尾。管理在首次Native重建后真实带仓refill开始pause/resume，然后继续观察，不重复tiny/fault。资金40/20、profile150/30不变。before-v25基线GRID0/no private/heartbeat已保存。第24已正常CLOSED且新Native预检11DEX0/USDC198.882717，旧47235/10561已终局；不要续用旧RID。最新关闭备份031653Z-9bc928d7；普通入口仍未开放，完整资格未过。

2026-10-17 15:50（北京时间）最高优先：第24已在原两小时deadline自动正常结束，不再RUNNING。g-97f4ea0d005e4cb2 CLOSED/STOPPED/e2，cleanup85433476-58dc-4355-b2ec-afd6bafc2bf4 succeeded，helper47235/自动收尾10561本轮均已poll terminal0，不得重放旧RID/start。总6Native fills/2stops、11DEX零仓零单、net+.020593 USDC、结算198.882717。完整bundle拒绝LIVE_RELEASE_LOOP_RECOVERY_MISSING：refill2而非>=4，pause/resume未触发。自动备份grid-20261017T031653Z-9bc928d7 last_seq250 integrityok；未生成candidate/未开启普通入口。soak在正常STOPPING期间记录GRID_NOT_RUNNING/epoch/protection过期，不能说全段0异常或普通24h完成，最长原连续6898.9秒。Native保护本身没有失败退出记录。
正式service_release.verify组件已用24/20/23完整Native artifacts+fullflight+faultv8实际只读重放通过（1gen），proof grid-service-native-v24-full-components-proof.json；这不是完整LiveRelease.evidence通过。最新只读预检grid-service-native-v25-preflight-source-proof.json重新确认Native11DEX零仓零单，保留原24历史审计文件未覆盖。当前运行16522仍private24已过期，ordinarygrants=[]，准备空仓换private25。
第25文件已准备、政策已创建但尚未部署/未开单：RID service-loop-20261017-v25；40总/20reserve、long immediate/GTC/3cross/5格、150/30不变，间距2.6（旧4），fresh mark-9.75 rounded.1到+13宽。只读Native预览VALID/qty.0044；费用最差双taker循环净额仍正.001789876（不含初始IOC和资金费），proof economics25。每次create前用新鲜market.rules复核正价差，不合格零开单。最长观察6h（从start返回deadline持久化），政策8h到2026-10-17T15:50:19.576644+00:00，native/health/diag观察8h，health改1s取样。到期正常收尾，达条件提前结束，不能降低原4双向refill门槛。
新私有helper25在首次Native重建之后的真实带仓refill即pause/resume，然后继续观察至原4refill要求，不再将管理验证拖到行情次数达标后。实际代码离线success与pause失败场景均核对、失败正常stop且无resume，费后负价差拒绝，无真实RPC/密钥；proof grid-service-native-v25-offline-proof.json，不当作实盘管理已通过。before-v25 baseline已核对GRID0/no private，private-v25 spec准备好。收尾/只读review bundle/helper均已按25复制，不会安装grant。下一步空仓launchd加载25、绑定版本/健康、启动独立observer/主helper/自动收尾，再核对实际live句柄。核心执行代码未改，不必为私有helper重复1398全量或重验已通过fault23。

以下为历史，不把旧“进行中”当当前状态。

2026-10-17 10:22（北京时间）第24保护重建后的真实入场已Native独立核对：refill maker buy.0044ETH@2421.3，OID577233536014/TID1017090017021526，UTC02:20:17.491，Native startPosition=0，CID/side/qty/price/fee/crossed=false逐项匹配；发生于本轮Native保护确认之后。新stop577232950997在入场后的Native orderStatus仍OPEN。该买成交触发refill sell.0044@2425.3，intent9e3d5947-ebde-4c31-a3df-7052564b98a9，CID0x978dfbcc2070f4b70000000000000152/OID577241352180，Native OPEN，Sell/Gtc/reduceOnly=true/qty/price/coin匹配；Native接受距源买成交6405ms。proof grid-service-native-v24-post-rearm-fill-proof.json，audit85669已terminal0，只读无交易动作；观测非原子，不冒充最终全Native审计。
截至该核对已5fill、其中refill2（sell2421.3 +buy2421.3），本轮首次真实归零→保护重建→重新入场→继续补单链路完成，仍缺至少4个双向refill、pause/resume、收尾Native/组合审核。持续观察超过真实1小时、0issues，但operator-trial/ordinary=false/completed24h=false。不要把1小时或本链路成功当作全部资格。
保持47235/10561/44354同一轮原11:14:58deadline；不改配置或延长，不重跑通过的fault23/small20。普通入口仍关闭。

2026-10-17 10:17（北京时间）第24保护重建后新增开仓单顺序已独立Native验证：归零maker sell2425.3生成的refill buy.0044ETH@2421.3，Native OPEN，market/side/qty/limit/Gtc/reduceOnly=false/CID/OID完全一致；Native接受时间比engine first-generation confirmed_ms晚53780ms，源execution时间精确等于原stop自动取消/本轮归零started_ms1792202637252。proof grid-service-native-v24-post-rearm-entry-proof.json，audit23104已terminal0；只查原CID，无签名/新订单。该项证明是重建后恢复挂单，不是该买单已成交、不是完整资格；仍需后续真实入场/双向至少4refill/pause/resume/收尾正式组合。
当前继续同一47235/10561/44354，原两小时deadline中国11:14:58不变。不要把前54秒授权/风险额度等待当Native裸仓过渡：这时原生止损已确认，持续Native lease由正常看护刷新；首次immutable lease仅用于原窗口证据，不是当前最后lease。

2026-10-17 10:12（北京时间）第24运行中间点在线备份完成：data/grid-backups/grid-20261017T021011Z-4b543936，last_seq248/integrityok。独立只读核对已包含当前RUNNING/e1、4fills、1confirmed coverage、6ACCEPTED/1CANCELED/4FILLED、reserved40；opaque凭证与两政策文件size/hash/owner/private权限全部匹配。proof grid-service-native-v24-midpoint-backup-proof.json。配套源快照015726Z-ui-resilience已保存。注意此备份是活动网格快照，不是CLOSED或资格证据；恢复必须隔离只读并重新Native核对，不得直接启动恢复的私有RID或重复签名。
helper47235本轮45秒wait确认live，当前仍4fills/1refill sell、native_confirmed、0退出，持续观察约3118秒/0issues。继续等待本轮归零重建后的真实买入refill；原11:14:58截止不变。最终收尾仍由10561做Nativeaudit/bundle审核/新备份，本次活动备份不替代最终核对。

2026-10-17 10:07（北京时间）第24本轮首次真实保护重建成功且formal coverage_windows组件已在实际只读DB上重放通过：第四笔initial maker sell.0044@2425.3（UTC02:03:57.252）自然归零，旧Stop577206626534 Native reduceOnlyCanceled时刻1792202637252；原deadline1792202667252。新stop833b5f57-7f25-48d2-9fb0-99ea0f9b49c8/OID577232950997，独立Native OPEN在1792202648340（11.088秒），engine确认1792202650871（13.619秒），cap42.6976/150，immutable first lease与epoch/config/account/market完全匹配。原始unknownOid短暂可见性记录保留，只查原CID，不重发。
proof grid-service-native-v24-first-rearm-proof.json，flight固定前缀副本 grid-service-native-v24-flight-rearm1.jsonl 已600+hash固定；原完整flight继续追加。正式coverage_windows复用真实函数且通过1generation；本次仅从flight取旧stop Native取消的partial_native_stop_audit，不是完整native_audit组件（后者须CLOSED并匹配全部成交/11DEX）；formal_service_qualification=false。不得拿它冒充完整资格或替代结束后的fullflight audit。
总4fill、其中refill1 sell，目前自然归零后继续等待真实买入refill；仍缺>=4双向refill、pause/resume/退出与组合审核。保持原helper47235/原两小时deadline，不重复注入fault或tinyclose。未来small20/fault23复用同scope现证据。

2026-10-17 10:04（北京时间）第24首个真实因果买卖往返已独立Native核对：普通maker buy.0044ETH@2417.3(OID577208180990/TID1036589742255997)→refill maker sell.0044@2421.3(OID577220746817/TID884030462526941，UTC02:01:38.229)→新refill buy.0044@2417.3。两笔Native fills的CID/OID/TID/coin/side/crossed=false/qty/price/fee逐项匹配；新buy intentd8f41de2-c52c-4c06-aab8-1eb9c27c4530，CID0x978dfbcc2070f4b7000000000000014f/OID577231296443，Native OPEN，Buy/Gtc/reduceOnly=false/coinETH/price/qty完全匹配。因果key从源execution逐层精确绑定。proof grid-service-native-v24-first-cycle-proof.json，audit9851已terminal0。不能重放/重签该CID；此证明无交易动作、full_qualification=false，不修改FIFO账本或收益口径。
当前总3fill（IOC+maker买+maker补卖），其中refill实成交1、仅sell方向，仍缺双向至少4refill、本轮归零保护重建及pause/resume/闭合组合审核。主47235/收尾10561/soak44354继续同一轮，原11:14:58deadline不变；临时idle断言80997独立有界。普通grants仍空。Native只读核对额外22 info权重，没有扩大资金或补单规则。

2026-10-17 09:58（北京时间）已保存最新完整源码快照 data/grid-code-checkpoints/grid-code-20261017T015726Z-ui-resilience.tar.gz，sha256 0150843f7693c818d0cb1106258946bc9c455a84536bdd4615ac3880ea0726d7。包含294源文件（原284+2独立CLI/相关测试/订单展示模块/3说明），不含用户vault/.env/data；所有manifest size/hash逐项重读归档和当前源验证，包版本与1398基线完全一致。proof grid-source-checkpoint-ui-resilience.json。测试记录分开写明原核心1398与新增69/45/详情6专项，没有宣称新整包全量重跑；不是已安装的不可变运行目录，当前runtime仍工作区。
第24主47235/自动收尾10561/临时防休眠80997本轮30秒poll均live，当前RUNNING/e1、2fill、约2289秒观察0issues；尚无refill实成交或本轮归零重建。维持原11:14:58deadline，后续actual CLOSED→自动Native audit→bundle审核→备份，普通入口仍关闭。

2026-10-17 09:55（北京时间）第24仍RUNNING/ACTIVE/e1、2fills（初始IOC+2417.3普通maker买），卖refill2421.3已Native OPEN但尚无refill实成交；主47235/自动收尾10561本轮已poll live，持续观察约2088秒/0issues；原11:14:58deadline不变。不能为等待成交重启或重放start。
实盘UI只读检查发现并修复：行情读取阻塞账本/行情错误禁用全部操作。现在账本先显示、后续轮询不等行情；后台行情同scope只单请求，scope变更/销毁取消旧请求，迟到回包不覆盖新状态。详情失败仍禁用旧操作，操作预览与服务端检查保留。订单表区分初始建仓/因果补单/网格减仓/结束及残仓退出/止损，价格标题明确委托价或触发价。新增order_view.js，未改财务执行。
专项6 pytest通过0.49秒（wrapper含6 Node场景，不能相加），日志grid-detail-refresh-tests.log；实际8789页已验证初始/补单/止损及.0088ETH，隔离quote503有账本与原预览入口，detail503无操作，POST硬405。临时IAB6/7/8/9均关闭；隔离fixture69982已终局143，不能重启当实盘后台。proof grid-detail-resilience-proof.json，截图grid-detail-order-purpose.png/quote-failure.png；说明GRID-DETAIL-RESILIENCE.zh-CN.md。
额外将1398原代码快照sha核验后逐个比较108个原Python模块AST，全部一致；只有hlauthorization注释文本不同，新release_audit/soak_observer独立CLI均未加载当前引擎。proof grid-core-ast-continuity-v24-proof.json。当前没有待部署财务核心变化，无需为本次前端调整重复持仓故障；新UI专项不宣称在1398旧全量内。

2026-10-17 09:40（北京时间）第24真实新进展：普通maker买.0044ETH@2417.3，NativeOID577208180990/TID1036589742255997，时间UTC01:38:44.048，与账本逐项核对。引发refill卖.0044@2421.3，reduceOnly/Gtc，intent808388a0-7141-487c-a178-b589fc1fc81f，CID0x978dfbcc2070f4b7000000000000014e/OID577220746817，实际Native OPEN且所有字段精确匹配；源fill Native crossed=false、数量/价格/费/CID/OID/TID一致。proof grid-service-native-v24-first-refill-proof.json，单次只读audit69240已terminal0，不重跑/不重发任何订单。观测确认距源成交52.205秒；Native order timestamp差33768ms（两种口径分开，不宣称秒级补单）。
当前仍RUNNING/e1，总2fill（初始IOC+普通maker买），尚无refill实成交、归零重建及完整loop资格。主helper47235/收尾10561/持续观察44354继续，原两小时deadline中国11:14:58不变。另仅修正hlauthorization的旧WS免费注释，无执行逻辑变更；官方角色查询60/次，2roles+extraAgents共140，与本地权重相符，review proof grid-authorization-read-cost-review.json。不能把注释变化当新核心版本或提速已完成。

2026-10-17 09:36（北京时间）第24仍RUNNING/e1/1初始fill，无refill；helper47235经45秒wait实际确认live。新增只读阶段性能/健康汇总 grid-service-native-v24-startup-and-health-proof.json：start created01:10:51.697739→ended01:14:58.690496，共246.992757秒；5placed/0pending。采样5713次heartbeat最大1155.113ms/watch最大1275.846ms，实例失配与非healthy均0；300个预算诊断峰值877/900、中位600、未记录Native rate-limit origin。文件引用记录当时captured_bytes与prefixhash，后续append不等于旧前缀改变。此为阶段采样，不是连续24h/完整Native或服务资格。不要为启动等待或零新成交重放本轮；保护持续新确认，原观察deadline仍11:14:58中国时。

2026-10-17 09:30（北京时间）无人值守主机条件核对：当前AC供电，AC sleep0/battery sleep1。为本轮另加临时/usr/bin/caffeinate -i -w16522 -t7200，真实PID20539/session80997，pmset已确认它持有PreventUserIdleSystemSleep断言，proof grid-service-native-v24-idle-sleep-proof.json。只防空闲自动休眠，不防合盖/强制睡眠/断电；不改永久设置，执行PID退出或7200秒自动释放。不要误当第四LaunchAgent或交易故障看护；若未来runtime换PID，旧断言自然退出，按届时实际供电与仍活动网格判断是否另建新有界断言。
第24 helper47235本轮经30秒wait确认live，RUNNING/e1，当前仍1初始成交/0refill，持续观察约617秒/0issues。原deadline不变，不能重启/重新start或延长，普通grants仍空。目标active，主机条件核对不等同成交或正式24h通过。

2026-10-17 09:23（北京时间）接续：第24 RUNNING/ACTIVE/e1，仍1初始成交/0refill；campaign47235、finalizer10561本轮复poll确认live。独立Native working-orders proof已核对：5网格+1原生stop均真实OPEN、全部owned、OID/side/price/reduceOnly一致，采样book2419.0/2419.1，最近buy2417.3/sell2425.3，均未cross该采样book。此为非原子单次读，不替代最终审计或持续保护。data/run/grid-service-native-v24-working-orders-proof.json。
新增前端详情状态说明（STARTING/RECOVERING/PAUSING/STOPPED_WITH_POSITION/READY），解释启动核对/额度可能数分钟、各状态实际含义；仅文案和提示样式，不更改交易动作/核心。node --check通过，8789/static/v2/grid/detail.js返回200且逐字节匹配当前源；proof grid-detail-lifecycle-notices-proof.json。未做过渡状态实页渲染，不能声称视觉验收或已包含1398旧全量。本轮没有重启执行服务。
soak44354持续operator-trial（最近约206秒、0issues），不算普通24h。当前仍缺实际4双向因果refill+自有loop保护重建/pause/resume及完整组合资格。依次继续同一真实session，不能重复开第24/延长原deadline/重放旧RID。源码hash核心不变，新独立soak CLI45专项见上文。

2026-10-17 09:20补充：第24仍RUNNING/e1/1初始成交，47235/10561已确认live。独立只读soak_observer CLI新增，不被当前引擎加载、无交易所调用/密钥/交易动作；45项相关测试通过0.89秒，implementation-proof及GRID-SOAK-OBSERVER文档已存。实际当前试采样0issues/1sample/0sec，ordinary=false/completed=false。持续operator-trial观察session44354已启动，output grid-service-native-v24-soak-observation.json；试验记录不能替代普通入口24h。计时双时钟>=86400，采样gap<=15，health/epoch/实例/配置/lease/持久异常会累计；运行最多24h30，网格CLOSED等结束；不安装grant。核心1398部署未改，不能把新CLI45说成已在那次全量里。

2026-10-17 09:15（北京时间）第24已RUNNING/ACTIVE/e1，start8a5db1fa-55f4-4ef0-be9d-be3fdc64d14d succeeded，5placed/0pending，当前1笔真实初始成交。观察原deadline_ms=1792206898697（UTC 2026-10-17T03:14:58.697000+00:00，约中国11:14:58）；政策到中国12:09，不延长原观察期。不重放start。helper47235和独立observer本轮复poll确认live，finalizer10561live。接下来等实际双向refill+原生保护归零重建；不是已完成循环或资格。

2026-10-17 09:14补充：第24首次真实入场已成交，尚STARTING，不宣称已RUNNING。新增自动只读收尾helper data/run/grid-service-v24-finalize.py，session10561已poll确认live；只等待actual CLOSED，再顺序运行Nativeaudit24、完整review-bundle、paired备份。Nativeaudit失败就停止，不生成/安装grant；组合审核失败仍保存备份。finalization.json为执行中记录不是终局，最多10800秒。源码不变，当前仍沿用同一start/helper47235，不重放。

2026-10-17 09:12（北京时间）第24已真实创建并启动中：GID g-97f4ea0d005e4cb2，RID service-loop-20261017-v24，STARTING/ACTIVE/e1。新runtime16522/site89365/watch13846，helper47235（已复poll确认live），health85660/nativeflight49918/diagnostics16220均live启动；原23已CLOSED/Native零仓后优雅bootout/bootstrap，不改核心源码。不能重放start/新RID，先查同一handle和DB。
第24是两小时有界loop观察（从start返回后起算，deadline持久化），政策TTL UTC04:09:22.795711（中国12:09），40总/20预留、long immediate/GTC/3cross/5格、fresh mark-15至+5，保护150/30不变。观察器3小时，Native独立状态最多512条（原60不足长观察），diagnostics改5秒本地采样。完成>=4双向因果refill+native_confirmed+正库存后pause/resume再正常stop_flat，不重复tiny probe；未来small_close用20、独立fault用23。private24 small_close_probe=false，普通grants=[]/real_dispatch=false。基线before-service-trial-v24.plist已验证GRID0/no private/heartbeat，sourcehash与1398一致，version-proof24保留。
新增私有只读review-bundle helper data/run/grid-service-v24-review-bundle.py：等三GID CLOSED，精确核对身份/方向/执行/杠杆/覆盖profile及Native artifact hashes，调用真实audit_release全门槛；只在实际通过后生成未安装candidate（capital40），永远不修改正式policy/开启派发。当前实测因grid_not_closed拒绝，无candidate写入。收尾顺序：本轮实际CLOSED→Nativeaudit24→运行bundle helper→备份→审核/有限开放；未通过不得宣称资格，不重跑已通过fault/small除非受新代码影响。

以下09:08及更早条目为历史。

2026-10-17 09:08（北京时间）当前优先接续：第23持仓故障与Native审计正式组件均已独立只读重放通过。g-c9172805f2df4e0c CLOSED/STOPPED/e3，52CLOSED/123executions，open/UNKNOWN/预算预留0。真实.0088ETH+已归属工作开仓单，SIGSTOP后6.976809秒替换、72.127145秒RPC恢复、138.769秒Native退出、141.666秒DB关闭（观察142.039292秒），均在原180秒门槛内。2笔Native匹配、11DEX零仓零单、USDC198.862124、净成本.016392。proof data/run/grid-service-position-fault-v8-final-proof.json，绑定原fault-v8.json和native-v23-proof.json的sha256；不是完整服务资格。helper原ConnectionResetError/finished=false保留历史，最终状态由权威DB+Native独立确认；不能重放该RID/start。
执行现PID14879/site89365/watch13846，services status已核对，grid-local-services.json已同步。当前仍加载private23政策（TTL UTC01:36:20.903927），普通grants=[]/real_dispatch=false，不能把私有试验当普通入口开放。备份grid-20261017T010526Z-80100c8c。核心1398版本未变，另加只读审核CLI专项69通过。
加速执行顺序：不重复已通过的small_close20、coverage22和fault23；下一轮只集中补齐4笔双向因果refill成交、pause/resume，再组合审核并有限开放/24小时观察。当前无运行中网格；下一轮尚未创建政策或启动，必须先核对实时状态、Native清理与原授权边界。计划采用一次更长、固定上限的观察代替反复20分钟试验，观察器与政策有效期须同步，仍保留40/20、150/30和真实成交条件。不得凭行情不配合降低资格，不为文档/私有helper重复跑全量，不扩展新交易所/高级模式。本轮用户要求加快，不代表取消风控或完成原产品全范围。

以下记录为历史，实时状态以上述09:08条目为准。

2026-10-17 03:25（北京时间）接续当前22仍RUNNING，UTC19:24:30最近2fills（初始买/初始卖），没有refill实成交；Native原生重建第一代已在7.422/9.706秒真实确认，proof已保存。helper82174持续live，应等原观察deadline UTC19:28:46正常收尾或候选达成；不改配置、不重复start。其余Native/health/diag句柄26281/49986/64046与部署69743见后文。
新增独立只读发布自检CLI entropy_arb/grid/release_audit.py，69相关检查通过（tests/test_grid_release_audit.py +live_release/service_release），没有更改现有核心算法或运行中引擎。复用真实LiveRelease.evidence/Service verifier，SQLite mode=ro/query_only、一致快照、不加载密钥/客户端/引擎，空grant不能算通过，永远不打开派发；坏输入符号输出不回显。文档GRID-RELEASE-AUDIT.zh-CN.md，真实当前政策proof grid-release-readonly-current-proof.json为0grants/False。这里只做了独立CLI专项，不能说后加工具已在1398那次全量里。当前核心1398文件未变、无需为私有helper/文档重跑全量。
准备的v23持仓复验仍未运行/无policy：helper改为对称mark±20宽40（同5格、40/20、150/30、long immediate/GTC/3cross），以提高RUNNING后仍有真实仓位的机会；不是增加资金/改变保护约束。fault-v8另加每笔Native限价==manifest、Native杠杆/保证金模式==配置、0<=Native数据年龄<5s，已用成功v14 Native artifact重放这些新增谓词通过。额外版本PID/RID/sourcehash证明仍强制，见v23-plan.json。不得现在运行或对22注入，等22CLOSED/Nativeaudit/备份后再新政策部署。

以下最新第一代成功和核心部署信息继续适用，实仓/成交续接重查。

2026-10-17 03:16（北京时间）重要新进展：第22 g-f8a7747d67274abb已RUNNING/ACTIVE/e1。start67a83cc4-5792-4963-be25-052084106f6b，UTC19:04:38.235169→19:08:46.004359，247.769秒（统一额度后存在节流等待，不宣称启动秒级）。helper82174持续live，health26281/Native49986/diagnostics64046，执行69743/site89365/watch13846。正式20分钟观察预计UTC19:28:46结束，政策TTL19:47:47.954696。不要中断/重启/重放请求。
首次真实归零/原生重建成功窗口已观察：初始buy.0044@2402，initial maker sell.0044@2406.5，归零后旧Stop576992922466于1792177852359 Native reduceOnlyCanceled，原deadline1792177882359；新Stop576998575797（intent35be9fb6-cdb9-4da9-b275-960cc6333334）独立OPEN received1792177859781（7.422秒），engine confirmed1792177862065（9.706秒）。cap确认42.37376/150，原30秒期限未延长。首次不可变Native lease保存在coverage record确认事件，当前lease每2秒更新。proof data/run/grid-service-native-v22-first-rearm-proof.json +原Native flight-confirmation.jsonl。此是单个真实补挂窗口，不是完整服务资格；尚无refill实成交。refill buy.0044@2402.5已ACCEPTED等实际入场。截至UTC19:15:37仍仅2fills，last_rate_limit=None、cooldown0、weighted582/900，不能宣称已完成持续24h。
本轮另仅准备了新版本受影响持仓恢复复验（未运行、无policy）：data/run/grid-service-native-v23.py / v23-plan.json，RID service-fault-20261017-v23；fault helper data/run/grid-service-position-fault-v8.py。沿用已通过v7的严格Native持仓/stop/ownedworking gross/PIDbirthUID/epoch/watchdog校验和一次SIGSTOP，增加data/run/grid-service-native-v23-version-proof.json必须实际绑定RuntimePID/RID/当前sourcehash，否则不允许注入。当前22继续loop，不能把它改成fault或现在启动23。第22CLOSED+Nativeaudit+备份后，若适合再创建新TTL政策并部署版本证明；v23用于独立fault_grid，未来loop22/small20可能组成bundle，前提仍完整真实谓词通过。
1398核心无新编辑，无需仅因这些私有helper/文档再跑全量。最后备份grid-20261016T185948Z-54ec011d/源代码快照1398仍为第22前基线。普通入口关闭、grants=[]；完整loop/pause/resume/Nativeaudit及新路径故障/正式资格/24小时与原产品范围仍未完成，目标active。

以下部署及早期信息保留为历史，不沿用旧实时状态。

第22补充（UTC19:05:39）：GID g-f8a7747d67274abb，STARTING/ACTIVE/e1，helper82174已确认live，预算552/900、last_rate_limit=None、cooldown0；不能当作已RUNNING或已发生新成交。下一步只继续同一helper和SQL，不重启。

2026-10-17 03:04（北京时间）最高优先：第22 RID service-loop-20261017-v22 已真实启动helper82174；health26281 / Native49986 / diagnostics64046，执行69743/web89365/watch13846。GID待从data/run/grid-service-native-v22.json/权威DB读，不重放start。long immediate/GTC/5格/3倍cross，fresh mark-15至+5宽20，40总20预留/service150/30；20分钟正式观察从start返回起算，4双向refill+native_confirmed+净正后pause/resume/tinyprobe，否则normalcleanup。新政策TTL UTC19:47:47.954696（中国03:47）；普通grants=[]、普通metrics.real_dispatch=false。基线before-service-trial-v22.plist GRID0/无--grid-trial-policy/heartbeat，收尾要actual CLOSED +Nativeaudit22 +备份再恢复/部署，不重复旧请求/IOC。
1398核心修复已正式加载：HTTP/WS info统一保守900权重/60秒，350预留、原Native消息/连接限制和429退避保持。WS先获得容量，逐条发送前扣权重；未发送部分退款。额度耗尽零I/O，不HTTP回退绕过；风险/授权本地等待映射RISK_API_BUDGET_WAIT，保持PLANNED原身份、不签名、不暂停，Native429仍冻结。诊断last_rate_limit记录HTTP类型或WS客户端/Info类型，不含地址/正文。完整1398passed/2skip110warnings76.98秒、专项111，proof grid-ws-weighted-deploy-proof.json，sourcehash固定。当前没有待部署核心应用改动。
第21 g-03e9205505bc4c00启动后触发RISK_HL_WS_MARKET_RATE_LIMITED，自动暂停再helper退出；op e8ff8abe-0c54-4a06-a640-9b85db135123 succeeded/CLOSED，3fill/1stop/11DEX零仓零单，净成本.022579、结算USDC198.865059，helper56285终局。旧日志不含原始429来源，不能断言HTTP或WS来源/服务端内部共享权重；旧本地HTTP used331未计WS，不能据此说总用量低。Native单次WS计数0/快读证明保留，但现在明确是旧HTTP计数不是可持续吞吐/无查询成本。各WS文档已加更正。
第20 g-c28822e3563f4019独立最小残仓退出成功：probe op5e6c4ded-d258-4e1d-ad34-6864be2c662f，starting.022-stage.0219(两笔Nativefill)-tiny.0001，Native tiny reduceOnly/Ioc/filled/origSz.0001/sz0/startPosition.0001均独立核验。7fill/1stop/11DEX零仓零单，net-.248425，结算198.887638。Native audit component和small-close predicates真实重放通过；proof v20-small-close-proof.json和v20-proof.json，可供未来small_close_grid用，不能算loop/rearm资格。helper81513终局；其cleanup OPERATION_CONFLICT是与独立probe同一个进行中stop冲突，只观察后CLOSED，没有重复平仓。
最后完整备份grid-20261016T185948Z-54ec011d lastseq231，50CLOSED/119exec为第22前基线；代码快照grid-code-checkpoints/grid-code-20261016T190225Z-1398.tar.gz，284文件与SOURCE-MANIFEST逐一hash核验，proof grid-source-checkpoint-1398.json。只含选定源代码/测试/webassets/requirements及包版本，不含用户vault/.env/data；这是回退快照，当前运行仍引用工作区，未声称隔离不可变发布。
前端另完成详情7指标桌面3主+4副两行，移动390无横溢出，实页验证图片data/run/grid-detail-compact-desktop.jpg和-mobile.jpg、proof grid-detail-compact-proof.json；没有点击任何交易操作。此CSS/class变化在1394全量之后单独实页验证，1398全量已覆盖当前源码。Cua临时tab5已关闭、viewport已reset，无需再打开只是核对同一事实。
下一步复用同一第22live句柄观察实际持续查询/Native限流来源/因果循环/保护重建，不再靠WS通道绕过额度。避免重跑未修同一阻塞的试验。第14旧持仓故障证据保留，但新读/限速路径须核对受影响退出覆盖，不能直接把旧135.672秒当新版本全部证明。真实loop/rearm仍未过、24小时真实观察/原完整产品范围未完，目标active，无新用户授权缺口。

以下历史状态不得作为当前。

2026-10-17 02:40（北京时间）接续仍为第20 g-c28822e3563f4019，helper81513在本轮末已再次poll确认live。不要重放start、不热切换；按原20分钟观察预计UTC18:46:20左右结束后normal cleanup。当前状态/成交必须重新查SQL，不能依据旧2fill推断永久。
本轮新增强只读证据：一致账本副本+真实Native账户，独立DB无密钥/无签名器、/exchange硬拒绝，正式rt.preview(dispatch-risk)按当前0.0132ETH库存及原0.0044网格单位返回VALID，约3.872195秒、REST0，live_eligibleFalse；隔离副本guardian未配置，不能当作正式派发/看护或资格。proof data/run/grid-ws-risk-shadow-preview-v2-proof.json。首版helper未加载actor而调用grid_snapshot失败，原log/clone保留；v2只读plan修正，不涉及实盘库或私钥。pending1394核心源未再修改、不需要仅因文档/此helper重跑全量。
仅准备下一轮备用helper（未运行、未创建政策）：data/run/grid-service-native-v21.py +v21-plan.json。若20结束仍欠循环，待20实际CLOSED/Nativeaudit/备份并加载1394后，fresh mark区间mark-15至mark+5、宽20/5格，保持long immediate/GTC/40+20/service150/30；固定静态示例证明此布局初始仅1个cycle，使真实归零/Native自动撤销更易观测，仍需真实行情和同一完整资格验证，不把较容易成交当作通过。政策新TTL届时再创建，不得现在启动或改20配置。若20已合格，则无需跑备用loop，转验证新传输受影响的持仓退出/正式审核。目标active。

以下当前部署/句柄说明继续适用，实时状态续接再查。

2026-10-17 02:31（北京时间）最高优先：第20真实循环正在运行，GID g-c28822e3563f4019/RID service-loop-20261017-v20。执行63211/site89365/watch13846，helper81513、health58696、Native99664、diagnostics57337都为本轮真实句柄；同一helper已复poll确认live，不重放start、不因等待重启。start1ab0d953-2d64-4069-8557-9c63c6cc6c5b成功，UTC18:24:52.336619→18:26:20.557148（88.221秒），placed5/pending0；上轮166.258只是单次比较不保证常态。
当前RUNNING/ACTIVE/e1，初始买入0.0088ETH@2417.5 +普通maker买0.0044ETH@2415.1，净0.0132（续接查实时，不能沿用）。原生止损Native lease约1秒内更新。已自动提交/ACCEPTED一笔因果refill sell0.0044@2419.1，尚无refill实成交/双向4次循环/成功rearm证据。首次补单已独立Native确认OPEN，intent c1e54b0e-530e-4340-a068-463e55129497，proof data/run/grid-service-native-v20-first-refill-proof.json；该只读确认已终局，不重跑创建/签名。
第20参数五格、当前mark±10宽20、40总20预留、long immediate/GTC/3倍cross/service150/30，20分钟观察从18:26:20返回起算；满足4双向refill+native_confirmed+净正后pause/resume/tinyprobe，否则正常cleanup。政策TTL UTC19:08:22.662859，普通grants=[]/metrics.real_dispatch=false仅普通入口。基线before-service-trial-v20.plist GRID0/无--grid-trial-policy/heartbeat；结束要Nativeaudit20+备份再恢复/部署，不拷当前private20当基线。
第19经原rid-cleanup正常提前结束，为部署已测试全账户读取；op c8367a8c-00fa-423b-9ecf-0e12f901d004 succeeded/CLOSED，Nativeaudit19两fill/一stop/11DEX零仓零单，net-.01646，结算USDC199.136063。helper13836终局。实际最后备份grid-20261016T182300Z-c4e5ad50 lastseq218。
部署与pending必须区分：当前1391版本全账户criticalWS已加载，proof grid-ws-account-deploy-proof.json；源文件现另有待部署1394版本（完整1394passed/2skip110warnings78.95秒、专项130），新增实时meta/userFees批量及bound-grid风险预览criticalMarketWS，普通网页仍HTTP，8种只读info白名单不含签名/action。sourcehash grid-ws-risk-pending.json，文档GRID-WS-RISK-READS.zh-CN.md。完整规则/费率/行情/11DEX+现货输入Native只读两次3.381024/2.667076秒、REST0/0签名，不构成完整派发或循环资格。1394未部署，不中途切换第20。
第14旧版本持仓故障成功证据保留。此次新账户传输影响控制/退出读路径，旧结果不能单独证明新传输异常的完整覆盖；有限开放前核对并验证受影响退出路径，不能只因测试绿宣称全面完成。原完整目标及24小时实际观察仍未完成，active，无用户授权缺口。

以下历史状态不得作为当前。

2026-10-17 02:20（北京时间）当前：第19 g-8108dbc96c6c4baa仍RUNNING/ACTIVE/e1、只有初始0.0088ETH成交，无refill/rearm新证据。helper13836已重新poll确认live，health82018/Native77851/diagnostics29440复用，执行60046/web89365/watch13846健康。正常观察到UTC18:30:23左右后由同一helper正常cleanup；不能重放start、因观察超时重启或中途部署。政策TTL UTC18:49:47.960952，普通grants=[]。
有重要待部署核心改动：全账户critical只读WS批量路径已实现，完整1391 passed/2 skipped/110warnings77.12秒、最终专项118，24项新增含正式LiveRisk/Executor离线补单和错仓/外来单拒绝。源码hash与限制data/run/grid-ws-account-pending.json。允许6类无签名accountInfo，每批4，唯一ID/kind/type绑定，整批预校验，scope/429不回退，临时失败原HTTP额度回退；普通网页不走此通道。主账户4读一批，原有全部DEX/现货/模式及观测代次/风险/epoch/fingerprint不删减，无结果缓存，原REST签名发送不变。详情GRID-WS-ACCOUNT-READS.zh-CN.md。
实际主网只读完整快照两次成功，未使用stream：11DEX/现货/ETH，3.057182秒、2.285396秒，原生年龄2328/2269ms，REST消费0、0签名。Native证明grid-ws-account-snapshot-native-proof.json。只能证明两次快照，不是开单/补单/rearm资格。当前runtime仍原1367版本，不能把1391源码当作已部署。第19结束后先Nativeaudit19+备份，才部署这版。Nativeaudit脚本当前加载新Transport但只调原post，不涉及交易。基线before-service-trial-v19.plist GRID0/无--grid-trial-policy/heartbeat。第14故障成功证据保留，不重复；真实loop/rearm/smallclose仍欠缺，原完整目标active。

以下历史实时仓位/状态不能直接沿用。

2026-10-17 02:11（北京时间）第19最新：GID g-8108dbc96c6c4baa 已RUNNING/ACTIVE/e1。第19start e9451aa4-796c-4c7d-a80d-e5a1572f93dd，placed5/pending0；created UTC18:07:37.318707，ended18:10:23.576975成功（166.258秒，不能声称完整启动已低于秒级）。真实初始买入0.0088ETH@2411.6，当前仅1笔成交；5笔普通网格挂单已铺设，真实订单/最新仓位续接再查。原生整仓止损576936368894/CID0x978dfbcc2070f4b70000000000000119已独立Native确认OPEN，见v19-native-flight-confirmation.jsonl。helper13836仍live，health82018/Native77851/diagnostics29440复用；20分钟正式观察从18:10:23返回起算，不要重启或重复start。当前没有真实refill或成功rearm证据。
只读后续传输探针：grid-ws-account-path-native-probe.json证实userAbstraction/activeAssetData/frontendOpenOrders/perpDexs通过同一WS并发Info读取（4条约2.882秒）；grid-ws-account-extra-dex-native-probe.json证实xyz的clearinghouseState/frontendOpenOrders与spotState同WS（3条约1.354秒），均0签名、只传输能力证明，未接入账户风险路径或部署新代码。官方WS post支持Info HTTP请求，但不能据此绕过全组合风险、时效及观测代次检查。当前仍1367正式版本。

以下较早第19准备信息继续有效，实时成交/仓位不能沿用。

2026-10-17 02:07（北京时间）最高优先：第19轮已启动helper13836（真实live句柄），只读health82018 / Native77851 / diagnostics29440，执行60046/web89365/watch13846。RID service-loop-20261017-v19，GID从data/run/grid-service-native-v19.json/权威DB读取，不能重放start。40总20预留、ETH long immediate/GTC/5格、当前mark±20宽40（创建2%限制不变）、service150/30、20分钟观察从start返回起，4双向refill+native_confirmed+净正库存后pause/resume/tinyprobe，否则正常cleanup。政策TTL UTC18:49:47.960952（中国02:49），普通grants=[]/普通metrics.real_dispatch=false，私有19已加载。Native/health/diagnostics只读观察上限2400覆盖启动和收尾。
第18 g-03eafc3bd850442e因HL_POSITION_SNAPSHOT_STALE在initialize未生成订单/成交就拒绝，normal cleanup成功CLOSED，Native audit18完成：0fill/0stop/11DEX零仓零单，USDC199.152523/成本0。不能重跑旧helper。诊断确证actor规则加载误走public额度，连续观察排队25.105秒；该延迟不是stale的已证原因，失败原生时间未记录。三次后续Native只读数据年龄346–421ms，不能据此说永不stale。已增加拒绝时age_ms/roundtrip_ms日志，无地址，五秒/未来时间拒绝不变。
新修复已部署完整1367 passed/2 skipped/110warnings77.64秒、专项86。runtime._load_actor与私有创建的meta/userFees查询使用现有critical40，逐次新读/原总900及429冷却不变，网页继续等待；epoch新操作抢占。proof data/run/grid-actor-read-priority-deploy-proof.json，备份grid-20261016T180252Z-4b90aa2a lastseq215 integrityok。开始19前47网格全CLOSED/open0/unknown0，服务/心跳/看护健康。
收尾必须等19实际CLOSED，运行genericNativeaudit19；如partial须核对实际Native且正常stop_flat续接，不重放旧start/盲目重复IOC。基线恢复使用before-service-trial-v19.plist（GRID0/无--grid-trial-policy/heartbeat），不能复制当前private19为基线。故障14已通过不重复，全资格仍需真实loop/rearm/smallclose+不可变Native证据，后续有限开放和24小时实际观察。目标active。

以下历史状态不得作为当前。

2026-10-16 22:46（北京时间）最高优先最新状态：用户要求加快，聚焦实盘必需闭环，已通过故障不重复、扩展功能后排。第17 g-bc562c2e58264e20 零成交，原cleanup b920e71c-e1cf-4426-9a4c-f49842e295de partial/remaining0，本地止损OPEN；Native独立确认576686532107已canceled且零仓零单。正常stop_flat继续核对（新幂等key service-loop-20261016-v17-cleanup-finality-1，op7f18b675-e45a-4ca8-985c-0a6fbb2a498f）成功CLOSED。旧helper finished:false是首次partial的历史记录，不得重跑它。generic Native audit17完成：0fills、1stop、11DEX零仓零单、USDC199.152523、成本0。46个grid全部CLOSED/open_intents0/unknown0/预留0。最后备份grid-20261016T144454Z-42d4a361 lastseq214/integrityok。
授权WS优化已正式部署：执行46835/web89365/watch13846，原verified before-service-trial-v17基线恢复，GRID_REAL_DISPATCH=0/无private政策/grants=[]，RPC与新heartbeat/watchdog同instance健康。完整1363 passed/2 skipped/110warnings78.76秒，专项140；sourcehash/deployment proof data/run/grid-ws-authorization-deploy-proof.json。每次授权仍实时读取role/agent/validUntil，不缓存；批量2roles按ID绑定，独立socket，冷连接失败保留，有界HTTP回退。只读成功样本约0.892644秒不能代替完整签名/订单耗时。
下一步：复用故障14已通过证据，利用已部署提速版本补实际loop/原生保护重建/small_close缺口；新受限试验需要新RID和有期限专用policy，当前无活跃试验。不要恢复private17、重放旧start或因helper旧finished:false重跑。真实行情零成交不能算通过；同一阻塞没修复不重复跑；完整审核合格后有限开放并至少24小时实际观察。全产品目标仍active，不能标记完成。

以下历史状态不得作为当前。

2026-10-16 22:13（北京时间）当前最高优先：第17轮已启动，RID service-loop-20261016-v17，执行41832/网站89365/看护13846。helper13654、health11521、Native confirmation13915、diagnostics56323均已确认真实live句柄，必须复用同一工作/查看实际状态，不重新start。GID在data/run/grid-service-native-v17.json生成后读取，刚启动未必已存在。参数改为ETH long immediate/GTC/5格、当前mark上下5（宽10，每格2）、总40/预留20/service150/30，与16相比只扩大验证范围/格数，没有增资金或保护限额。试验private17 TTL UTC14:55:27.195367（中国22:55），普通grants=[]，metrics.real_dispatch=false仅普通入口。helper最多3次创建前价格范围过期拒绝重试，必须确认RID未分配且保留原配置，已分配后不改范围。观测20分钟从首次start返回计时；4双向refill+native_confirmed+净正持仓后正常pause/resume/tinyprobe，否则正常stop_flat。不是已取得正式资格。
第16 g-cfb9214fca344a2e 于14:06读到mark2460.2而固定区间2446.4–2449.4，持续零订单/零成交WAITING_RANGE。为减少无效等待，在权威SQL确认零订单/零fill且WAITING后，调用原rid-cleanup正常stop_flat（op20fc3482-e9f3-444e-b65b-74d0a128cfb6）成功；不是观察超时后盲目重启。generic Native audit16已完成：0fill/0stop，11DEX零仓零单，USDC199.152523、净成本0。helper67947、health28516、Native33730、diagnostics52003、audit72665均终局；不可重放旧start。最后备份data/grid-backups/grid-20261016T141200Z-d6566593 lastseq213完整性ok，45 CLOSED/107executions为第17前基线。
后台源码仍1353 passed/2 skipped/77.39秒版本，Core Unknown索引解析修复已部署（grid-rearm-index-deploy-proof.json），随后UI资格金额12相关+3node验证/HTTP字节一致（grid-qualified-capital-ui-proof.json）。没有待部署核心改动，也没有继续放宽时限/UNKNOWN重发。Native确认观察脚本从原窗口起对unknownOid持续查到OPEN/原deadline，即使durable状态暂未变，避免漏掉真实期限内独立证据；字段一致、只读、2400秒上限/60状态。
必须等实际第17CLOSED并跑PYTHONPATH=. .venv/bin/python -u data/run/grid-service-native-audit.py data/run/grid-service-native-v17.json，之后恢复data/run/grid-runtime-before-service-trial-v17.plist（GRID0/无private/heartbeat有），不能恢复当前private17 plist。若helper cleanup错误/超时但已有持久退出，观察同一个GID/操作，不重复start/签名。独立故障14已通过135.672秒及Native审计，不重做故障；完整grant仍需合格loop/small-close/各代NativeOPEN和不可变lease。未满足不能普通放行。原完整产品范围与24小时真实观察继续，目标active。

以下历史状态不能当作当前。

2026-10-16 22:00（北京时间）最高优先当前：第16轮 live helper仍在运行，必须查询同一真实句柄。RID service-loop-20261016-v16，GID g-cfb9214fca344a2e，执行38540/web89365/watch13846；最近WAITING_RANGE epoch1、零成交/订单（不是永久空仓结论，续接重查）。helper67947、health28516、Native确认观察33730、diagnostics52003。原Native长观察89798只读进程已SIGTERM，替代为confirmation脚本，未重放交易；初次观察脚本缩进错误在启动前py_compile捕获并已修复，33730为确认成功运行的句柄。Native脚本在原窗口内对unknownOid持续0.5秒观察，不因durable UNKNOWN状态未变而略过未来OPEN，可提供deadline内独立原始记录；文件grid-service-native-v16-native-flight-confirmation.jsonl，2400秒上限/60状态。底层网格不重启。政策TTL UTC14:34:05.612403，GRID env1仅私有16，普通grants=[]；metrics.real_dispatch=false仅普通入口。helper最多观察20分钟（首次start返回后），>=4双向refill+native_confirmed+净库存正再pause/resume/tinyprobe，否则到时正常stop_flat。第16创建前最多3次仅价格scope拒绝重试，确认RID未分配才更新配置；不重试任何未知交易。
第15 g-31bd39eabc2e4463已CLOSED：4Native fills、2止损、11DEX零仓零单，净成本.001784，最新USDC199.152523。没有refill实成交/成功补挂资格。原生旧Stop576633670500在UTC13:39:42.539归零撤销，30秒deadline13:40:12.539；新Stop576638101669实际创建于13:40:01.033（18.494秒），但submit ACK waitingForTrigger后CID lookup返回unknown，executor首次5次解析仍未知，rearm立即REARM_UNVERIFIED退出。独立unknown样本13:40:02–06，首次独立OPEN接收13:40:25.131已超deadline12.592秒；不能当作期限内确认，亦不能断言6–25秒间何时首次可查。新证据grid-service-native-v15-index-delay-proof.json及原Native flight long/Native audit保存。
本轮核心修复已部署并完整1353 passed/2 skipped110warnings77.39秒（专项60）。ServiceCoverage.rearm对同一个UNKNOWN保护intent在原始取消deadline内只查询解析，仍一次签名/一次Native order，不重建CID、不重发、不刷新期限；只有OPEN才继续完整确认；取消/ABSENT_FINAL/其他结果拒绝，deadline或新用户操作胜出。加原始剩余时长的monotonic等待上限；每轮fence核对guardian/当前策略/epoch/时限/敞口，卡住HTTP仍受独立supervisor退出。新增3场景：8次索引未知后OPEN、原期限到时、期间暂停，均不重发。proof grid-rearm-index-deploy-proof.json，最后备份grid-20261016T135010Z-8b24ccaf lastseq212 integrityok。后台源码全部已加载，无待部署核心编辑。
前端另修资格金额：qualifiedDefaults返回同一qualification的capital_limit，选择资格时若默认总投入超过限额则只降低金额并同步输入框，显示该币种上限；不增加用户较小投入，不混不同grant。12相关pytest和3node通过，独立网站HTTP字节核对一致；proof grid-qualified-capital-ui-proof.json。此项在1353全量之后单独验证，不能把前述full log说成含此后改动的再跑。
持仓故障第14单项通过仍有效：g-6f1ffa9da0754a3a、fault v7与-final-proof、Native v14-proof，两正式证据组件已独立核对；6.903接管/81.100RPC/131.128真实退出/135.435 DB CLOSED（观察135.672），旧失败v2/v5保留。第16若成功，future full grant必须以第14为独立fault_grid并另有合格loop/small-close及原生各代不可变证据；普通入口仍关闭，不能因读查询修复就发布grant。当前第16等待行情非用户授权阻塞，其他完整产品范围与24小时真实观察都仍未完。
收尾：实际第16 CLOSED后跑PYTHONPATH=. .venv/bin/python -u data/run/grid-service-native-audit.py data/run/grid-service-native-v16.json，再恢复data/run/grid-runtime-before-service-trial-v16.plist（GRID0/无private/heartbeat），不要用当前private16 plist；若runner cleanup conflict/timeout则观察已有持久退出，不重复start/重复签名。下一步只依新诊断修复/完成正向循环审核，不重做已经通过的持仓故障，不长篇猜测auth缓存/基础库存替代。目标active。

以下历史状态不得当作当前。

补充Native观察：data/run/grid-service-native-flight-long.py新增只读长观察，输出grid-service-native-v15-native-flight-long.jsonl，2400秒上限/仍60状态，覆盖原观察1200秒会早于helper最终deadline结束的问题。原Native13474保持原句柄，不重放交易；新长观察句柄3829已确认live，后续资格优先使用此原始文件（在首次实际保护重建前开始）。不与测试helper/Runtime交易循环混淆。

2026-10-16 21:35（北京时间）当前最高优先：第14轮真实持仓故障已通过，Native audit已完成，两组件独立重放均通过。GID g-6f1ffa9da0754a3a，fault v7 proof及-final-proof；SIGSTOP34205，.0107ETH、gross52.700924/150、原生Stop576613866281 OPEN，6.902957秒替代、RPC81.099783、Native退出131.128、DB CLOSED135.435秒、观察确认135.672311秒。2 Native fills匹配，11DEX零仓零单，净成本.040829，USDC199.154307。旧失败v2/v5不可改。第14 helper37423/fault20941/health23554/Native55842/audit26004已终局，diag59058之后需查同句柄确认，不能重放第14start。
当前已切第15只做循环：RID service-loop-20261016-v15，GID g-31bd39eabc2e4463，13:37:40时已从WAITING_RANGE进入STARTING，epoch2，最新成交/仓位须重新查询；执行35788/网站89365/看护13846。helper55319、health90404、Native13474、diagnostics15107均本轮已确认live句柄，不能因超时重启或再start。策略ETH long immediate/GTC/3格/40总20预留/service150/30，范围见data/run/grid-service-native-v15.json（创建前价格过期仅在确认没有RID已分配时最多3次刷新，并保留拒绝原始config；首次创建已成功，无重试）。最长观察20分钟从start返回计时，然后正常stop_flat；>=4双向refill+native_confirmed+净正库存后暂停/恢复与tiny probe。当前尚无成功重建或多周期证明。政策TTL UTC14:16:12.642701（中国22:16），普通grants=[]，metrics.real_dispatch=false仅普通入口状态。
代码仍完整1350 passed/2 skipped、75.13秒版本，已在第14前加载且第15相同，无未部署核心编辑。WS行情/STOPPED退出轻量对账/gross工作单故障验证见grid-ws-exit-deploy-proof.json与GRID-WS-EXIT-READS.zh-CN.md。退出轻量分支仅STOPPED；ACTIVE/PAUSED全账户不变。最后备份data/grid-backups/grid-20261016T133112Z-f392dd8a lastseq204 integrityok，43 CLOSED/103fills为第15创建前基线，不能当未来实时仓位。当前private15活跃，元数据已改RID/PID。
收尾必须等实际第15CLOSED，再用generic Native audit（PYTHONPATH=. .venv/bin/python -u data/run/grid-service-native-audit.py data/run/grid-service-native-v15.json），恢复data/run/grid-runtime-before-service-trial-v15.plist（GRID0/无private/heartbeat有）；不得恢复当前私有plist。第14fault网格已可作为未来bundle的独立fault_grid，但完整grant还需要新loop/small_close/各代原生OPEN/不可变lease+Native原始撤销时间，未齐不能普通放行。之后有限开放/24小时实际观察及原完整产品范围继续。不要重做已通过故障，不要长篇猜测缓存/基础库存方案；按实际新诊断处理。目标active，无用户授权阻塞。

以下历史活动状态不得沿用。

2026-10-16 21:25（北京时间）接续最高优先：第14轮实盘故障正在执行，必须先读同一日志/句柄，不能重复start或重启被测runtime。RID service-fault-20261016-v14，GID g-6f1ffa9da0754a3a，helper37423、fault20941（v7）、health23554、Native flight55842、diagnostics59058已确认运行。原fault v6观察器53339未控制且已SIGTERM，勿再用。已SIGSTOP PID34205，Native保护Stop576613866281 OPEN，实仓.0107ETH并有已归属工作网格买单，gross<=150。sent_ms1792157094459（UTC13:24:54.459），替代约6.903秒，最新61.56秒时RPC未恢复且grid STOPPING。watchdog13846/site89365未变，替代runtime以services status为准。fault结果文件data/run/grid-service-position-fault-v7.json；deadline从sent_monotonic起180秒，closed_seconds也<=180才能passed。
本轮新代码已全量1350 passed/2 skipped/110warnings75.13秒并在第14轮启动前部署。proof data/run/grid-ws-exit-deploy-proof.json，文档 GRID-WS-EXIT-READS.zh-CN.md。核心改动全部已加载，尚无后续待部署应用编辑。新增MarketInfoSocket只读公共WS post(metaAndAssetCtxs,l2Book)，每session/origin一连接/一inflight、本地300msg/min/10newconn/min/4秒含队列，ID/响应类型校验，不缓存，不含action接口；Native429阻断WS且触发REST退避；仅critical行情采用，UI仍原REST。临时不可用受限回退HTTP，错scope/429不回退；原5秒/保护deadline不改。真实只读probe在本地900/900时2.153秒取行情，盘口年龄527ms，2WSmsg/零签名；首次另有timeout_during_rest_fallback，失败说明保留在grid-ws-market-implementation-native-proof.json，不称总是成功。
恢复提速严格仅STOPPED的HL：RecoveryBarrier保留完整成交/未知/订单/归属/资金费核对，最后对账只用严格position_snapshot并校验经济账户；ACTIVE/PAUSED仍完整账户快照。Runtime该退出恢复预留200代替350；所有实付读取逐项计费不提高限额。新增STOPPED/PAUSED/错账户/过时Native测试。签名前全组合风险原路径保持。
第12轮 g-c5e6a4ae04dc4ced 已CLOSED、5Native匹配、11DEX零仓零单、净成本.069605，USDC199.189018。fault v3观察先到时未注入；不能说故障通过。第13首次48之前60宽区间超过试验2%限制，创建前拒绝且未分配网格；保存grid-service-native-v13-rejected-range.json后确认无RID分配再修正48重试，helper91142终局。第13 GID g-7a399191f117428e 已CLOSED，3Native匹配、11DEX0仓0单、净收益+.006118、USDC199.195136；fault v5真实SIGSTOP31069→32543用6.803778秒，RPC81.981275秒，Native退出139.843秒，CLOSED196.582秒，180失败原样保留。proof grid-service-position-fault-v5-final-proof.json及grid-service-native-v13-proof.json。第13 fault v4(97055)未控制、已SIGTERM，v5(12454)终局failed；audit69493已成功终局。第13不能借为成功资格。
故障覆盖真实活动网格的已归属工作开仓单（比只允许全减仓单严格测试更多在途风险）：v5/v7先排除UNKNOWN/DISPATCH/CANCEL/未发PLANNED，原生每单CID/OID/方向/数量/price/reduceOnly与当前grid journal绑定；保留intent_manifest和现仓+全部原始开仓挂单gross，<=原配置150才注入。service_release.fault增加该精确risk_profile分支重算gross与manifest；不带该记录仍要求所有原生订单只减仓。没有放宽180秒。真实第13失败经新verifier独立read-only重放仍拒绝。
当前private14 TTL UTC14:05:13.749120（中国22:05），普通grants=[]，metrics.real_dispatch=false指普通入口，不代表没有私有签名。第14参数long immediate/GTC/2格/40总20预留/范围2457.3–2505.3/service150/30。最后42 CLOSED时备份data/grid-backups/grid-20261016T132013Z-a442fc86 lastseq199完整性ok。最终一定观察当前grid真实CLOSED并跑Native audit（PYTHONPATH=. .venv/bin/python -u data/run/grid-service-native-audit.py data/run/grid-service-native-v14.json），恢复公共关闭基线data/run/grid-runtime-before-service-trial-v14.plist（GRID0/无private/heartbeat有），不能用当前私有plist。若helper因连接Reset或cleanup OP_CONFLICT失败，观察已有持久退出，不重放新start。
接续：先完成第14实际故障结果及Native独立核对；若<=180正向，再用新RID/政策验证成功保护重建与多周期。服务资格的loop/small_close网格须与独立fault网格不同；缺真实完整证据不可创建正式grant。要继续有限开放及24小时实际观察，全产品目标仍active，不以P0子集complete。不要再次长篇猜测缓存/账户查询替代方案，按新原始错误和实际耗时修复。

以下历史PID/余额/活动轮次不得沿用。

2026-10-16 10:33（北京时间）当前真实状态：本轮是实际代码/实盘证据进展，非仅等待。第10、11轮均已CLOSED且所有runner/health/native/diagnostics/audit句柄已终局；不要重放任何已用RID。40 CLOSED/93 executions/187events/open=UNKNOWN=预留0。最新独立Native proof data/run/grid-service-native-v11-proof.json：11DEX零仓零单、USDC199.258623。
执行PID54075/网站89365/看护13846，已恢复公共关闭基线GRID_REAL_DISPATCH=0/grants=[]/无--grid-trial-policy，心跳/看护同实例新鲜且healthy。当前没有活跃试验。元数据已更新。最后备份 data/grid-backups/grid-20261016T023146Z-60599c55 完整性ok,lastseq187；部署证明 data/run/grid-capacity-lock-deploy-proof.json。所有本轮核心改动已加载，无后续待部署核心代码。
已实现服务模式LiveRelease正向审核，不再永久硬编码关闭：本机私有grant可带service_evidence（fault_grid/fault_proof/native_audits/native_flight，私有绝对路径+字节sha256）；原审核仍保留，service新增至少4笔因果refills、全部generation闭环/Native原始取消时间/期限/敞口/每侧完整原生OPEN/首次不可变lease、全量原生成交匹配及独立持仓SIGSTOP接管<=30秒/最终CLOSED<=180秒。原生证据、账户/币种/密钥版本/配置hash/故障恢复事件与持仓重建交叉核对。缓存不能跳过。新事件保留generation初始敞口/确认lease。尚无实际bundle通过，service_mode_qualified=false，不能发布grant。报告 GRID-SERVICE-QUALIFICATION.zh-CN.md。
第10 g-f0027bd32c8a4e67：4原生成交，1笔真实sell refill，净成本.0084；正常退出请求02:09:44.582665→CLOSED02:10:07.210456约22.63秒；无归零重建资格。
第11 g-50ff20669d704b18，RID service-loop-20261016-v11：long immediate/GTC/2格/2437.1–2439.1/40总20预留/service150/30。3笔原生成交（初始IOC拆成.006/.005，.011普通sell主动成交2439.5），净成本.017047。Native Stop576144326269在02:19:07.234归零reduceOnlyCanceled，deadline02:19:37.234，第一次重建guard_before_account报价观察失败，自动退出，已有Native/coverage/health/diagnostics原始文件。曾used883/900，另有initial订单等350容量；旧guard未记原始错误，不能断言唯一根因。真实新保护OPEN仍未通过。
后续已修复：AccountExecutor容量预约移到签名锁外，避免一个等350额度的普通订单挡住已有160额度的止损/撤单；真正下单仍锁内、等待后重读epoch/intent并全风险检查。新增service_coverage_observation_failed保留generation/deadline/安全原始错误码/quote_ts，不放宽失败行为。新专项并发测试证明保护可执行、普通仍等待、期间换epoch后普通EXPIRED不签名。创建页从同一个qualification选mode/policy/service cap/seconds，切账户/币种重置旧保护参数；已HTTP核对实际静态文件。专项64、前端38；最终完整1326 passed/2 skipped110warnings73.85秒，node3场景和diff检查通过。详见最新部署proof。
接续立即推进：新RID/新政策复验持仓故障<=180秒（旧第8的190.233秒失败不得改写）；独立故障网格必须区别于最终loop/small_close；可在等成功循环期间先完成故障项。用新诊断再验证真实保护重建，必要时针对原始错误修复，不要重复盲跑/长篇猜测缓存或账户查询方案。普通订单阅读与保护预算争用仍需真实复验；此次锁修复不等于额度总量问题已解决。新增故障helper需修正v2截止时间从sent_monotonic起算，passed也校验closed_seconds<=180（旧脚本替代30秒后另算180不能作为正式通过）。之后用审核器对真实bundle审核→有限开放→至少24小时实际观察。用户授权持续，无需询问权限；全产品范围保留，不能以P0子集标记目标complete。

下面为历史，旧“当前”、PID和仓位不得沿用。

2026-10-16 10:00（北京时间）当前：用户再次要求加速。近期只推进Hyperliquid小额网格连续实盘、持仓故障复验及有限开放；24小时观察单独计时，原完整范围保留。最新执行PID46942/web89365/watch13846，已加载保护复用去冗余350预留、activeAssetData精确20权重、明确无需设置且已有完整配置证明时省第二遍激活预览。未证实/实际更改仍完整检查；恢复原数量不变。专项48，全量1291 passed/2 skipped，73.90秒，证明data/run/grid-activation-read-cost-deploy-proof.json。
第9轮已CLOSED零交易，proof grid-service-native-v9-proof.json。第10轮 g-f0027bd32c8a4e67 正在RUNNING，RID service-loop-20261016-v10；ETH long deferred/GTC/3格/范围2432.3–2438.3，总40/预留20，service150/30；政策到UTC02:29:26。已有两笔maker买.0073，各价2434.3和2432.3，合计.0146；两笔只减仓卖出补单已接受2436.3和2434.3，尚未补单成交/归零重建。Native整仓Stop576122640448，lease持续更新。不得沿用之前空仓余额。普通grants=[]，metrics.real_dispatch=false不代表私有试验无交易。
runner17229，health4298/native61336/diagnostics59055是本轮已确认句柄，查询同一句柄与实际文件，不重放start。helper最多观察20分钟后正常stop_flat；正向循环后才暂停恢复/tiny probe。结束需独立Native audit；CLOSED后恢复data/run/grid-runtime-before-service-trial-v10.plist（已核GRID0/无private/心跳有），不得用旧private备份。没有后续未部署核心源码改动。持仓故障旧180秒失败仍保留，正式service资格仍未通过。元数据已同步本轮PID和RID。

下面为历史，旧“当前”、PID和仓位不得沿用。

2026-10-16 09:24（北京时间）最新：已部署原生保护独立观察与本地期限。ProtectionWatchScheduler约2秒/侧2权重，只查已归属的完整Native OPEN，不给开仓授权；保存ProtectionLease，期限从查询开始计时，慢响应/总额度耗尽/HTTP挂起/迟到成交/暂停不能续期或漏判。确认窗口仍以原生撤销时间起算；新Stop确认和新lease同事务。重启先对有风险的活动service网格持久请求退出，再读账户。原生单独保护/空仓等区间不变。新增10场景，专项94，全量1286 passed/2 skipped110warnings75.07秒，diff检查通过。proof data/run/grid-native-protection-watch-deploy-proof.json，报告 GRID-NATIVE-PROTECTION-WATCH.zh-CN.md。
当前执行PID43052/web89365/watch13846，私有第9轮 g-fde5b36cf18845f2，新RID service-loop-20261016-v9；仅ETH long immediate GTC、2格、40总投入/20预留、service150/30，TTL见data/run/grid-service-operator-policy-v9.json（UTC02:02:55）。普通grants=[]，metrics.real_dispatch=false是普通入口状态，不代表没有私有验证授权。最新37 CLOSED/1 WAITING_RANGE，executions86/events174，逻辑预留40，未终局/UNKNOWN0；第9尚无成交/无订单。最后独立余额199.28407为第8收尾值，不能把未来新增成交后的余额沿用。
runner61389、health7518、native flight80392、diagnostics4872均在运行；必须查询同一句柄或实际状态，不得重放start。最多观察20分钟；多个真实refill/Native归零重建且有库存后才做暂停/恢复及tiny_close_probe，失败/到时正常收尾。原政策原始配置不可改来追价。最终空仓后恢复 data/run/grid-runtime-before-service-trial-v9.plist（GRID0/无private/有心跳），核对Native及记录后更新状态。新Nativewatch应用层代码已全部加载，当前没有后续未部署的核心改动。第8实际故障接管已有证据但180秒门槛失败；第9未做新故障、未得新资格。目标active，原完整范围保留。

以下是历史状态，不得当作当前仓位/授权/进程。

2026-10-16 最新收尾：第八轮 g-a8486ece8deb43a9 已 CLOSED；.0109ETH初始买2443.9、maker买.0109@2441.9、正式只减仓卖.0218，三笔Native匹配，净成本.050838/手续费.039938。独立11DEX零仓零单，USDC199.28407；37 CLOSED/86fills/173events/open=UNKNOWN=预留0。新90%数量策略实际双买后未发生旧饱和预算暂停，但无refill/归零重建，不授循环资格。
已实际SIGSTOP36810，Native成交重建证实故障时持仓.0218、原生止损576069347230 OPEN、无增加风险订单；独立watch13846不变，6.269877秒替代38526，74.455618秒RPC、132.684秒真实退出、190.233秒最终CLOSED。helper v2原始passed=false/closeout_not_confirmed_within_180s必须保留；原runner连接Reset且finished=false，后续自动收尾事实由final-proof/Native-proof证明，不可说180秒通过。所有runner/health/native/diagnostics/fault/audit句柄已终局，不能重新start旧RID。
由诊断533/900但20权重被550公共上限卡住，修复最终挂单查询执行预留；最终持仓使用严格轻量position_snapshot并核对账户/原生时效/数量/账本，保留退出前和IOC后的完整归属/历史/资金费核对。错账户/非零持仓不得释放预算；相关74通过，全量1276 passed/2 skipped110warnings74.47秒。备份 grid-20261016T005547Z-b07321e0 完整性ok。空仓撤下private8、恢复GRID0/grants=[]后加载最新代码，实际PID/看护健康/源码哈希见 data/run/grid-final-close-position-deploy-proof.json 和 services status；不能把前一PID38526当现执行进程。网站89365、watch13846继续。
审核仍需关注：当前缺失保护识别依赖Native查询，必须验证总读取预算耗尽时，未知撤销尚未被识别的阶段是否仍能满足原取消时间起算的过渡时限；仅执行心跳健康或最终空仓不足以证明此项。真实重建资格审核前必须给出该情形的明确处理及验证。
接续：先核对部署证明及实际服务，再新政策/新RID复验最终确认时长、真实Native归零重建和多个补单周期，之后正式服务模式证据审核/有限开放/24小时观察。目标active，没有用户授权阻塞，不能以本轮故障自动退出当成全部实盘完成。详情 GRID-SERVICE-POSITION-FAULT.zh-CN.md、GRID-OPERATING-HEADROOM.zh-CN.md。原完整产品范围未取消。

下文为历史，旧“进行中”、PID、余额和资格均不代表当前状态。

2026-10-16 最新：第六轮 g-8f65ba712194485e 零成交正常收尾，35 CLOSED；原生止损已挂好但确认查询超时，新增 executor.lookup 的关键读取额度（UNKNOWN仍只查）。第七轮 g-a1065d0b2cce4d1a 已 CLOSED，三笔 Native 匹配、净成本 .043713、余额199.334908；36 CLOSED/83fills/167events/open=UNKNOWN=预留0；11DEX零仓零单。未refill/未服务重建，风险暂停 BUDGET_EXCEEDED。真实价格/费用复算 ordinary19.924595 > 可用19.922774，差 .001821。已加新求量普通预算90%运行余量，原固定数量仍完整预算验证，不改风险门槛；网页显示余量。专项84、全量1273 passed/2 skipped110warnings73.65秒，node语法和diff检查通过。
已部署额外启动/退出关键报价、保护单160预留（代替无必要350）和暂停原始风险证据持久化；此前全量1265。proof grid-execution-read-closeout-deploy-proof.json、grid-operating-headroom-deploy-proof.json。当前正在第八轮 g-a8486ece8deb43a9，新RID service-loop-20261016-v8；2格、40总投入/20预留、service150/30秒，私有政策TTL见文件，普通grants=[]，不等同公共开单。执行PID36810/web89365/watch13846。runner句柄49575，health58105，native flight47355，diagnostics72517；必须先查同一句柄/状态，不能重复start。runner会限时收尾，多补单+确认重建后才暂停/恢复。最终应空仓后恢复 data/run/grid-runtime-before-service-trial-v8.plist（已验GRID0/无private/心跳有），不能借旧私有政策重放。首次读取市场历史/轻量检查不算实盘资格；目标active，保护重建/持仓故障/正式证据审核尚待完成。

以下为历史。当前若有交易，不能把上次已平仓余额当作实时资金。

2026-10-15 23:10（北京时间）收尾聚焦：用户明确要求加快，近期仅验收 Hyperliquid 小额网格连续实盘；顺序为真实归零保护重建/继续成交、持仓进程故障接管、精确配置证据审核及有限开放/24小时观察，暂停非必要扩展。新轻量保护持仓查询已实现：仅 clearinghouseState 权重2，无assets/evidence，严格结构/数量/重复币种/时效/账户核对；只用于准备保护、缺失识别和重建，所有新增风险仍全账户核对。专项50通过，完整1262 passed/2 skipped110warnings76.60秒；主网只读ETH0仓1.278秒，未新增交易。备份grid-20261015T150916Z-48812180完整性ok。部署结果以data/run/grid-protection-position-deploy-proof.json为准；计划详情GRID-FOCUSED-CLOSEOUT.zh-CN.md。最新交易状态34 CLOSED/80fills/161events/open=UNKNOWN=预留0，最近全账户原生核对余额199.378621。普通GRID0/grants=[]/无private，服务保护模式未通过、未做持仓故障，不能恢复普通开单或复用旧试验RID。目标active，无用户授权阻塞。

下面是历史记录，旧余额、进程号和“最新”结论不代表当前状态；续接先读取上述proof和services status。

2026-10-15 21:59最新：第二轮service g-2601b6817741433d真实自然入区多次校验后仍离区，零下单/零成交，正常结束；发现等待/保护行情使用public额度被550上限阻塞，新增critical_market_snapshot(22)，初始保护及重建account使用critical_account_snapshot(350)，网页不借执行预留且总额/429/计费不放宽。第三轮g-2084da4b942447e7实际.015ETH@2342.1+两笔.005@2344.2/2342.4，出现RISK_HL_OWNED_POSITION_MISMATCH暂停，限时流程正常.025@2335收尾；4笔Native逐笔匹配，fee.052621/net-.242121，余额199.398395。无refill/service重建，不具资格，尚未持仓故障演练。原生Stop575502044158入场OPEN、最终清仓同毫秒reduceOnlyCanceled；11DEX全0仓0单。失败与新成交时间接近但旧失败快照缺失，不能断言唯一根因；离线确证旧Native快照+新已归属成交竞争，新增账户owned_execution_checkpoint，仅OWNED_POSITION_MISMATCH且观察内归属成交和完整指纹均变化时DEFERRED不签名、不派发、无permit、保留PLANNED，下一次稳定匹配才可发；稳定数量差异/预算不足仍拒绝。专项61，完整1243 passed/2 skipped110warnings77.31秒，grid-owned-race-full-tests.log已终局；保护专项92。备份grid-20261015T135736Z-9067a8fd后空仓部署runtime8732/web89365/watchdog16704，GRID0/grants=[]/commission0/funding0，无private policy。32 CLOSED/76fills/151events/open=UNKNOWN=预留0。proof grid-owned-race-deploy-proof.json、grid-service-native-v3-proof.json、grid-service-native-v3-entry-proof.json；看护1442样本/最大心跳1049.889ms、检查1178.762ms/实例失配0。v2/v3 runner和health observer进程均已终局；state finished=true。旧v3policy文件尚保留但未加载，不能借它重放旧RID。下一步新有限政策验证修复后补单/归零重建，再持仓故障退出，补公开service证据审核器。见GRID-OWNED-SNAPSHOT-RACE.zh-CN.md和GRID-PROTECTION-READ-PRIORITY.zh-CN.md。全目标active，无外部阻塞，不能用局部测试当上线完成。

以下为更早历史，旧PID、余额及“当前运行”均以本段为准。

2026-10-15 21:31最新：第一轮service私有主网试验g-a83789bc1ef34faf已CLOSED，5笔真实maker买入各.005ETH（2377/75/73/71/69）及.025卖出2366.9共6笔Native逐笔匹配，手续费.035523、净成本.188023，余额199.640516。没有refill成交/service重建，不具新资格。NativeStop574906329900在最终清仓同毫秒reduceOnlyCanceled。30 CLOSED、72fills/142events、open/UNKNOWN/预留0；11DEX原生0仓0单。旧OperatorTrial.expire把所有资格丢失统记EXPIRED，03:04早退先于政策03:37到期，无法反推具体原因。已修复细分refusal并持久记录同一次guardian诊断，已STOPPED不重复干预、原清仓范围保留；专项94，全量1237 passed/2 skipped110warnings73.27秒。grid-trial-refusal-full-tests.log已终局。备份grid-20261015T133019Z-ea447e32后空仓优雅重载，当前runtime4510/web89365/watchdog16704，GRID0/grants=[]/commission0/funding0，无private试验政策。旧试验policy文件保留但已过期且未加载，不能重放旧RID/start。proof grid-service-native-v1-proof.json与grid-trial-refusal-deploy-proof.json；详情GRID-SERVICE-NATIVE.zh-CN.md。data/run/grid-local-services.json当前PID已同步。上轮临时浏览器页已关闭，截图grid-coverage-preview-ui.jpg；预算显示6位已实际核对。下一步带新原因审计的小额真实补单/归零重建及持仓故障验证，再补新的公开证据审核器；目标active，尚未完成。

2026-10-15 10:43：保护期额度求解、配置资格隔离、看护健康门禁及异常退出、创建/详情预览已部署，全量1233 passed/2 skipped。初始/库存风险模型按gross而非净额，恢复原数量不静默缩小；LiveRelease/OperatorTrial按cap/seconds精确隔离，旧hash不变，新模式公开资格仍SERVICE_UNQUALIFIED。看护同实例/PID、双时间戳<3秒、running/healthy、有重启预算且不在60秒冷却。网页150cap预览.00016BTC/143.12，RPC ETH119.568/150，均只读且资格false。部署grid-coverage-preview-deploy-proof.json，截图grid-coverage-preview-ui.jpg。该时点29 CLOSED/66fills/134events/余额199.828539，已被上段新试验结果取代。

以下为历史记录，旧PID及“仍无预览入口”均已被上段替代。

2026-10-15 09:58：独立watchdog第三LaunchAgent已部署并通过空仓SIGSTOP接管/自恢复实测。第二轮15933→16417，6.678秒重启/11.243秒RPC恢复；看护16353→16704，1.027秒。单用kickstart -k首轮失败，失败证据保留。全量1216 passed/2 skipped。详见GRID-WATCHDOG.zh-CN.md。

注意仅空仓本机进程故障通过，不等于混合保护持仓/API故障认证；整机关机/睡眠不受同机看护覆盖。新模式validate_config仍FEATURE_UNAVAILABLE，不能恢复普通开单。下一步把覆盖额度加入预览/数量约束、界面覆盖阶段和依赖，以及LiveRelease/OperatorTrial按service_coverage政策独立认证（不能借旧long/GTC资格），再做受限真实混合循环/持仓故障验收。全目标仍active，无用户授权障碍。

之前归零识别/原生重建已接入并离线验证；固定generation身份、UNKNOWN只查、deadline不重置、NativeHTTP挂起不阻塞退出，详情GRID-SERVICE-REARM.zh-CN.md。新的看护并没有解除该模式的资格门。

下一步独立看护/故障资格（不能把同进程supervisor计时器当成进程死亡兜底）、预览最坏敞口/时限、界面覆盖阶段，以及新service_coverage配置的独立实盘资格。旧资格不得借给新模式；直到这些及真实循环/恢复验收完成，保护validate_config的FEATURE_UNAVAILABLE门继续保留。目标active，无用户权限障碍。

上轮独立固定止损往返已证明候选失败（清理成功）：fixed_prearmed_ioc v2先挂止损574749635042，买入.00013BTC@81745（OID574750381953），只减仓卖出.00013@81710（OID574751227526），Native在归零时reduceOnlyCanceled该止损，主动撤单之前已查询确认。两Native fills独立匹配，净成本.014112/fee.009562，余额199.828539；证据grid-fixed-prearmed-native-proof.json。不得继续假设固定止损跨零保留。

下一步按原开发规格§9.2允许的服务端覆盖阶段实现混合保护：明确新配置的专用敞口上限/时限、预览最坏在途成交、持久覆盖状态、归零撤销识别、原生重建及失败自动退出、看护/掉线兜底真实演练。不能宣称无缝原生保护，不自动切换旧配置/复用旧资格。完整设计约束见GRID-FLAT-PROTECTION-GAP.zh-CN.md最新段；固定数量两方向空仓接受的历史证据仍有效，但已证明不能跨零保留。目标仍active，非用户权限阻塞。

上一轮保护缺口自动收尾、历史gap资格拒绝、状态提示及已结束详情不依赖行情均已部署，旧PID3667/1328过期。详见GRID-GAP-CONTAINMENT.zh-CN.md和GRID-FLAT-PROTECTION-GAP.zh-CN.md。完整连续性仍未修复。

先前浏览器关闭提示及disabled按钮实际通过，截图grid-live-halted-message.jpg；临时haltTab验收完成已关闭，仅关闭新增页，旧用户标签未动。新异常退出提示此次语法检查通过但尚未在实盘STOPPING时渲染验收。

# 2026-10-15 01:33 最高优先：修复空仓重入保护缺口

第二轮g-692191aae71a4bf1已收尾29 CLOSED，executions66/events132，预留/未终局/UNKNOWN0。Native0仓位/0挂单、USDC199.842651；13笔Native匹配，净收益+.088943、fee.037157，证明grid-short-native-v2-proof.json，但qualification_passed=false！完整refill/maker两侧都实际发生，发现保护缺口所以不得资格放行。

最后初始buy把仓位清零，Native整仓Stop574413598229 reduceOnlyCanceled；旧sell refill后来0.0046@2446.2实际成交重新产生无保护短仓。风险监控自动pause并取消，但取消过程中既有单仍可成交。Native原生撤销时间+随后重新开仓成交已证实，不是快照误差。正常probe buy.0045+tiny.0001最终清仓。watch64930已结束因NEEDS_ATTENTION，stop46965及audit5345均终局成功。Native原时点情况grid-short-flat-stop-cancel-proof.json。

安全关闭：原live政策保存在data/run/grid-live-policy-before-flat-gap.json600；当前livepolicy grants=[]，launchd runtime env GRID_REAL_DISPATCH=0，已删除--grid-trial-policy参数，metadata real_dispatch=false并记录halt reason。新开普通资格全部关闭，未恢复。执行新PID见services status（先1069，后只读重载又变）；网站89365。私有旧policy文件还在但没有加载且RID两轮均用完，旧policy已allocated80、TTL中国01:51。不能直接恢复旧plist（GRID1带private）或盲目开启policy。备份grid-20261014T172835Z-0c44ca65包含当前关闭政策+原审核政策。

手续费配对守恒已修改、测试并在只读重载后加载：pairing.py中间50位/最后补余额，支持历史SQL与同事务待写、超量拒绝；汇总100位。5场景完整1129 passed/2 skipped（68.32秒），之后旧28位/负费用第6测试也通过。RPC catalog新增real_dispatch，create显示实盘新开单关闭；API/live_release专项22通过、JS语法通过。还需浏览器确认新关闭提示及截图保存。

重点文件GRID-FLAT-PROTECTION-GAP.zh-CN.md。官方父单normalTpsl只在完全成交或部分成交后保证金取消时激活子单，普通部分成交/手动取消没有保护，不能用其名称当原子连续保护保证。资料已browse官方。简单自动rearm只能保护下一次发送，不能消除已经接受的开仓挂单在归零后成交的窗口。保留最小保护库存若采用须明确策略配置、预算/库存/收益/退出及真实复验；不得偷偷改数量/假装中性全支持。需完整设计修复并真实复验后才恢复旧普通资格。目标active，不应blocked；无用户权限障碍。

# 2026-10-15 01:07 第二轮空头进行中

g-692191aae71a4bf1 正常运行，private request_id native-short-residual-20261015-v1（此名只是政策预留RID，实际第二轮循环验证）。配置更窄2443–2451/5格、30投入+10缓冲，initial short.0046@2449.4，四普通卖出.0046，其中三个taker更优价和最后maker；随后两笔真实maker买入refill，普通maker两侧齐，尚等待sell refill成交。上方NativeStop574413598229。当前29个网格中28 CLOSED/1 RUNNING，不报告空仓。

只读watch句柄64930，最多1200秒，grid-short-watch-v2.log/json；开始时7成交，refill_sides=[buy]。stage start句柄64587先因detail-.0184与Native-.023差异失败，实际是查询间最后卖出成交；当前库存相符，并修正仅试验脚本用最新库存核对，start_submitted阻止重放，第二次只读核对85647已成功。驱动grid-short-native-v2.py stage start/pause/resume/probe/stop。

手续费配对守恒源代码已改但运行服务尚未重载：pairing.py最后份额补余额，读取先前与同事务待写分摊；ledger.pair_net_from_allocations和pair_summary用100位汇总。5新用例，相关15通过，全量1129 passed/2 skipped（68.32秒），log grid-fee-conservation-full-tests.log，测试50232已终局。当前交易仍旧模块，必须空仓后备份部署；不要把源码编辑当作已部署。

执行97911/网站89365，private policy仍UTC17:51:05.714894（中国01:51）到期，同政策累积已预留80（两轮各40），额度100/capital50不可再随意添加第三轮。下一步观察真实双向refill，完成pause/resume和持仓重启，再probe退出；若未齐仍不得放行普通short。剩余时间请实际查时钟。

# 2026-10-15 第一轮空头已收尾

最新28 CLOSED，Native主永续0仓位/0挂单、USDC199.753708；executions53/events116、预留/未终局/UNKNOWN0。g-bfc2dfe3a0cb406c初始short.0092@2430.1+三个maker sell .0046@2433/37/41，十分钟watch52683已终局无完整loop。pause成功撤5保留仓位-.023/原上方Stop574385739705；resume19838成功重挂5buy只减仓，初始未重放，但因内部bound .138→.161替换Stop为574402644753（先确认新再撤旧，原生时间独立核对），不能声称原OID未变。

probe句柄43364已成功：买回.0229，再真实reduceOnlyIOC买回.0001 startPosition-.0001；6Native成交OID/TID/价量费独立匹配。净收益-.230351，fee.040381。证据grid-short-native-v1-proof.json，阶段grid-short-native-v1.json。Native证明句柄48122终局成功，最新账户没有敞口。

方向资格基础已部署：实际普通政策仍BTC/ETH long GTC；private short政策仍在runtime97911启动参数，网站89365。第二个私有request_id native-short-residual-20261015-v1尚未使用；同政策已累计预留40，cumulative100/capital50，UTC17:51:05.714894（中国01:51）到期；若继续第二轮需先核对当前时钟/政策/空仓，不重放第一轮已结束脚本。后续实际双向refill+空头保护重启仍待完成，不能开放short资格。

最新全量1124 passed/2 skipped（68.22秒），后专项19（preview绑定）通过。这个阶段无引擎新修改，只有真实试验/证据。配对净费用分摊约1e-28尾差，账户实际费用与净P/L精确，后续可补分摊守恒。目标尚未完成。

# 2026-10-15 00:35 最新续作状态

方向独立资格已实现并部署：LiveRelease.grants/verified按grant_id键；同市场允许不同模式/政策且拒绝重叠；grant(config=...)按真实配置选择，context/preview绑定对应证据；permits_grid和资金增加按原grant_id；目录qualifications保留组合关系，历史合并保留较早合法起点。当前普通政策仍只BTC/ETH long GTC，未添加short虚假资格。完整1124 passed/2 skipped（68.22秒），后专项preview19也通过。

私有ETH short immediate GTC5/cross3实际已启动g-bfc2dfe3a0cb406c，区间2421–2441，上方SL2502，30投入+10缓冲。真实初始sell.0092@2430.1，Native上侧买入整仓SL OID574385739705；五笔普通挂单。已两个maker sell.0046@2433/2437，形成买入补单2429/2433 ACCEPTED；尚无双向refill真实成交。只读观察进程exec句柄52683正在跑，最多600秒，输出data/run/grid-short-watch.log/json；如句柄有效继续等，不重启观察。若结束因网格改变立即查实际状态及收尾；试验尚未完成。

执行97911/网站89365，launchd执行plist追加--grid-trial-policy=/Users/alex/Work/entropy/data/run/grid-short-operator-policy.json；原plist保存在data/run/grid-runtime-before-short.plist。私有政策schema1、capital50/cumulative100，request_ids short-loop/residual20261015-v1、仅short immediate GTC、small_close_probe true，UTC2026-10-14T17:51:05.714894到期（中国10/15 01:51）。原scope到期cleanup保留。加载时bootout后bootstrap首次5输入输出错误，确认旧PID消失/配置合法且job未加载后再次bootstrap0成功，未PID+Popen。备份grid-20261014T162158Z-a1ca6ce5包含政策、原DB107事件。

实盘驱动data/run/grid-short-native-v1.py，PYTHONPATH=.执行stage start/pause/resume/stop/probe；creation/start_submitted持久标志，勿新RID重开。start句柄20067已结束成功，raw Native与config在grid-short-native-v1.json，公开实盘资格false。后续需要实际循环、pause/resume、原生保护与完整tiny买入退出，方可审查short资格。当前持仓不能用旧27CLOSED状态报告空仓。整体目标active。

# 2026-10-14 23:10 最新续作状态

真实K线图表缩放/拖动/键盘复位和最近200笔本格真实成交标记已实现、测试并实际页面验收。新增 candle_viewport.js；candle_panel.js事件/有界视野/标记读取，widgets.js真实按时价三角/缺口空窗、detail.js观察标记；style.css工具栏。10项实际JS场景经Python调用；完整1121 passed/2 skipped（69.17秒）log grid-chart-navigation-full-tests.log。旧exec句柄58449已消失，现日志明确完整成功，不重启测试。

浏览器原页失效，曾创建隐藏IAB标签2验证g-164c63788c4a4b68，验收后已关闭仅此临时标签；旧标签1未动。1m120根/2成交→放大80根/0成交→较早80根/2成交；实际拖动、Home复位120根通过，截图 grid-chart-navigation.jpg、DOM证据 grid-chart-navigation-browser-proof.json。前后27 CLOSED，executions47/events107不变，未交易、未重启执行。执行90592/网站89365运行；当前余额以主网最新查询为准，上次实盘结束余额199.984059。

下一步继续更多方向认证与专业图表/长历史/高级策略；整体目标尚未达成。不要重启已结束交易脚本。

# 2026-10-14 21:43 最新续作状态

真实持仓强制退出演练完成：g-164c63788c4a4b68，30投入+10缓冲，ETH long immediate GTC2/cross3，建仓0.0118；确认原生SL OID574189579991后SIGKILL90119，launchd自动启90592，约129.675秒后RPC恢复RUNNING/ACTIVE。中断中和恢复后止损OID未变，初始CID/意图和成交无重放。随后普通stop_flat成功、27 CLOSED、未终局/UNKNOWN0、预留0、executions47/events107。

两笔实际Native成交OID/TID/价量费及startPosition0/.0118独立匹配，净收益+.025569，fee.026351，USDC199.984059，主永续0仓位/0挂单。Native证据 grid-active-restart-native-proof.json；阶段完整原生对象 grid-active-restart-drill.json；14CSV grid-active-restart-export.zip。持仓备份 grid-20261014T133559Z-b56ba963，勿盲目恢复其运行状态。执行90592/网站89365均launchd；最新完整测试仍1121 passed/2 skipped，此轮只有真实验收脚本和文档，没有引擎改动无需重复全套。

脚本 data/run/grid-active-restart-drill.py 用PYTHONPATH=.调用。曾修正脚本读取order_intents不存在exchange_order_id字段；只重复只读核对，start_submitted持久标志避免重复启动。export曾修正清单status路径为manifest.grid.status，最终14CSV有效。完整目标尚未完成。下一步完善图表缩放拖动与成交标记，再继续更多方向/长期故障验证；不要重复已结束的真实启动脚本。

# 2026-10-14 21:27 最新续作状态

恢复时限和公平调度已开发、1121 passed/2 skipped（69.35秒）并部署。执行90119、网站89365，资格核对两市场；26 CLOSED、未终局/UNKNOWN0、资金预留0、executions45/events103。实际服务无响应演练 static200/api5035.006秒，同PID恢复，指标不变，审计 grid-runtime-unresponsive-drill.json；主网最终0仓位/0挂单见 grid-recovery-native-flat-proof.json。

备份 grid-20261014T132311Z-6a631dda。新增 tests/test_grid_recovery_deadlines.py 六项，单项读取90秒、lookup_intent10秒及失败账户公平重试，不跳过UNKNOWN核对。全部CLOSED的SIGSTOP演练不代表持仓断网/整机断电验收。后续继续真实持仓故障与图表缩放拖动成交标记/更多方向。目标尚未完成。

# 2026-10-14 21:16 当前续作状态

真实 K 线已开发、测试并部署；主网 BTC/ETH 公共查询各120根，真实运行服务 ETH 1m/15m/1h 各120根。开单与详情使用独立读取、30秒有界缓存，无定时 K 线查询；120根OHLCV、成交量、缺口/未收盘、行情优先/完整网格视野。实际详情1h切换通过，截图 grid-candles-detail-chart.jpg。

完整回归1115 passed、2 skipped（67.57秒）；之后视野改动另19 focused passed。执行PID89367、网站PID89365均launchd running，26 CLOSED、0未终局/UNKNOWN、资金预留0、executions45/events103未变。备份 grid-20261014T130908Z-2ddeeef9。原有两市场资格启动已复验。K线仍是最近窗口，缩放拖动、成交标记、完整回测/推荐未实现；后续继续故障演练及图表能力。

# 当前续接入口（2026-10-14）

优先读 [当前实盘范围](LIVE-READINESS.md)、[实现状态](IMPLEMENTATION-STATUS.md)、[使用说明](GRID-LIVE-USER-GUIDE.zh-CN.md)、[收益保护验收](GRID-PROFIT-PROTECTION.zh-CN.md)。下文保留历史阶段，旧进程号/余额/未放行结论不代表当前状态。

最终部署执行87184、网站75852；BTC/ETH资格有效，26网格CLOSED、未终局/UNKNOWN0，14CSV真实观察导出通过，截图grid-range-observation.jpg。

最新等待入区间：五秒行情、币种及规范化边界核对；并发2、同账户单任务/公平轮询，超时20秒，暂停取消查询且不误取消自身启动epoch。真实ETH g-1222a91692994849 WAITING_RANGE、placed/intents/库存0，新鲜2508.7记录RANGE_OUTSIDE，随后普通结束。独立主网0仓位/0挂单，USDC199.95849，费用0。共26网格CLOSED，45成交、事件103；最新全量1096 passed、2 skipped、109 warnings（77.27秒），相关39和等待/导出15通过。详情显示历史观察，归档新增observations.csv（14CSV）；结束备份grid-20261014T123023Z-eb08a547完整性通过后launchd优雅加载。未自然入区间，不据此宣称真实触发完成。继续其他方向/策略及部署，目标active，见GRID-RANGE-WATCH.zh-CN.md。

最新预览优化：输入合并为单个请求，只续接最新参数，后台不新增计算；过期后保留旧结果并标记失效，点击更新预览才重算。实际139秒过期闲置仍只有1份，同参数手动更新只新增1份；Node5项、观察/网站等16通过，全量1087 passed、2 skipped、109 warnings（74.92秒）。浏览器定时器receiver兼容已修正并回归，收益止损输入移除min=0。详见GRID-PREVIEW-QUEUE.zh-CN.md。静态资源无需重启，本轮没有交易。

BTC/ETH 已审核做多 GTC 普通入口可用；新服务端金额/初始分母ROI止盈止损已接入参数、实际风险检查、暂停监控、持久退出和详情。真实ETH g-ace39310f3ff4a8d 预建0.0114后收益率止损自动触发，正式退出后CLOSED；原生成交与账本全部匹配。净成本0.041631，最新独立USDC199.95849、主永续0仓位/0挂单。第一轮 g-f9f4aa411d8d4731无成交，已正常结束，不算收益触发。当前25网格CLOSED、预留/UNKNOWN/未终局0，45笔成交、事件102。

全量1086 passed、2 skipped、109 warnings，75.84秒；相关专项51 passed。随后JS预览增补也语法通过。触发中断初始启动的结果改为真实状态及protective_exit，而不固定冒报NEEDS_ATTENTION，重放保留结果。空仓备份 grid-20261014T114424Z-febf95ae 已验证，再由launchd优雅重载。当前管理必须使用launchd标签，不能旧PID kill+Popen；实时查询services status。实际审核/普通创建范围保持BTC/ETH；高级条件/移动、其他方向/交易所、长期运行和服务器部署仍需继续，目标active。

---

# 网格后续开发入口

2026-10-14 11:16 北京时间续接：所有网格CLOSED，两个macOS LaunchAgent仍在运行；Runtime已通过launchctl单独重载为74360，网站73276未改变，BTC/ETH两市场资格保持正常。新增后端Decimal累计ROI及详情卡，分母初始资金+累计规模增加+成功缓冲追加，释放/关闭不重置，重放不重复计入；有仓位估算/覆盖缺失待核对。实际普通ETH结束网格HTTP验证分母40、净收益-0.02679、ROI-0.066975%；全量1062 passed、2 skipped，专项17通过。当前只做简单ROI，不冒充时间加权收益或冻结分母的保护触发；后续条件/高级触发未完全接入。下一步核对详情页面截图，并继续其他方向、交易所与高级功能，目标active。服务管理必须使用launchd注册标签，不能手动PID kill+Popen。完整旧事实与审计见普通实盘/本机服务/ROI报告。

2026-10-14 多币种推进：同一账户的ETH正式引擎已真实启动，完成0.0118 ETH建仓、两笔网格挂单、原生整仓止损，以及0.0001 ETH完整小额只减仓退出；全部撤净CLOSED，主网仓位/挂单0，USDC余额200.03119，本轮成本0.011237 USDC。修复小额退出误拒合法兄弟币种仓位，改为核对全账户准确归属及数量，外来/不支持/变化的持仓仍拒绝；全量1027 passed、2 skipped，空仓后部署。BTC普通网页入口继续可用；ETH须取得真实补单循环和恢复证据后再开放资格。详见[ETH主网验收](/Users/alex/Work/entropy/planning/grid/GRID-ETH-NATIVE.zh-CN.md)。继续多币种并行、其他方向和部署验证，目标active。

2026-10-14 最新：普通网站实盘创建已开放并验证。当前已保存账户的BTC主永续支持做多、GTC、立即/延迟建仓、1–3倍全仓，单网格授权上限200 USDC，实际资金/预留/风险仍逐次核对；其他币种/设置保持只读，不绑定账户币种。资格由第十四轮真实循环/恢复和第十六轮真实小额完整退出支持，加载时再次核对原生订单/成交/仓位，预览和创建事务绑定资格版本；总开关关闭或撤销资格禁止新增风险，但保留原范围退出。普通HTTP创建的 `g-99cbabbdd0c64200` 已完成真实建仓、挂单、保护和普通HTTP停止退出，独立主网确认仓位/挂单0，余额200.042427，本轮验收成本0.01579 USDC。网页预览等待采用独立连接，实测显示校验通过及实盘创建按钮；全量1022 passed、2 skipped。详见[普通网站实盘入口](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-RELEASE.zh-CN.md)。继续其他方向、多币种共享账户和部署验证，目标active；不能据此宣称完整币安/欧易能力已实现。

2026-10-12 正式引擎实盘更新：已在主网成功启动两侧中性网格，取得0.00034 BTC真实网格成交、手续费和资金费记账，并完成受控恢复后的正式只减仓退出及订单清理。第五轮合计成本0.077046 USDC，退出后余额199.688124、仓位/挂单0；补单虽自动生成，但持续页面读取导致额度等待，尚未实际提交。已修复主循环监督阻塞、停止续接、未发送退出恢复、查询优先级及页面共用观察；第六轮4格验收进行中。普通网页实盘仍关闭，目标active，不能视为完整网格循环认证。详细事实与最新测试见[正式引擎验收](/Users/alex/Work/entropy/planning/grid/GRID-ENGINE-TRIAL.zh-CN.md)。

2026-10-12 实时观察与中性保护更新：真实账户的11个永续DEX/共享资金已通过持续订阅接入，缺订阅/快照/连接代次或心跳时失效；分别提交的两侧整仓止损在主网同时存在并均撤销，零费用，余额199.76517、仓位/挂单0。保护触发自动退出、重启续接及UNKNOWN不重发、响应超时状态查询已接入。全量919 passed、2 skipped、108 warnings（61.86秒）。普通网页实盘仍关闭、验收/划转额度0，目标active。下一步直接用正式GridRuntime做受限原生网格闭环与资格认证，见[实时观察和下一步](/Users/alex/Work/entropy/planning/grid/GRID-CONTINUOUS-OBSERVATION.zh-CN.md)。

2026-10-12 正式保护接线更新：整仓止损已进入订单执行器、库存/FIFO、恢复和暂停/停止流程；新增启动前挂置及发单/周期保护核对，中性要求两侧完整保护，保护成交冻结补单。创建页已接入止损价格，订单表显示整个仓位。新增14项保护验证，全量886 passed、2 skipped、108 warnings（58.52秒）。余额199.76517、无实盘网格，本阶段未新增真实交易，普通实盘按钮仍关闭。已实测组合账户WebSocket数据接口；下一步实时观察、原生中性/触发/部分成交、自动收尾与完整网页实盘认证。见[正式保护接线](/Users/alex/Work/entropy/planning/grid/GRID-PROTECTION-RUNTIME.zh-CN.md)，目标仍 active。

2026-10-12 后续真实验收：空仓多头/空头整个仓位止损均可挂置、查询和撤销；新增一次先挂整个仓位止损、再0.00013 BTC真实入场及减仓退出，Native 429期间止损保留，按持久记录完成清理而未重复开仓。本次成本0.01098 USDC，累计成本0.02483，当前余额199.76517、实际仓位/挂单均0。新增保护恢复及共享REST限速，最新872 passed、2 skipped、108 warnings（58.36秒）；新增验收/划转额度均0，普通网格实盘仍未放行。下一步正式网格保护、消息/历史恢复和完整网页实盘闭环，见[整个仓位保护接入](/Users/alex/Work/entropy/planning/grid/GRID-NATIVE-POSITION-PROTECTION.zh-CN.md)。目标保持 active；后文较早余额和测试数为历史记录。

2026-10-12 实盘接入更新：真实主网账户已保存，代理地址已按原生授权关系修正为资金主账户；USDC 统一账户基础检查通过；一次0.00013 BTC真实入场、原生止损挂置、重启清理和只减仓退出已完成，总成本0.01385 USDC。当前可用保证金199.77615，实际仓位和挂单均为0。全量841 passed、2 skipped。已兼容统一模式的 USDC 主永续全仓读取与预算，普通网格真实开单仍关闭。新增独立限额原生挂撤单验收，详情与当前缺口以[实盘接入进展](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-COMMISSIONING.zh-CN.md)为准；下文保留前期检查记录。

最新改造：保存账户不再绑定币种，同账户多个不同币种共享签名／执行锁和资金预留，各市场独立管理订单与仓位；断线及重启先完成组合对账再重建。旧凭证和已有网格保留。新增21项多币种离线验证，最新全项目791 passed、2 skipped、108 warnings（52.49秒）。入口仍为8789，实盘派发仍关闭。先读[多币种账户报告](/Users/alex/Work/entropy/planning/grid/GRID-MULTI-MARKET.zh-CN.md)。以下770项为前一阶段记录。

更新：2026-10-12。先读 [IMPROVEMENT-REPORT.zh-CN.md](/Users/alex/Work/entropy/planning/grid/IMPROVEMENT-REPORT.zh-CN.md) 和 [IMPLEMENTATION-STATUS.md](/Users/alex/Work/entropy/planning/grid/IMPLEMENTATION-STATUS.md)。不要沿用历史实现报告的 G01–G18 全部完成结论。

最新页面恢复：用户已准备独立账户，但旧临时入口失效。已在后台启动持久 production 运行时与独立 `grid_web.py` 网站，并实际打开[网格账户](http://127.0.0.1:8789/#/grid/accounts)。固定本机端口8789，关闭浏览器不删除配置；未设置开机自启动。网站不初始化旧套利 profiles/.env/worker。账户保存与检查接入仍由用户在页面完成，不读取或询问其私钥。真实开单关闭。新增6项网站回归，全项目最新770 passed、2 skipped、108 warnings（50.24秒）；启动及验证见[持久网站入口](/Users/alex/Work/entropy/planning/grid/GRID-LOCAL-ENTRY.zh-CN.md)。

当前：独立账户/加密凭证、SDK签名准备、持久杠杆控制、私有流、资金费和存量模型已接入；已新增逐次发送前风险观察/签名后账本与消息变化复验、5秒周期风险核对及失败暂停撤单；真实减仓退出改用最新报价，拒单/UNKNOWN不连续重发。实盘认证仍为0，生产 write_enabled 与适配器默认 live_qualified 都保持 false。先读[实盘收尾与验收入口](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-CLOSEOUT.zh-CN.md)，再读存量、资金费、账户及执行报告。真实资金体验目标仍未完成。

用户最新决定优先：[模块分离](/Users/alex/Work/entropy/planning/grid/MODULE-SEPARATION.zh-CN.md)。使用者负责与套利使用不同真实资金账户；不开发跨策略互斥或旧 worker 桥接。用户最新要求尽快收尾、准备实盘，并要求继续；优先首笔实盘链路，不扩展推荐/移动网格等功能。账户已加密保存并实际通过接入检查与一次受原生止损保护的小额真实入场/退出；当前余额199.77615 USDC，无仓位或挂单。用户已明确授权在不需要其参与的情况下使用不到200 USDC实盘资金继续推进，目标仍 active；无需重复询问验证授权。密钥在页面输入，不发聊天。代码保护、小额残仓和真实验收仍有工作可做。

## 后续优先级

1. 完成交易所侧/本地保护与小额残仓、停止保护及真实退出认证。逐次补单风险观察、签名后变化复验、5秒周期观察与失败推进epoch/暂停撤单本轮已接入；真实原生保护和小额退出还未完成。恢复保留原数量，显式调整才能改变数量；风险暂停仍保留仓位，不能当作止损平仓。已有小额规则只阻断重建/退出，不是假定交易所可接受小额平仓。
2. 实际保留边界、经济排序和发布延迟证据；隔离环境实际签名/成交/撤单竞争/断连恢复及保护认证、部署演练。持久历史起点、资金费游标和60秒节流已实现，120秒重查不是完整性证明。生产 fills/funding 认证 floor 仍为空；订阅确认不能替代历史覆盖，不得直接 live_qualified=True 绕过门禁。统一/组合保证金与逐仓未认证，区间模型不覆盖无限价格变化或未来资金费。
3. 首笔实盘完成后再补价格条件、ROI/收益止损、移动网格、自动补资金的 Runtime 动作、调度、操作幂等、重启恢复与网页管理。
4. 后续产品阶段：真实 OHLCV 数据、行情驱动回放和推荐界面、通知、全量可追溯导出、模板与推送。
5. 隔离测试环境认证及 Linux 部署演练。

## 不得回退的约束

- schema v6。运行库文件排他锁必须先于迁移获取；Store 回调通过 call_soon_threadsafe。
- 发送前读取持久 epoch / desired_mode / status；DISPATCHING 是在途，不可作为未发送过期。
- 暂停/停止抢占启动、恢复和调整；任何较旧异步回调不能再写 ACTIVE / RUNNING。
- 成交必须绑定本网格订单与正确账户/市场，按真实数量记账；重复消息不再次配对或补单。
- UNKNOWN 查询优先；缺历史覆盖不得推进恢复 checkpoint 或宣称已对账。
- 配置版本不可变；调整后旧库存仍使用旧版本退出价；追加 buffer 预留不被规模调整覆盖。
- 存量风险上下文只从服务端归属/覆盖证明生成，创建不能认领外部仓位；回加已有保证金受实际净值限制，亏损减少预算，盈利不自动放大规模。
- 每笔原生普通网格发送需新风险观察，签名后检查许可时效、账本/预算/其他订单/消息变化；周期核对与发送共用账户锁。风险失败先冻结epoch再撤单，不能覆盖新暂停/停止；真实减仓使用新报价，UNKNOWN/拒单不连续发送下一笔。
- 旧库存退出只减仓，旧平仓不重启旧循环；新版本清理已重建的旧需求，迟到旧成交仅为新增剩余库存生成退出需求。
- 资金费入账/归属/覆盖同一事务；失败保留旧游标，恢复强制查询；CLOSED 结束时间不可重写，重复停止不得释放新网格占用。
- 旧操作失败清理同样核对 epoch，不能覆盖新 STARTING；首次真实启动先对账，再做账户控制与发单。
- 恢复成功后需要重新构建订单；CLOSED 必须同时证明账本仓位、实际仓位和订单均已清零。
- UI 以服务端配置、预览、金额和履历为准；参数一变旧预览立即无效；图表不制造 K 线。

## 可重复验证

```sh
.venv/bin/python -m pytest tests planning/grid/review/test_review_regressions.py -q --tb=short --disable-warnings
# 最新结果：770 passed, 2 skipped, 108 warnings in 50.24s

.venv/bin/python -u planning/grid/review/serve_isolated.py
# 自动打印临时本地网页地址，模拟账户，无真实 profiles/.env；Ctrl-C 清理

.venv/bin/python -u planning/grid/review/serve_isolated.py --hl-readonly
# 真实公共行情 + 全零公开观察地址，不加载密钥，真实开单禁用
```

新增 tests/test_grid_hardening.py 覆盖 17 项资金、并发、拒单/未知补单与客户端绑定场景；原 planning/grid/review/test_review_regressions.py 的 15 项失败场景已全部通过。历史 REVIEW 保留为修复前证据。

HL 依赖用 requirements-grid-hl.txt，官方 SDK 固定 0.24.0；eth-account 与 SDK 兼容版本为 >=0.13,<0.14。测试签名使用公开单元测试私钥，不读取项目凭证。证书校验使用 certifi，禁止为网络问题关闭 TLS 校验。

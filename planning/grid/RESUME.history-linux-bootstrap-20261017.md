# 当前接续点：Linux已迁移，只读，完整回归仍待解决（2026-10-18 北京时间凌晨）

1. 已连root@taoli.coinfetcher.xyz，密钥/独立目录/隔离情况见下方记录；不再询问用户SSH。旧套利PID1198962保持不变。本机runtime/watchdog/web均disable+bootout，com.entropy.grid.tunnel把本机127.0.0.1:8789安全转发到服务器，只影响访问，不负责交易。严禁重启本机执行造成双写。
2. 三个远端网格服务active/enabled，runtime GRID_REAL_DISPATCH=0、空正式grants；新接入授权/模式/余额/clean/市场/signer通过，1个真实资金账户（SQLite有2行历史账户，不能当丢失）。服务器独立Native最新复核：11DEX零仓零单、余额198.794373；远端data/run/grid-service-native-v32-preflight-source-proof.json。60个历史网格全CLOSED/所有intent终态。尚无新资金动作。
3. 实机60关键测试通过；实际空账户SIGSTOP→11.384625秒新实例+介入记录通过。这不替代持仓故障资格。正式普通24h仍未开始。
4. 原309源包遗漏公开测试向量/config.example.yaml/discovery-watchlist.yaml，已补齐。三模板Umask拼写已改UMask并实际确认0077。最终公共312文件包data/grid-code-checkpoints/grid-code-20261017T1604Z-linux-complete.tar.gz，SHA256 7bf89283d06f97af86c84bee0587c5d91bff02ae175bbb91e093cc06ef6f0818；远端data/run/source-release-complete.tar.gz及SOURCE-MANIFEST已逐文件核对。只有模板、公开样例、迁移文档改动，核心交易代码未再改。
5. 完整Linux回归在独立低优先级systemd transient unit entropy-grid-regression-v2.service运行（Nice10/CPUWeight10/RuntimeMaxSec1800）；日志data/run/linux-complete-regression.log，完成有linux-complete-regression.xml和ExecMainStatus。当前日志仍见一项F，尚未收集终局失败名称，不能报全通过或开新资金验收。早先SSH前台全量350秒超时，不能当通过；日志linux-full-regression.log已有部分进度，但无完整结论。v1已停止，勿重复启动测试占用CPU。专门33个accounts测试、2个Node UI测试另外通过；按dot索引推断失败名不可靠，不要相信linux-regression-pending-tests.json里的failed名字。
6. 新第32轮仅准备未启动：data/run/grid-service-native-v32.py（v31复制，仅新RID/v32文件名+独立seed模板）、grid-linux-loop-v32-template.json、grid-service-v32-finalize.py，以及Native audit/health/stream/native-flight observer均已复制远端。finalizer只独立Native审计+备份，已移除旧Mac small20/fault23 review-bundle复用，不创建/安装正式候选。还没有创建v32 trial policy、没有修改unit启用trial、没有创建campaign transient service；不要以为已开单。代码syntax已检查。先处理完整测试失败，再按新日期/独立RID/50累计上限、实际40资金20reserve/3cross/150-30保护/6h观察窗口创建有期限新政策并重新验证健康/Native。失败31原期限和证据一律不复活，正式grants仍空。
7. 已创建当前聊天heartbeat自动跟进，id=linux，名称“网格 Linux 实盘验收跟进”，每15分钟。不要重复创建；只在关键变化/故障/确需用户时通知，普通24h与终局审计完成后停用。goal API仍旧blocked（历史SSH问题，现已解决），工具无active切换，但用户已恢复授权推进，继续实际工作，不再报告仍缺主机或私钥。

下方是历史过程，以上为准。最新远端部署汇总proof=data/run/grid-linux-installed-readonly-proof.json；本机停止与新备份proof=data/run/grid-linux-cutover-local-stop-proof.json。公网taoli根路径仍旧套利，nginx没改；未来公网网格入口须独立鉴权及Host/Origin/TLS处理，不能裸露8789。

# 最新：独立Linux已安装并迁入账户，当前只读（2026-10-17 UTC15:50后）

SSH：root@taoli.coinfetcher.xyz，项目Pendle_Proxy.pem，IdentitiesOnly/BatchMode/StrictHostKeyChecking均yes。独立/root/code/entropy-grid，Python3.11.6独立venv，旧套利/root/code/entropy及entropy-console PID1198962保持不变。网格3个systemd已enable，GRID_REAL_DISPATCH=0，正式policy grants=[]，无新金融动作。

实机60关键测试通过；真实SIGSTOP看护已在11.384625s替换runtime并写入watchdog_recovery_requested（空账户），proof=data/run/grid-linux-infrastructure-proof.json。这不是持仓故障资格。修复三模板Umask→UMask，服务器show已确认0077；归档源模板因此有后续修订。原公共包遗漏planning/grid/acceptance-vectors.json导致全量收集失败，已单独补传公开测试向量，第二次Linux全量正在运行（本地exec session73354，远端data/run/linux-full-regression.log），不得在结果前报全通过。

资金切换已做：本机fresh Native 11DEX零仓零单、余额198.794373；60grids全CLOSED/所有intents终态。已disable+bootout本机runtime/watchdog（不是仅停止）；旧本机网格web也停用。新私有备份data/grid-backups/grid-20261017T154718Z-37d8ce68 seq288/integrityok；仅closed DB/成对加密vault/空正式policy通过已验证SSH迁入。未复制Mac心跳/PID/socket/老trial policy。本机转发com.entropy.grid.tunnel已安装，127.0.0.1:8789现为SSH隧道到服务器loopback8789；三网页/API实际200且runtime-86826141。隧道只影响访问，本机休眠不再停止服务器。

注意账户计数：历史SQLite accounts有2行，但vault/UI只有1个资金账户，不能把这视为迁移丢失；初次验证脚本错误assert UI==2，尚未写成功proof。随后实际accounts_verify成功，checked_ts2026-10-17T15:50:24.496561Z，授权/模式/资金/clean/markets/signer全pass，execution pending；远端proof grid-linux-account-readonly-start-proof.json。账户WS已connected/observation ready，base venue gap=true尚需具体网格恢复校验。当前仍无新活动网格。

下一步：取Linux全量结果并解决实机问题；重新记录逐源文件/包/部署配置版本；新独立Linux有界验收（新RID/政策，不复用失败31）及真正持仓故障/小残仓/闭环/断线恢复证据，全部正式审核后普通实盘及实际24h。不要重启本机runtime，不要把Mac23故障时间当Linux证据。公网taoli根入口仍旧套利，nginx未修改；现独立网格仅通过安全SSH转发访问，后续公网入口须独立鉴权并处理Host/Origin/TLS，不可裸露8789。

以下为历史记录，以上为准。

# 最新：SSH已恢复，正在独立安装Linux网格（2026-10-17）

用户指出项目目录有密钥。已使用项目的Pendle_Proxy.pem（不读取或输出内容）成功连接root@taoli.coinfetcher.xyz；主机校验通过，认证阻塞已解决，无需再次向用户索要登录方式。

实机只读核实：OpenCloudOS 9，Python 3.11.6，pidfd API及当前内核打开/关闭成功。旧套利entropy-console.service保持active，原/root/code/entropy有现存改动，不能覆盖/pull/reset/restart。网格三个systemd单位原先均不存在，loopback8789空闲，磁盘约41GiB。

已创建独立/root/code/entropy-grid（0700），公共309文件归档已上传（SHA256 4dc2916bdf171c6c3d29a830684a6d47382137290ad07b84c62c42b74a443810）；正在逐文件验证、解包、安装独立venv。尚未迁移账户凭证，尚未启动远端网格，未发新金融动作。下一步先只读启动并实际SIGSTOP测试固定unit看护；账户切换前必须停用本机runtime/watchdog、Native零仓零单复核，防止双写。Linux实际资格与正式24h仍未通过。

下方是历史记录；其中“待提供SSH/认证阻塞”已由本段解决。

# 用户已补充服务器域名，已找到部署记录；当前SSH认证待恢复

最新用户说明：之前项目已部署，域名taoli.coinfetcher.xyz。已在DEVOPS.zh-CN.md、deploy/DEPLOY.zh-CN.md、CONSOLE-V2-DEVELOPMENT-SPEC找到同一目标，文档记录root与/root/code/entropy（尚未远端核实）。此前只查SSH配置漏查部署文档，不能再要求用户重复给域名/目录。

已实际尝试只读SSH：BatchMode=yes、StrictHostKeyChecking=yes、root@taoli.coinfetcher.xyz，服务器已知主机校验通过但拒绝本机默认id_ed25519公钥，exit255。诊断确认Offering public key后仍Permission denied(publickey,gssapi-keyex,gssapi-with-mic)。ssh-agent无身份，~/.ssh只有该默认私钥/公钥，没有其他配置身份。未读取或输出私钥，未修改远端任何文件/服务，不改旧套利。

已异步询问用户原来用什么登录命令/工具/别名/已有密钥文件路径，不能让用户发密码/私钥内容。当前新的认证阻塞首次出现，不能沿用此前缺域名的3轮计数；本次用户回复视为重新推进。Linux适配1484测试/归档保持，尚未安装或实机验收；本机没有活动网格，正式grants仍为空。需要正确登录方式才能做真实Linux迁移，不通过旧控制台API绕过SSH认证，不猜其他用户名或传凭证到其他IP。

以下为此前准备记录，最新目标与阻塞以本段为准。

# 最新：Linux看护适配已完成本地验证，待服务器连接信息（2026-10-17）

目标active；用户授权CLI/API不到200USDC独立账户自动推进实盘，禁止CUA金融操作、不改旧套利、不引入账户互斥。用户刚回答“已有 Linux 主机”，但尚未提供SSH连接名/地址和部署目录，已异步询问，不能猜目标或把本地OrbStack当远程全天主机。

## 当前真实资金与服务
- 第31 g-a14ab46c2c3042dc CLOSED/STOPPED/e5；主7588 terminal0，收尾69014完成Native0/review1/backup0，没有合格候选/grant。最新fresh Native复核 data/run/grid-service-native-v31-power-incident-final-source-proof.json：5fills/2stops/11DEX零仓零单；余额198.794373，净-.007746（gross+.02112，fees.028883，funding+.000017）。原第31审计保留。
- 最后CLOSED备份 data/grid-backups/grid-20261017T123520Z-9372e494 seq288/integrityok。当前runtime5573/web89365/watchdog13846仍健康，普通grants=[]，没有新活动网格，不要重跑31或盲开32。
- 第31带仓pause/resume和首笔买.0044@2431.8→补挂卖2434.4曾通过，但没有refill成交；睡眠期间又有maker买243?实际2429.2/2426.6成交，醒后已统一平仓。保护事件永久存在，本轮不能用于完整loop。

## 本次失败根因已确认：真实合盖休眠
- pmset系统日志：北京时间20:14:24 Clamshell Sleep（电池79%），20:29:54 DarkWake，20:30:39 Maintenance Sleep，20:34:12开盖Wake。
- 独立health：UTC12:14:25.415→12:29:54.149，wall928.734秒但monotonic1.009秒；第二段12:30:40.533→12:34:13.023，wall212.49秒但monotonic1.007秒。soak记录CLOCK_CHANGED2/OBSERVATION_GAP2，最长连续1044.749秒，不是24h。
- 原Native有效期30秒不能在休眠中维护，12:29:53触发PROTECTION_SERVICE_NATIVE_PROOF_EXPIRED，helper正常stop_flat succeeded。当前AC电源，但临时caffeinate -i不能覆盖合盖睡眠。不能通过延长保护期限/复制旧证明来绕过，也不因本次主机休眠再改交易保护逻辑。

## 运行中版本与新Linux代码（新Linux修改尚未部署）
- protection._verify已使用critical_market_snapshot，原5秒行情/标的/触价/Native检查不变。1464passed/2skip/110warnings/81.81s，86相关通过；已部署5573并绑定67源码hash，grid-service-native-v31-version-proof.json。
- 303文件归档 data/grid-code-checkpoints/grid-code-20261017T1144Z-protection-quote.tar.gz，sha eaae0e42c21617178b39d8e9ddb896328198f865b96eebd7aa575532d823df40。包版本/逐项hash验证；不是不可变运行镜像。
- 前几轮修复：resume新鲜保护证据原子绑定、pause/保护替换有限先查后撤、ACCEPTED正确撤单状态、unified失效只重采样不签、断线freeze/Native看护；真实断线恢复仍无合格独立证明。

## 下一步：为Linux独立主机迁移准备
1. 等用户回复确切SSH别名/user@地址和部署目录；本机~/.ssh/config只有Include ~/.orbstack/ssh/config。不要猜 /root/code/entropy 是用户选定目录（那只是现有服务模板）。不能传密钥/数据到不明确主机。
2. 独立进程看护 entropy_arb/grid/watchdog.py 的恢复命令仍硬编码launchctl（约191/196行），现有deploy/entropy-grid.service缺heartbeat/watchdog配置，仅默认GRID_REAL_DISPATCH0模板；不能直接宣称Linux支持实盘。可先在本地准备Linux systemd看护适配/单位配置和隔离测试，实机安装后必须验证实际替换、恢复/退出，不以Mac测试冒充Linux验收。旧套利服务不动，避免双写同一资金账户。
3. 当前普通准备绑定31（已失败），候选不会合格，不能安装。新主机的新有界验收仍须正式loop/small/fault/profile/Native/源文件/guardian审核；小残仓20、故障23是否可复用要核对新运行平台与保护范围，不能把Mac故障时间直接当Linux证据。之后普通网站HTTP新建、实际24h、终局Native/备份。
4. 用户的多币种/多交易所/高级功能全目标保留；目前先推进可靠实盘主机和入口，不标complete。历史见 RESUME.history-through-v31-20261017.md。异步问题未回答前可继续独立本地Linux适配，不把首次缺主机信息设blocked。

## 新Linux准备完成（本地验证，不是实机证明）
- 新 entropy_arb/grid/systemd_control.py：固定entropy-grid.service，systemd InvocationID+OS UID+/proc cwd/argv+DB/heartbeat作用域核对，pidfd固定旧实例并二次核对后SIGKILL，再start同unit，身份变更不杀新实例；缺pidfd接口不报告有效控制范围。
- watchdog.py增加auto/systemd/systemd-user选择，macOS仍launchd；原采样/冷却/3次预算/介入标记规则不变。systemd模板加入runtime heartbeat、Restart=always，以及独立deploy/entropy-grid-watchdog.service，默认派发0；目录仍是模板占位，不能当用户目标。
- 20个新隔离用例；相关52通过。完整1484passed/2skipped/110warnings/86.09秒，data/run/grid-linux-watchdog-final-tests.log。全部本地替身/回归，不声称Linux真实重启或实盘验证。安装检查 planning/grid/LINUX-CUTOVER.md。
- 新准备归档 data/grid-code-checkpoints/grid-code-20261017T1532Z-linux-watchdog.tar.gz SHA 4dc2916bdf171c6c3d29a830684a6d47382137290ad07b84c62c42b74a443810，309个公共源文件逐项hash/大小核对，包版本不变。proof data/run/grid-linux-watchdog-preparation-proof.json。
- 当前Mac5573仍加载1464旧版；watchdog.py/systemd_control.py等当前源与已加载版本不同，不能把旧67hash当新部署证明，不要未更新版本证据就启用实盘。未改当前服务或远程主机，未发金融动作。等待用户SSH目标/目录；本次已经完成有意义独立工作，不设blocked。

## 阻塞审计（连续3个goal turn同一缺失项）
用户已确认Linux主机存在，但仍无SSH连接目标和部署目录；原异步问题保持待答。本地Linux看护/模板/隔离测试/完整1484回归及归档均已完成。重新核对SSH配置仍只有OrbStack include，不能当用户指定服务器。DB活动网格0/未决intent0，普通grants=[]，普通控制器未启动。不能安全执行远程核查、上传凭证、安装和Linux实机故障/实盘验证。无进一步独立关键路径工作，目标按规则设blocked，收到确切连接目标及目录后继续，不改门槛或在休眠笔记本重开交易。

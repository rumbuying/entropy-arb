# 最新 HL：用户明确要求解除币种/固定金额/3倍限制，已实际正式发布

2026-11-01 用户要求「HL取消这些限制然后上线，我要开始正式使用」。已在其授权下将HL从测试市场/金额白名单切到私有schema2账户级发布：实时179个主永续市场、capital_limit=null（取消40/200测试金额门）、杠杆使用原生币种上限（当前BTC40/ETH25/SOL20），保护过渡敞口按2×总投入×杠杆计算。真实账户保证金、共享预留、风险/精度、止损、看护与恢复检查保留；当前开放模式继承原做多/立即或等待入场/GTC，不声称做空中性逐仓HIP3全部上线。原ETH循环/残仓/持仓故障证据仍保持原市场，新增授权与原生证据明确分开，不伪造每币种单独认证。

从线上HL准确源隔离候选 `/root/code/entropy-grid/data/staging/hl-production-open-v1/source`，只部署live_release分派、新hl_production_release模块、hl_account目录钩子及create/qualification两个JS，随后仅修账户页汇总/过期执行待认证文案。未复制本地混合Runtime或Bulk改动。Linux相关131通过/0fail/error/skip，前端4通过。纯公共原生预检：ETH/BTC/SOL均80USDC/5倍VALID；部署后实际RPC BTC同样VALID/live_eligible=true、guardian healthy；活动grid/待执行operation/未知intent均0，金融发送0，没有自动建单。

实际发布时间11:20:32+08，HL worker161833→978576、watchdog161838→978578。HL web162206、Bulk worker978179/web4049358、套利198980本次前后身份相同；Bulk仍由另一位开发者推进，其资金政策/任务不在本轮范围。生产目录仍 `/root/code/entropy-grid`，仅HL服务受控切换；当前live-policy在data/run/grid-live-release-policy.json为schema2，禁止按旧记录覆盖回schema1/旧白名单。部署前备份grid-20261101T031928Z-b6f7acdc、integrityok/seq5718；上线后新政策另做配套备份。

完整发布记录 `planning/grid/reports/2026-11-01-hl-account-production-release.md`，本地证据 `data/run/hl-production-open-v1/`。公网IAB有既有登录ERR_INVALID_AUTH_CREDENTIALS，未改认证；生产RPC、实际网站静态HTTP200和SHA已验。一次性预检/部署脚本均已执行结束，不重跑原身份，不重启本机网格，不恢复AI定时巡检。HL此次是已发布的软件权限变更，不是新的实盘观察轮或资金样本。

# 最新补充：终局核验脚本预演 11/11 PASS

bulk-final-audit.py 已校准并预演通过（11/11 PASS）：venue 终身 fills/orderHistory、引擎 7 笔 executions/4 配对、27 张全 CLOSED、周期成交全被账本覆盖、fees 对账残差 1.5e-18、funding 残差 2.7e-10（≤1e-9 界）、账户平仓、事件审计、备份 integrity+34 表。11-02 03:20 UTC 到点后重跑一次确认观察期内无新事件即正式开放。

# 最新补充：观察期自主日志已部署（cron 每小时快照）

cron /etc/cron.d/bulk-observation-snapshot 每小时整点运行 observation-snapshot.py，追加网格状态/成交/配对/venue 计数器/保护事件到 data/run/observation-log.jsonl（首条 11-01 04:01 UTC 已写入：27 CLOSED、7 execs、4 pairs、venue 平仓零挂单计数器冻结）。到点后终局核验时一并审计此日志确认 24h 无异常。

# 最新补充：终局核验已脚本化（一键执行）

终局核验脚本部署于 releases/bulk-normal-trial-v43/bulk-final-audit.py（只读）：venue fills/orderHistory 对账、引擎 executions/pairs 计数、全网格 CLOSED、计数器对账（venue 舍入残差规则）、观察期事件审计、备份恢复验证（integrity+三周期账本）。11-02 03:20 UTC 到点后一条命令执行，全 PASS 即按 r16 范围正式开放。

# 最新：统一网格门户上线（11-03）

/grid/ = 统一门户（nginx 静态直出，两张卡片）；/grid/hl/ = HL 控制台（新路径，代理 8789 根）；/grid/bulk/ = Bulk（不变）；其余 /grid/* 照旧代理 8789。同一 basic auth。后端零改动，浏览器实测双控制台完整启动+API 200。临时测试凭据用后即删。配置：deploy/nginx-grid-location.inc + deploy/grid-portal/index.html（与 /etc/nginx 与 /var/www/entropy-grid-portal 同步）。注意 URL 内嵌凭据会导致页面 fetch 被拒——正常认证对话框无此问题。

# 最新：限制放开交付（11-03）——13 币 + 10 万/50 万额度 + SOL 多币全链路实证

r17c 政策（90 天窗口）：13 币全 eligible、capital 100k、cumulative 500k。排除 PAPER（风险 WS 不推帧）与 PAXG（零相关性对，可单币政策单独开）。SOL 全链路实证：买单挂簿→成交 0.8658@93.172→止损保护→平仓 92.774→配对入账→审计 10/10 PASS（fees 残差 6.8e-18）。真实亏损 ~0.37（恢复循环提前平市）。本轮七项修复部署（bound-read 取消停损路径、验证窗口 30s、watch 恢复窗口跳过、恢复重试上限 12 等）。**剩余缺口**：多市场恢复屏障可被快照流打断（已插桩待定位）；short 需新风险模型（long-only 几何）。单市场 BTC 24h+ 稳定。建议多币稳定前每账户 1-2 市场并行。

# 最新：观察期跑满+终局核验 11/11 PASS——Bulk 普通实盘按 r16 范围正式开放

24h 观察期跑满（hourly 快照全程无异常），终局核验 11/11 全 PASS（venue 对账/引擎账本/计数器含 venue 舍入残差/账户平仓/事件审计/备份恢复验证）。**按 r16 验证范围正式开放：bulk/mainnet/BTC-USD/long/deferred/post_only+gtc/单格 40 上限/累计 80**。系统持续可用——网站创建页可直接开新网格（r16 剩余额度 40，需新政策文件签发新窗口续额度）。三周期+暂停恢复+归零全部有真实证据（handoff §18.1–18.14）。

# 最新：24 小时观察期开始（自主运行，终局核验清单就绪）

观察时钟 11-01 03:20 UTC 起算、**11-02 03:20 UTC 到点**。自主组件全部核实：五服务 active、bulk 看护 healthy、heartbeat running、各调度器经三周期实战。观察期无在场网格（周期八零损失关闭），计数器冻结。**11-02 到点后终局清单（handoff §18.13）**：①venue/引擎全量对账（含 venue 舍入残差规则）②观察期事件审计 ③从 grid-20261031T045510Z 备份恢复 DB 验证 ④按 r16 范围正式开放 ⑤文档收尾。全部通过即 Bulk 普通实盘上线完成。

# 最新：暂停→恢复→结束归零完整闭环样本采集完成（第三步证据齐备）

周期八 g-06a3d47e（r16 窗口）上完成全部剩余操作样本：主动暂停（买单撤下、止损保留）、主动恢复（epoch 3 回 RUNNING、全部订单重新挂上 venue）、结束归零（stop_flat→CLOSED，27 张全部 CLOSED，venue 平仓）。**新修复**：暂停 epoch bump 使旧 lease 陈旧化，watch 在 resume 激活中按「过期」误杀——修复为对 lease 代次落后于当前 epoch 的陈旧 lease 跳过强制（止损始终在场，保护从不缺席；当前代次过期仍强制）。第三步证据齐备：4 买 2 卖成交、多笔拆分配对、暂停→恢复、3+ 次结束归零、scoped 恢复实战、保护退出实战。剩余：24h 观察跑满（至 11-02 03:20 UTC）、终局核验、备份恢复验证、按 r16 范围开放。账户净盈利 +0.0069 保持。

# 最新：政策更替 r15b（累计 100/48h）；周期七满编在场等待成交

r15 累计被 CLOSED 网格预留占满（40/80）→ 签发 r15b（新哈希重置累计，40/格、100/累计=校验硬界、48h 至 11-03 03:03 UTC）。周期七 g-f0001258e1c943a1 RUNNING：止损 76834.72 + 3 postOnly 买（77222.77/77377.99/77533.22）全部 venue resting（fullAccount 核实），77688.44 在 mark 上方正确等待。注意：/tmp/bulk-cycle2.py 的固定 request_id 跨周期会 409，须换新 id。TRIAL_VETO 诊断在位。观察清单同 §18.10。

# 最新：24 小时观察期开始（周期六满编在场，自主运行）

周期五被 venue 403 限流安全退出（零损失）；周期六 g-f2137fc54（mark 居中）满编在场：3 postOnly 买+止损全部 venue resting（fullAccount 独立核实）。**观察期时钟 2026-11-01 03:20 UTC 起算**，r15（48h 至 11-03 10:21 UTC、40/格 80/累计）覆盖全程无需续期。观察清单（handoff §18.10）：账本 vs venue 对账、断线场景已实战、暂停→恢复样本待做、小残仓待采、11-02 满 24h 后终局核验+备份恢复验证，全过则按 r15 范围正式开放。教训：venue 读接口 403 限流，轮询 ≤1/10s。

# 最新：三个完整真实周期闭环——周期三为多笔拆分配对的真实 maker 循环

周期三 g-99b9d3043：价格上行，**两笔买入分别成交**（0.00025788+0.0000195@77552.85 费 0）→ 流断线保护退出 → flatten 0.00027738@77560.13（费 0.00753）**正确拆分配对到两笔买入**（2 行 pair_allocations，首次多笔配对证据）→ CLOSED。21 张网格全部 CLOSED，venue 平仓零挂单，fees -0.026759 精确对账，realizedPnl +0.0183，账户较基线累计净盈利 +0.0069。**收敛缺口修复已部署**：恢复期流重连后 basis 刷新时同步清除 STREAM_CHANGED 缺口并重算 ok（原只清 funding 缺口，使退出循环卡死）——该修复直接使周期三收敛。第三步证据：3 次买入成交、2 次 flatten、多笔拆分配对、3 次 scoped-basis 流断线恢复、3 次 CLOSED。剩余：暂停/恢复样本、保护续期带仓恢复、小残仓、24h 观察（r15 续期）、终局核验。r14 至 11-01 15:15 UTC。

# 最新：两个完整真实周期闭环（第二个盈利）+ 第三网格在场等成交

周期一 g-4925b3ad（fill 77429→flatten 77357.02，净损）；周期二 g-b98afe0fa（fill 77314.19→**持仓资金费收付 +0.0002866 入账**→flatten 77464.37，**realizedPnl +0.0163 盈利**，1 配对，CLOSED）。venue 对账：fees -0.019229 十进制精确，funding +0.000267824 折叠一致（venue binary64 舍入残差 1.1e-10 在 1e-9 界内，显式记录），totalMargin +0.0069 净盈利。**新修复**：scoped fold 字段符号约定（存储相对 counter 反号，sign=-1）+ residual 接受 venue 舍入（≤1e-9）显式记录 + residual 公式（native vs exact）。上轮「ack-then-cancel」定性修正：venue REST 端点对新订单有分钟级可见性延迟，订单实际一直在 book（保护 verify 与平仓行为证实）。第三网格 g-99b9d3043 RUNNING（4 单 venue resting）等成交。r14（24h 至 11-01 15:15 UTC）在位。安全：账户除两周期外无变动。

# 最新：venue 订单可见性异常完整证据链（修正 ack-then-cancel 定性）+ 网格 RUNing 等待

r13 网格 g-b98afe0fa（mark 居中 77004-77623）RUNNING：止损 ACCEPTED、3 买单 ACCEPTED（原始 HTTP 响应捕获=venue ack 我们的确定性 OID 为 resting）、1 买单 PLANNED（高于 mark 正确等待）。**但 openOrders REST 查询 >10 分钟返回空**。修正上轮定性：BULK_ORDER_HTTP 捕获的 `{"cancelled":...}` 是自己 close 时的撤单 ACK，非 venue 异常撤单。鉴别证据：同一读通道 protection.verify 能确认止损在 book（买单才拿 permit）；g-4925b3ad 周期买单曾在 openOrders 可见并成交。剩余假设：新订单可见性延迟（副本/索引）、ALO 无仓位账户生命周期、WS 与 REST 数据源不同步。**安全分析**：买单不在 book→无成交→无仓位→止损无需生效→安全但网格 inert；发散后果是无成交而非无保护风险。下轮判定工具：worker 私有 WS 快照（权威）对比引擎账本；或 r14 试 GTC/联系 venue 支持。

# 最新：首个完整真实周期闭环（maker fill+flatten+配对+CLOSED 对账）；新阻塞=venue ack-then-cancel

r13 签发（12h）→ 网格 g-a63d34a9 创建启动 → 买单 ack resting → **venue 数秒至数分钟后静默撤单**（openOrders/orderHistory/fills 均无行；原始 HTTP 捕获显示重发时 venue 返回 cancelled——ack-then-cancel 模式）。排除签名/nonce/ALO crossing/保证金/副本延迟；探针时代的单在 orderHistory 有行且持久——差异指向 venue 会话绑定/风控规则，**需 venue 文档或支持确认，是上线前置阻塞**。此前：第一完整真实周期已闭环（g-4925b3ad：maker fill 0.00027702@77429 费 0 → 保护退出 flatten sell 0.00027702@77357.02 费 0.0075 → 1 配对 → CLOSED 对账一致：fees -0.0117 十进制精确）。安全状态：账户零仓位零挂单。本轮修复部署：pre-I/O 拒绝 deferred 化（两级 fence）、可证明未发送出口、scoped 边界/零经济终态/子成交证明、funding 指纹剥离、监视器豁免、bulk 看护。服务器 bulk 套件 1128 全过。插桩待清（清单 handoff §18.3.3/§18.6）。

# 最新：真实 maker 成交入账+受保护退出进行中（第三步首个真实周期）

r12 签发（8h 窗口）→ 新网格 g-4925b3ad（77051–77807 4 格 40U 止损 76649）创建启动 RUNNING → **真实 maker 成交**：买单 0.00027702@77429 被 taker 吃单（maker 费 0），入引擎账本，仓位开启，venue asOfSlot 前进。随后流断线逢保护证明过期 → 设计内保护性退出：撤非保护单、留止损、平 0.00027702 仓位（flatten 在重试队列）——RECOVERING/STOPPED epoch 4 自主收敛。**这是第三步首个真实周期样本**（真实成交+真实仓位+真实保护退出），非失败。下轮：等 CLOSED→对账→r12 窗口内重建等 maker 循环 2-3 轮→带仓恢复样本→24h（r13 续期）。r12 明确 40/格 80/累计 8h。

# 最新：退出收敛 CLOSED→新网格已建并达受管理 WAITING_RANGE（止损在场）

resume 成功：g-ba64b7e3（epoch 3）止损 ACCEPTED 在位、3 买单 PLANNED、mark 77500 高于上沿 77487 → WAITING_RANGE 设计延期（价格回区间后 range_start 自动挂买单）。本轮修复清单：①快照经济内容去重（指纹稳定性根因）②激活窗口误杀×2 ③读延迟 15s/120s+observed_after_read ④两级 fence 的 pre-I/O 拒绝 deferred 化（回到 PLANNED 重试而非 ORDER_NOT_CONFIRMED）⑤可证明未发送出口（无 journal 行/未 claim→EXPIRED_EPOCH 两跳）⑥scoped basis 边界按 nonce+request_hash 匹配+零经济终态接受 ⑦bulk 看护已 healthy ⑧scoped-basis 子成交证明（止损撤单绑定条款全匹配时）。服务器全量 bulk 套件 1128 通过。**r11 累计额度用尽（两格各 40）——下一网格需 r12 政策**。插桩待清（清单见 handoff §18.3.4）。

# 最新补充：退出收敛的精确缺口=MAY_HAVE_SENT 行的确定性 OID 查询（v42 先例）

退出未收敛的最后缺口定案：epoch-21..32 的 fence-fail/TimeoutError 留下若干 MAY_HAVE_SENT bulk journal 行（claim 已提交、I/O 被打断）——有确定性 order_id，但 venue 从未出现该订单，lookup 按 design 拒绝把「查无」当「未发送」证明 → scoped basis PROOF_INCOMPLETE（已命名到 cid）→ stop_flat 保持 RECOVERING 重试。**正确解法有 v42 先例**：对这批行按确定性 OID 做原生查询（orderHistory 近段窗口），有终态行则照 v42 语义入账；持续查无则等 venue 保留窗口前移自然收敛；**不得**把查无改成未发送证明。安全状态：止损单 resting 在 venue、账户零仓位计数器不变、r11 不再新开支路。插桩清理清单见 handoff §18.3.3。

# 最新：三个真正根因已修并部署（快照去重缺陷/激活窗口误杀×2/读延迟参数），网格停在受保护退出等待

单点排查定案：①bulk_stream 快照去重用整行 JSON（含每秒时间戳）→ 挂单期间 private_revision 每秒 bump → 风险指纹永不稳定 → 全部买单 RISK_EVENTS_PENDING 无限延迟；改为经济内容去重+回归测试。②激活窗口误杀：observe/watch 在租约未写窗口判死 STARTING/RECOVERING 网格；采集侧超时（COLLECTION_TIMEOUT/SNAPSHOT_EXPIRED/NATIVE_SNAPSHOT_STALE）改 deferred 重试（监视器+发送路径），不再冻结。③读延迟：bulk 装载时 max_age=15/collection_timeout=120；observed_from_ms 改数据到达后取时钟。配套：bulk 独立看护 entropy-grid-bulk-watchdog.service（--unit 参数化；WorkingDirectory 必须=release 目录，python -m cwd 优先级会吃旧 bulk/entropy_arb 副本；scoped() 接受 release 子目录）已 healthy。**状态安全**：止损单仍在 venue（resting），账户零仓位计数器不变；网格 RECOVERING/STOPPED 等 UNKNOWN 买单（从未成交，venue 保留窗口未覆盖其身份证明，设计上不猜测）的退出收敛——下次 stop_flat 重试或可证明性出现即收敛。教训：HL 调优时间窗需按 bulk 读管线重新校准，先读 risk-watch 的 source_age_ms 实测。插桩 RESUME_TIMEOUT/NA_BUMP/NA_PAUSE_SET/PERMIT_EXPIRED/PUMP_BASIS_REFRESH_FAILED 仍在位待清。

# 最新补充：单点已收敛为「resume 激活后段 TimeoutError」，钩子已埋

单点最新证据：恢复链路全部 ok（scoped_basis/scoped_funding 持续 true）；resume 失败在激活后段（`RESUME_FAIL TimeoutError()` 空消息，epoch 6→7，随后各轮 resume 自撤上一轮止损再重挂）。已部署 `RESUME_TIMEOUT` 钩子（lifecycle.py:345 打印完整堆栈）——下轮第一次 resume 即得确切 raise 帧；初判=激活后段共享 1/s 步进读队列（风险观察+保护+候选下挂叠加）某处 wait_for 超时。另已修：`native_account` 的 observed_from_ms 改为数据到达后取时钟（读队列等待不再计入 5s 新鲜度窗口，消除间歇性 BULK_BUDGET_SNAPSHOT_STALE）。状态安全：venue 无挂单、账户零变动、止损意图随 resume 重武装。插桩清单：RESUME_TIMEOUT/NA_BUMP/NA_PAUSE_SET 在位（定位后清除），FENCE_FAIL/STREAM_CHECK_FAIL/RESUME_FAIL 已移除或替换。

# 最新：真实部署完成+第二步恢复接线主网验证+首张真实止损挂单（单点：激活后暂停源）

2026-10-31 用户批准提高试运行预算（~55 USDC 级）；实测 Bulk 主网 BTC-USD minNotional=1.0（其余 40–50）→ 试运行市场 BTC-USD。**已完成真实部署**：备份 grid-20261031T045510Z-v43-trial（DB SHA 0d8d919b…）→ `releases/bulk-normal-trial-v43/` → drop-in `95-normal-trial-v43.conf`（GRID_REAL_DISPATCH=1 + schema2 政策）→ 网站同路径驱动。政策 r1–r10 的累计额度被零成交启动调试耗尽（账户全程零仓零单计数器不变，多次独立核实），现生效 r11（余 40 一格）。

**第二步已上线并主网验证**：新增 `bulk_recovery_scope.py`（基线边界指纹跳过 pre-baseline 原单 + 基线后原单逐笔 bound lookup 不依赖历史窗口 + 全账户仓位/挂单双向对账 + 费用/资金费 f64 原生序折叠位级相等 + 绑定凭证/基线指纹/流代次）；接入 RecoveryBarrier（截断页→scoped basis→清过渡 gap）、_recover/pump_events 的 funding 降级、owned/portfolio risk context 的 scoped_funding 通道。**真实卡死网格 g-7017dcf0… 在主网经 scoped basis 恢复并干净平仓**——§8.B 的真实形态已被此路径解决。调试修复：ledger 0/1 布尔×3、BulkRiskInputs 1MB deepcopy（每单 7s→ms）、native_order_id 绑定键、watch/observe 激活窗口误杀（无租约且(STARTING/RECOVERING或无活跃意图)→跳过本 tick）。

**主网真实进展**：网格 g-2ec0b7f2…（BTC-USD 4 格 40U）经网站 API 创建启动至 RUNNING，**原生病挂止损已真实挂上主网订单簿**（sell 0.00723242@76482.81 reduceOnly resting，独立核实）。**单点**：激活完成后网格被置 NEEDS_ATTENTION/PAUSED（epoch 1→2，非 watch/refuse/monitor 已知路径，事件无 protection_gap/risk_paused）——下轮先对该 pause 全调用点加日志复现，修复后此格恢复即等 maker 成交。操作要点：create 超时必须同 request_id 幂等重试；残留 PAUSED 格会以 BULK_RANGE_OWNED_CONTEXT_REQUIRED 挡住新预览，需先 stop_flat。资金边界决策已获用户批准；24h 观察政策仍需届时一次性确定。

# 最新：v43 普通执行工厂 + 网站路径 Bulk 试运行已贯通（离线端到端 8/8，服务器 bulk 1123 全绿），未部署

2026-10-31 继 v42 之后本轮完成第 10 节「第一步」的代码贯通：普通网格现在能从**既有网站/API** 在试运行授权范围内真实创建、启动、经普通引擎发出原生病挂与挂单（不再依赖 standalone probe）。生产仍是 preview v23/GRID_REAL_DISPATCH=0，本轮**未部署、未下单、未重启任何服务**；HL/套利未动。

**候选**：`/root/code/entropy-grid/data/staging/bulk-normal-execution-v43/source`（服务器）与本地 `data/candidates/bulk-normal-execution-v43/source`，从完整 v42 冻结树逐字继承，仅加明确改动；本地工作区 4 个 v42 后恢复文件（recovery/runtime/stream_recovery/bulk_leverage_probe）未被覆盖，runtime.py 三个补丁已按补丁同步回工作区。

**新增/改动**：① 新 `adapters/bulk_execution_factory.py`：从加密 vault 行组装签名 BulkKeychain+BulkOrderTransport+BulkAccountBudget+BulkRiskFeed 的执行级 BulkAccountVenue，`attach_execution_venue` 只接政策点名的账户/凭证版本，先捕 v41 平账基线再武装（write_enabled/live_qualified/execution_qualified），拒绝替换已执行 venue，基线失败即 worker 启动失败（不回退 preview 冒充）。② `operator_trial.py` 新 `BulkOperatorTrial`（schema 2：venue=bulk/mainnet、账户+凭证版本+市场+方向模式+执行政策+单格≤50/累计≤100+硬到期，不 pin request_ids）与 `load_trial_policy`（HL schema1 路径逐字保留；v1 文件给 bulk worker / v2 给 HL 均拒绝）。③ `grid_main.py`：非 HL worker 允许装载 schema2 试运行政策（allowances/live policy 仍拒绝）；政策要求 GRID_REAL_DISPATCH=1 才武装执行工厂；重启无政策文件→BulkOperatorTrial(None) 清理→expire() 对未平仓网格发退出。④ `runtime.py`：`_preview` 对 trial 范围内配置标 plan.live_eligible（capability=operator-trial:<hash>），`_create_grid` 在事务内由 `website_context` 构建上下文并 `validate_creation` 全量复检（含 protection 必填），v1 改走 `request_authorized`（行为等价），metrics real_dispatch 含已武装 trial。⑤ `rpc.py` catalog 合并 trial 市场授权（operator_trial 标记）；⑥ `accounts.py` start_stream/close 关停账户私有流与 risk feed；⑦ create.js 试运行市场文案。

**e2e 揪出并修复的既有潜在 bug**（此前无测试走过"有保护挂单在场+第二个普通挂单"组合）：`bulk_budget.order_matches` 对账本 reduce_only 0/1 严格 `is True` 失败；`_pending_floors` 要求订单带不存在的 market 字段；`bulk_range_budget._inventory_terms` 同样 0/1 问题；**`BulkRiskInputs.__deepcopy__` 返回自身**——门控链把 ~1MB/市场的冻结风险面 deepcopy 4-6 次/单，服务器实测每单 ~7s CPU、触发 5s 风险许可过期（cProfile 22.3s/29.5s），修复后毫秒级。这是生产必需修复，非绕过：冻结快照共享即拷贝语义，fence 仍逐单重验。

**验证**：新增 `tests/test_grid_bulk_normal_trial.py` 8 用例（政策分发/权限/范围、工厂仅平账后武装、网站预览标 live_eligible+创建幂等、越界市场/超额拒绝、启动先原生止损后 post-only 挂单全走工厂+门控、累计上限拦第二个并发网格、过期冻结新发送+expire 请求退出、preview-only 永不授权）。服务器 3.11.6+离线防护：8/8（9.8s）；bulk 全套 **1123 pass/0 fail**（faucet 测试需 `planning/grid/tools/dispatch_bulk_testnet_faucet.py`，已补入候选与工作区）；HL 侧 trial/live_release/accounts/executor_capacity/initial_inventory **114 pass**。本地工作区同步后 bulk 1120 pass + 2 个预存在环境失败（pristine v42 同样失败）。`offline_guard.py` 已复制到工作区根目录。

**下一步（第二步起）**：部署需一次性决定——政策文件真实边界 + GRID_REAL_DISPATCH=1 drop-in + 排他切换 bulk writer + 部署前 DB/vault 备份。**关键决策点：已捕获市场规则 ETH-USD minNotional=50 USDC，§4 的 $2/笔探针边界放不下任何真实网格订单**；需用户在「选更小 minNotional 市场」与「批准提高单笔/累计预算（如 4 格×~$13≈$55）」间拍板，不得自动扩权。其后按顺序：账户自有范围恢复接线（v41 基线+v42 批量证据接 RecoveryBarrier/sync_funding/风险可用状态，全局 history 仍 unknown）→ 真实有限闭环（maker 买卖补单/暂停恢复/结束归零）→ 带仓故障/再入场/小残仓 → 24h 观察。`observe_position_net` 16 单/仅 closed 限制仍在。



# 最新交接：用户决定交由其他开发者继续，当前轮仅交付文档

2026-10-31 用户明确要求「我交给别人做，你把交接文档详细写一下」。已停止继续实现、部署或启动实盘测试，交付 `planning/grid/BULK-IMPLEMENTATION-HANDOFF-2026-10-31.zh-CN.md`（466行，含可复制给接手AI的提示词）。该文档明确普通Bulk尚未上线、生产preview v23/GRID0、准确v42完整冻结源与overlay区别、普通进程非HL政策硬拒绝/HL专用试运行语义/preview-only工厂、账户范围恢复及current funding缺口、全部已完成证据和不应重做的金融RID、资金期限边界、HL/套利隔离与实施顺序。后续从交接阅读，不把此段当新开工指令。

交接轮2026-10-31 11:01:57+08实际SSH只读确认Bulk worker3301268/web2298148、HL161833/watchdog161838、套利198980均active/running，Bulk两进程cwd为独立bulk目录，v42候选目录存在；无服务控制/金融/凭证读取，未重新查询原生余额。余额500.691276、空仓空单仍以原v42 checked1793414571097证据为准，不伪称交接时资金新审计。四个归档/Native报告SHA本地重新核对一致，v40/41/42相关Linux报告0fail/error/skip，文档引用的本地路径全部存在。没有新增后台任务，所有旧金融身份仍不得复用。

# 用户最新纠偏：实盘上线过慢，优先贯通普通网格闭环

2026-10-31 用户明确质疑多天未上线。已承认推进拆分过碎、单项验证/历史回放/重复大范围回归过多，未尽早串起普通创建、启动、maker补单、账户对账恢复和结束。接下来近期交付以真实普通网格完整闭环为准，保留完整Bulk产品目标和正式门槛，不再追加同类独立金融诊断或用测试数量替代上线进展。既有独立账户和小额有期限授权够继续推进，无需用户再准备账户或等官方答复；24h仍须完整闭环后开始，不自动扩大旧期限/预算。

最新具体代码检查发现普通 commissioning 入口 `operator_trial.py` 的创建路径仍直接要求HL unifiedAccount/positions.szi、HL prepare_history、杠杆<=3等。Bulk connections仍只注册preview factory，不能单凭底层Bulk组件已过测试就声称正常网格能启动。后续应把账户范围恢复证明、实际签名/风险/保护运行工厂与这一普通网格启动入口一起贯通，避免只做另一份孤立证明。此用户问答轮仅重新确认路径缺口，没有新金融操作/部署/后台任务；前一中断轮也只有读取，没有新代码或进程要恢复。

# 最新：v42批量原单/OPEN恢复入账已Linux1194与原4主网单只读验证通过

上一目标轮v41为实质进展，本轮又完成正式恢复路径改造，不是仅探针或空轮询。原PID3874487已terminal，Linux **1194pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793414270967，XMLSHA82987c2bf132a143fbb9e067c15a6f797e1acea0e7a4d4e6c5fa632268ec9058。本机最终相关116通过；新batch16/OPEN Runtime3/止损实际量1共20新增。所有相关报告均已实际下载解析核对，没有未处理测试/只读helper handle。

新增bulk_batch_lookup：每轮共享真正不同的前/后两次openOrders和同一history分页，每个CID仍走旧精确语义校验；仅当轮缓存，16MiB/90s边界，主动yield给保护调度。64个OPEN/terminal普通单+fills总7次读取，单次支持256。BulkBoundEvidenceReader.lookup_many接入现有reconcile_owned_fills；default普通proof v1 ID算法保留，显式include_conditionals=True用v2，准确根单parked保留cumulative_knownFalse/quantity_basis=unchanged-parked-original；同OID市场FILLED止损可按真成交核对，取消未知不伪造0。

**Runtime _backfill_owned_positives已真正支持OPEN**：发现可变原单后，在未缓存部分<=256时重新取整组稳定前/fills/后证明，与缓存的不可变终态全序回放；OPEN不持久当终态证书，重启重新查。终态分批扫描轮换不被同几笔OPEN饿死；超一轮容量仍pending、不宣称长期全部完成。提交重验epoch/status/desired/revision及所有原请求绑定；既有执行再核对费用/身份/序号等经济字段，避免SQL重复键忽略费用漂移。实际Runtime用例5成交含2OPEN全部入账、只缓存3terminal、重复/重开0重复、同OPEN后续0.02只补1条/库存.38→.4/fee.05→.052，仍因全局history未知保持NEEDS_ATTENTION/0新签名。固定stop Native FILLED实际量小于potential bound的journal旧拒绝已修：正面已知0<qty<=bound保存真qty，普通FILLED仍必须满量，原nonce/action/UNKNOWN receipt不变。

**实际生产Python/SDK0.1.30原生只读已exit0**：grid-bulk-batch-orders-native-v42.json checked1793414571097，SHA77ca8eede12677a3536a1a14814f983c3f7b239309540628e284d4702050df57。原v33 entry/exit +v37 entry/Root共4单仅3次RPC均FILLED/已知qty[.00001959,.00001959,.00001951,.00001951]；额外普通v1两真成交fee.001051和含止损v2两真成交fee.00105/net0核对通过。总19公共读，0 Runtime writer/0金融/0密钥/0服务控制；原27金融记录及nonce/OID绑定逐字不变，5服务身份未变。Native当前flat/noorders、500.691276，fees-.0042/funding-.000018782/realizedPnl-.005505。helper planning/grid/tools/verify_bulk_batch_orders_v42.py SHAd539783a540adaf562b73f0a78cdf14558d34c70d260192ba7ff5c824d57429a。全部旧金融RID仍禁止复用/重启。

候选 `/root/code/entropy-grid/data/staging/bulk-batch-orders-v42/source`，524manifest/10overlay，archiveSHAabfbcdef6410464a31a0b4f7f4caea766f0b8a9066f50f442a969597a9ed2bca；精确v41父源，Recovery只移植当前新_backfill_owned_positives到已验SHA的data/run/grid-bulk-recovery-v30-isolated.py父本，输出data/run/grid-bulk-recovery-isolated-v42.py，未混入本机其它HL recovery_read重试。Bulk仅改reconcile_owned_fills方法，其余对比父源逐字一致；签名/发送模块未变。详细reports/2026-10-31-bulk-batch-orders-v42.md。

**下一步必须继续完成账户范围恢复出口与普通执行**：v42证明/入账普通OPEN，但全局HistoryPage/FundingPage仍unknown；现有funding.observe_position_net仍<=16、全终态/归零，仅closed净额，不能假称current持仓资金费已对账。用v41账户baseline绑定全部新原单（普通/stop）、缓存终态与当次OPEN证据，采集Native closed+current 的fees/funding/realizedPnl对全账户counter delta，正式处理根单取消未知（保留查询未知，足够账户库存/费用守恒可单列推导依据，不能直接改known0），再接RecoveryBarrier和实际风控可用状态。旧global complete/cursor不改，恢复交易必须有新的明确scope证明。其后普通创建/启动/暂停恢复/结束、maker买卖补单、保护重挂、带仓故障/小残仓/24h继续完成；当前仍无真实普通网格，不把只读空账户或离线OPEN当持仓故障成功。正式v23/GRID0未改，HL/套利未动，完整Bulk目标active。不等官方、不重复已完成探针、不恢复AI定时检查、不把10min额度扩24h。

另有可用的真实字段证据供后续资金对账：原v37首个持仓原始WS里margin.totalMargin=500.694001，较交易前500.694526只减少fee.000525；margin.unrealizedPnl=-.000086，position.unrealizedPnl=-.0000857464500000795，所以totalMargin是此例的现金保证金，不能再减unrealized当现金。v33 Native归零余额.694838→.694526，Native PnL+.000758/fee-.001051/funding-.000018782的代数差另有-.000000218，需保留现金量化差并找实际证据，不能猜舍入规则或EPS抹平/把外部资金当profit。当前账本理论trade cashflow与Native realizedPnl另列是既有设计，不把不一致包装成完整cash reconciliation。

# 最新：v41账户基线/创建历史起点已Linux1174与真实账户副本重启通过

上一目标轮v40完成净额记账与3035完整回归，是实质进展。本轮继续正式代码：新增bulk_account_baseline、提取共用closed_original_boundary并保留原require_idle整worker空核心限制；BulkAccountVenue捕获一次全账户经济起点，跨市场/凭证轮换/重开不重置；BulkGridVenue.history_cursor_from_snapshot接既有创建钩子（只读预览仍原CAPABILITY拒绝，有执行资格缺baseline则拒绝）。全部组件默认不授交易/恢复资格，不推进全局history/funding coverage。

本机相关120通过（新baseline组22）；Linux精确v40父源继承 **1174pass/0fail/error/skip**、test_exit0/source_unchanged，finished1793412381273，XMLSHA abcf0dc9a70f1814c2e200069b7a24f7979c71fa208403f882bcefacde950cc9；原PID3849270已terminal。没有未处理测试/原生helper handle，不重跑。候选 `/root/code/entropy-grid/data/staging/bulk-account-baseline-v41/source`，520manifest/6overlay，归档SHA f6ce3db5229cb11f3602b0203e75f2a491c1a0f5ee661dff0a4fe8bb8f1a3b2f。Bulk.py仅新增三个方法，normalize后与父源SHA完全相同；CLI仅提取require_idle函数内部检查，其他代码逐字相同；签名/发送模块未变，未混入本机HL改动。

**实际生产Python/SDK0.1.30只读副本核验已exit0并实际下载解析/验hash**：grid-bulk-account-baseline-native-v41.json checked1793412460614，SHA a59288dcd39994a487119d0d114bb67eea19f9d819f4eae313e07dd35072cb21。一次baseline+一次审计事件，BTC/ETH共用origin，重复调用0新HTTP，真实Runtime/Store重开保留指纹defb65946f84a2183fdd8deb711891179b542ee88c0f8eae80862b13671972f3；各市场cursor作用域不同/同slot640469011。原18intent/总27金融记录及nonce/OID绑定逐字不变，5服务身份未变，6公共账户查询、0金融/0密钥/0服务控制/0生产安装。Native全账户零仓零单、结清margin500.691276，fees-.0042/funding-.000018782/realizedPnl-.005505。副本integrityok、SHA f32fd47687bd1f02a916d8534eb6aef18c471d4f85fd192a47481d2dd84ff4dd。helper planning/grid/tools/verify_bulk_account_baseline_v41.py SHA92a082304cfef5c29a3d3211a2c046ae2bc640a7645806a8c1f4008c4b5be9d0。

**两个重要实测事实**：Native fullAccount实际没有pendingOnFill/lockedAmounts，不再把get默认值叫这两项原生零证明；baseline依靠明确空仓空单/原闭合请求/结清margin。原生history.asOfSlot在空账户等于最后账户活动slot，不是当前链头；baseline必须保存/排除全部旧原请求，不能单凭slot相等猜归属，也没认证全局history。详细report reports/2026-10-31-bulk-account-baseline-v41.md；实现/恢复后续约束 BULK-OWNED-RECOVERY.zh-CN.md。

**接下来直接接全部自有范围恢复，不能停在baseline**：现有v30 _backfill_owned_positives只缓存终态/<=4轮询、v40净额<=16原单且必须全部终态/全部归零，正常网格恢复时仍有OPEN订单与持仓，所以还不能用。需共享批量Native openOrders/orderHistory响应、逐ID绑定/累计量与fills对应，长运行分批和终态缓存；包含已证实同OID触发止损，parked未知不能伪装cumulative_known；closed+current全生命周期资金费/费用与全账户基线累计变化互证。当前position原始字段有fees/funding/realizedPnl但官方SDK会默认或忽略多项，必须读取原始数字文本，缺字段拒绝，不能套不存在的openTime/maxQuantity。再通过明确account-owned范围证据接RecoveryBarrier及资金费恢复出口，全局complete/cursor仍不改。旧止损cancel用户信任政策继续，但不能假称普通运行缺失root成交=0。

完整Bulk上线目标active；正式仍v23/GRID0，HL/套利未改。没有真实普通网格/补单/保护重挂/持仓故障/小残仓/24h新证明，需继续普通执行工厂及真实有限验证。所有旧v33/v37金融RID禁止复用；不重复已完成探针/不等官方/不恢复AI定时检查；10min额度不能自动扩24h。v41仅在独立副本建立baseline，正式执行前由新运行库在独占控制下重新捕获生产起点，不人工搬副本记录当资金权限。

# 最新终局：v40同源完整3035与实际资金费副本重启核验均通过

本轮是实质进展：v40资金费净额已接入正式同步器和统一收益，相关Linux1152、同一冻结源完整 **3035pass/0fail/error/skip**；完整原PID3831447已terminal，test_exit0/source_unchanged，finished1793411359074，XMLSHA75eb6e98708f3ee5cdfa0c451d24c7df813033c458b16ab6d6e02f0ef475e1ac。没有需要重启或继续等待的v40测试/原生helper。完整报告grid-bulk-funding-net-full-linux-v40.json已实际下载解析，确认与1152相关测试同一source_manifest且包含其全部测试文件；无未处理handle，不重跑测试。实际生产运行库SDK另只读确认仍0.1.30。

已下载并核验的真实副本报告/相关测试及源码冻结身份见下段；原生副本三次（含真正重开Runtime/Store）资金费净扣.000018782、原payment20位不改、调整差.000000000147494374996、手续费.001051、净损益-.0003112572；只有1净额组成项/1应用事件/1payment/1payment事件，没有重复。原27金融日志及5服务身份不变，0金融/0密钥/0服务控制。全局历史仍unknown/无cursor/资格false。原失败日志另保存在本机data/run/grid-bulk-funding-net-native-v40-initial-stderr.txt，不覆写历史。

完整Bulk上线目标继续active，正式生产仍v23/GRID0；下一主线是明确全账户起始baseline、全部自有订单（普通+触发止损）的有界分批及closed+current持仓净额/费用累计互证，再接普通生命周期与maker补单、保护重挂、带仓故障/小残仓/24h。不要回到重做已完成诊断/等待官方/要求全局旧历史complete才恢复，也不能伪造complete。v40仍仅闭合生命周期、单次<=16原单，不代表长期持仓恢复已完。最新BULK-LIVE-GOAL矩阵已按v23–v40真实证据更新，防止重复已完成容量/ACK/止损工作。HL/套利未动，不开AI定期检查，不复用旧金融RID，不把10min资金政策扩成24h。

# 最新：v40区间资金费已进统一收益；Linux1152与实际副本重启通过，完整回归3831447在跑

上一目标轮在资金费事务重查发现保护字段读取问题并完成修复，属于进展；随后受自动审批额度耗尽影响的测试未执行，未绕过审批。本轮实际重跑后相关106通过，新增竞态/生命周期组16通过；冻结精确v39父源，只移植sync_funding/performance/new sync_funding_net等本次方法，未复制混合HL的整个Runtime。v40相关Linux **1152pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793410903515，XMLSHA a825ea22094c32a7c2ce14e31d62778373b183cd075ce890969584ec8fef212f，原PID3828198已terminal。

**当前仍在跑的唯一测试是完整网格回归 PID3831447**，stage `/root/code/entropy-grid/data/staging/bulk-funding-net-v40`，原full-pid/full-stdout.txt/full-stderr.txt，报告ROOT/data/run/grid-bulk-funding-net-full-linux-v40.json。最近已实查进程present、约45%，尚无终局；必须先查同一进程/报告，不因观察期结束或瞬时SSH问题重跑。没有新金融任务或服务切换。

**实际生产Python运行库副本核验已exit0并下载解析**：grid-bulk-funding-net-native-v40.json checked1793411144747，SHA2f6ac06ea818a21247219ade82d51f69d9fb8b8c7a490f1522671843c28dcb51。原v33已结束两单/两成交重建在原副本，首次/重复/重开均1区间净额+1原支付，fee .001051，资金费净扣.000018782，原payment .000018781852505625004完整保留，净额差.000000000147494374996，净损益-.0003112572。全局funding仍INCOMPLETE/无cursor/恢复资格false/needs_attention。Native全账户flat/noorders/pending/locks0、500.691276；原27金融日志和5服务身份逐字不变，53公共查询/0金融/0密钥/0服务控制。副本integrityok，SHAe373cc6b4d2be81981ac426a6b2b502c6f3f48f4f0e40a09a400563176a2f8b9。原native helper初次因历史副本无ROI版本配置在成功入账后退出；失败日志保留，未删除/重建原副本或改冻结源。新的纯查询续验helper complete_bulk_funding_net_v40.py仅在原副本补明确标记的测试展示配置，不报告假ROI/真实资金分配，然后完成以上核验，SHA14ab83759094de0cbff4f9401fa0d23d46d43fe8bf353369516b8ef802f7750d。

候选source516manifest、5overlay，archiveSHA ae3c5fb75a714b061d4f1f214fec5c711e48da85f94b1755bdea7548ff7ba077。新funding_net模块使用实际持久fills摘要+原绑定，commit再验请求，区间/跨grid防重/归属生命周期；raw支付与net独立，统一收益同一读事务防重复收费。原全局历史错误保持，不伪造payments或coverage。详见 reports/2026-10-31-bulk-funding-net-v40.md。

**下一主线是普通账户起始baseline与已拥有范围恢复**：必须从flat新鲜Native全账户快照固定fees/funding/realizedPnl及旧请求身份边界，涵盖同账户多市场；全自有原普通单/触发stop累计与实际ledger匹配、closed+current position净额与账户累计变化互证，额外资金/外部仓单不得悄悄归属。不能继续要求未知全局旧history才能恢复，也不能翻其complete。v40仅闭合生命周期、单次<=16原单，仍需有界分批和current持仓净额接线；最新native当前持仓schema可从v37原470帧查看，只有size/price/realizedPnl/fees/funding等，没有openTime/maxQuantity，不能套closed schema。尚未实现完整owned-scope恢复/正式资格/普通补单、保护重挂、持仓故障/小残仓/24h；不回头复做已完成诊断或等待官方。正式Bulk仍v23/GRID0，HL/套利未动，完整目标active。旧v33/v37金融RID禁止复用；10min资金授权不自动扩成24h。

# 最新完成：v39 兼容修复Linux1115与实际生产运行库只读核验均通过

两份v39报告已实际下载并本机解析核验，不再有待轮询handle。最新详细报告 reports/2026-10-30-bulk-native-compatibility-v39.md；本机全部Bulk1022与Linux1115均通过，普通生产未变，下一步转资金费净额及正常恢复接线。

原helper会话94088已exit0，不再等待或重复：**grid-bulk-original-compatibility-native-v39.json** checked1793373068036，实际Bulk Python/SDK0.1.30；对原已CLOSED RID的startup fence通过，保存的2事件ACK精确重分类为原entry ACCEPTED/bulk_filled/.00001951，**原UNKNOWN receipt原样**。原Native entry及Root均FILLED/累计同.00001951；实际470原始帧在generation1转换44事件，经真实BulkAccountVenue与当前REST确认2条final fills；所有27条金融日志逐字未变，5服务身份未变，新鲜Nativeflat/noorders/500.691276。8次公共/account HTTP，0金融/0密钥/0服务控制，没有GridRuntime writer/vault/signer/order transport。全局历史仍unverified/普通资格false。最终两份v39报告正在取回（当前scp handle可poll），不要重跑原helper写同OUT。

本轮是实质代码与实际运行库正面证据；**完整Bulk普通网格上线目标继续active**，生产仍v23/GRID0。下一步从资金费净额/明确账户起始baseline/已拥有范围恢复接正常执行，不回去重测已完成ACK/自然止损，不恢复AI频繁跟进，不把只读核验称普通实盘通过。旧v37/v38所有金融RID不可复用，普通网格maker补单/保护重挂/持仓故障/小残仓/24h仍待完成；24h范围仍不能从10min测试扩张。

# 历史：v39 回执/已成交止损启动/真实持续快照兼容已过Linux1115；原生只读核验94088

上一目标轮为实质进展（真实止损闭环+v38修复/纯查询收尾/独立备份），本轮继续改正式Bulk组件，不重跑原金融RID，不复做同类止损。当前无任何新金融授权/grant/policy/unit/资金请求；普通生产仍v23/GRID0，HL/套利未动，Bulk observer3301268/web2298148正常。

已实际导出原无签名回执及公开原订单元数据 ROOT/data/run/grid-bulk-original-compatibility-input-v39.json，并取回本机，SHA75e100de556574f3d7c7c708c6f785c0ad581545bd151847f9252751677da7ee；新fixture multi-event-ioc-mainnet-20261030.json。submit_receipt现遍历合法execution-event statuses，只接受唯一精确原OID；不认领maker事件、同OID重复/格式不明仍UNKNOWN，数字/方向/数量与原限价检查补齐；旧MAY_HAVE_SENT仍只查原单，不覆写旧UNKNOWN。单事件授权错误语义保持，签名函数signed_body与BulkOrderTransport发送类已在freeze中与v38父源逐字校验相同。

require_idle新增对精确绑定Stop的Native FILLED/累计已知/非parked/正累计<=固定上限/原触发参数完整/domain+hash匹配的允许，取消证明原语义不改，矛盾取消/未知执行仍拒绝；这只是旧原身份闭合检查，仍需新鲜原生账户及独立真实授权，不认证全局历史。

**新真实运行根因已修复**：473行原公开WS JSONL内实际有470条原生帧，包括455次accountSnapshot，而非只初次发送；旧decoder把第二次snapshot当异常重连。新decoder验证所有快照后对相同完整帧去重，改变的重复快照只产生account_requires_reconciliation/递增private revision，不制造运输断线、不清除原恢复屏障；外部挂单仍置gap。begin/reconnect重置摘要，首次快照始终需要恢复。470帧已保存完整原始字符串fixture native-ioc-stop-stream-20261030.json，不把float重序列化作为精度真值。

WS普通原IOC以market/filled显示时，仅在精确绑定的原limit/IOC和一致字段下作临时观察归一化，不改journal；Stop原OID的market/filled通过v38精确分支。补齐WS到reader的tif/vwap字段；所有推送订单标记source stream、累计非权威，最终成交必须匹配REST。根单状态也纳入有界重复/乱序守卫，终态后旧stop/resting不能重开。真实470帧成功转换44条有效事件（其中2条provisional fills、1条普通filled、3条保护变化）；与真实REST两笔成交精确匹配，真实loopback WS无需重连，实际BulkAccountVenue ingest/confirm_pending得到2条REST final events，但完整历史仍unknown/gap保持，未虚报恢复许可。

本机针对157pass；全部Bulk1022pass。冻结 **ROOT/data/staging/bulk-native-compatibility-v39/source**，513manifest，11项覆盖，归档SHA **d9b717adab13de60834a47d7cc7d6c91f74bc32314e604b53d0b6e3ce775f92c**，继承v38精确基线，不复制本机其他HL修改。Linux原PID3332949已terminal：**1115pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793372854608，XMLSHA **9edc57f2bba6bbbb3a034a99845303a4a8e3527b6eceb2641f7ebea31bd29a51**。报告grid-bulk-native-compatibility-linux-v39.json尚待取回，勿重跑原测试。

**当前原生只读helper已启动，本机exec session94088，先poll原handle/报告，不重复调用写同OUT。** 使用Bulk实际生产Python/SDK0.1.30和冻结v39源，执行planning/grid/tools/replay_bulk_native_compatibility_v39.py；OUT ROOT/data/run/grid-bulk-original-compatibility-native-v39.json。它只读原生产库（不构造GridRuntime writer/vault/signing/order transport），验证已CLOSED原RID的27日志原样、原UNKNOWN回执保留、新startup fence通过、旧响应重新分类与原Native entry/Root正面累计；回放原470帧到真实BulkAccountVenue及REST确认，最后新鲜Nativeflat、5服务身份未变。只有公共/account HTTP白名单，无金融/凭证/服务控制，没有重启旧金融RID。现在尚不可称该原生核验通过。

下一主线仍是普通网格可用，不能停在兼容性：资金费全局历史unknown仍阻止常规恢复，需要明确起始account baseline、所有拥有的生命周期/当前仓位与原订单累计量/fees/funding Native净额对账，使用独立净额调整而非伪造payment/全局complete；再接真实普通创建/启动/暂停恢复/结束、maker补单、保护重挂、带仓故障/小残仓与24h独立终局。本轮是必要原协议修复，不是普通实盘已开放。原v37/v38证据继续有效且金融RID均禁止复用。正式24h范围不能从旧10min验证自动扩张。

# 已完成：v37真实原生止损闭环；v38精确识别、无签名收尾及独立配套备份通过

最终报告/原失败/原run/473帧公开WS原始JSONL均已实际取回并本机逐项解析核对，无下载凭证。WS SHA41fe6f2eb0b0ceb866b22304f5b46f6f6069691841a26a3acc5483f5c52785d9；settlement报告SHAf9ec81567530f603ceddc4f580babee4f52135b61c6b816b42ac5d7808d11491。另已完成独立SQL原请求保全核验 grid-bulk-trigger-request-preservation-v38.json checked1793370702669：金融前25条→当前27条，仅新增entry/stop这两笔，无任何退出或撤单新请求；原CLI到期快照 BULK/data/run/bulk-trigger-20261030-v37-1793370131836433470.sqlite3 SHA df1376d4f19042812b30dfe8466d3b2f54d70784ba861c5a75e9e0506f3972d7；比较当前27条除native_evidence/history追加外所有动作/nonce/OID/原UNKNOWN receipt逐字不变。实际交易前Native margin500.694526已从原launch确认。该证明最后scp若文件未到，查当前原handle，不重复审计写同OUT。最新总报告 reports/2026-10-30-bulk-native-stop-v38.md，目标矩阵已更新。下一主线见下方编号1–4，不要再重复同一止损探针或原RID。

本目标轮有实质代码、Linux测试和真实主网新证据，**完整Bulk普通网格上线目标仍active**。当前正式Bulk生产仍v23/GRID_REAL_DISPATCH=0；没有开放普通网格、没有24h资格。不要再重跑金融RID **bulk-trigger-20261030-v37** 或其任何launcher/grant/policy/收尾helper。

**实际已CLOSED**：原自然止损买卖均.00001951BTC，entry@76942.63、原Stop同OID市场成交@76829.85，fees .00105，账户总损耗.00325。原policy1793369231830–1793369831830，financial cleanup到1793370131830，未改。所有金融执行在原期限内；旧v37仅因把同OID的stop→market误当类型冲突，最后到固定期限正常结束并恢复observer。**保留其旧cli_exit2/原独立audit失败**，不能把旧程序自身报成功；后续只是无签名查证/终态追加，未延期政策/重发/开新仓/新退出。

v38已据真实原始响应修复该精确同OID转换，仅接受绑定Stop原请求、相同原trigger、sell/ROtrue、terminal market/ioc/filled、0<原生size<=原固定数量、executed==size且price=vwap>0；正面累计量已知，非parked。原triggered事件仍不代表累计已知；不同OID不猜归属。真实夹具+反例已覆盖。候选 **ROOT/data/staging/bulk-trigger-execution-v38/source**，508manifest、6项覆盖，归档SHA **b7763a755e1717b649e38721bce0af5cc8b9ace64819cfa5d6269dd47ff8e3e5**。Linux原PID3297976 terminal：**161pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793369993138，XML **8d16378daaececf394f863dd9baf5d9a5410153e73d61b45a0bf25380589882a**。父v37完整2955及相关1051均已通过；v38没有重复不相关全量，也未覆盖活动v37源或混入本机HL core变化。

**无签名收尾已实际成功**：grid-bulk-trigger-settlement-preflight-v38.json checked1793370203074先独立确认原entry/root均Native FILLED/累计同.00001951且全账户flat，并验证原v37源未动；此时原financial unit已inactive/MainPID0，observer是3300101。唯一readonly handoff unit entropy-grid-bulk-trigger-settlement-v38.service 只暂停Bulk observer、用新v38 helper在原GridRuntime独占追加证据并恢复观察。controller report helper_exit0/finishedtrue，finished1793370218308/restore_exit0/observer **3301268** active。helper **grid-bulk-trigger-settlement-v38.json** checked1793370218015，11次公共HTTP，无vault/signer/financialtransport/servicecontrol/新policy；原始精确fills逐OID与Native累计对应、净0、费用与baseline差.00105精确一致、3次freshflat且双时钟>=5秒。原intent/nonce/OID/action/UNKNOWN receipt均保留，只追加NativeEvidence及其历史；原诊断 phaseCLOSED/finishedtrue/native_flat_no_orders/stop_terminal_verified/**native_trigger_execution_proven=true**，closed1793370217973。global history/普通资格仍false。该最后只读终态写入发生在原金融期限之后，不等同续期交易。

只读数据库快照 BULK/data/run/bulk-trigger-20261030-v37-readonly-settlement-v38.sqlite3 SHA **0d8d919bd1732b17e56a7777bccdf521bd85624f100280e2ec966938923afc4b**。独立配套备份 **grid-bulk-trigger-backup-v38.json** checked1793370402183再次确认Native全账户零仓零单、500.691276；27条原intent/cancel与备份重开逐字核对、integrityok、seq0、3配套文件hash。目录 **BULK/data/grid-backups/grid-20261030T142641Z-2cc9d828**，DBSHA **7902f7f0351adcffc2ef239c909490cfd98043990fa4b017c194c1fe89e0bbe4**。凭证只opaque服务器内备份、未解密/下载；HL161833/watchdog161838/套利198980/Bulk web2298148自原金融启动前至今身份完全不变。

最近checked1793370292191，原financial unit、readonly settlement unit、公开WS采集unit均inactive/MainPID0/exit0，无活动金融测试/定时器；Bulk observer3301268正常。报告/原失败/原run/完整公开WS原始JSONL正在同一scp本机会话下载（见当前tool handle，先poll；别重跑任何原请求）。v37/v38 Linux证明已取回。本轮无需再花资金复做相同止损样本。

**下一执行主线必须从真实数据接正式行为，不再绕回空探针/等官方：**
1. `bulk_roundtrip_cli.require_idle` 目前旧Stop分支只接受CANCELED，尚不接受已经确证FILLED的同OID市场止损；下一次启动会被这个旧门挡住。应接受绑定/原hash/Native终态FILLED/累计已知且<=固定上限的正面证明，刷新原Native并独立freshflat仍需；不能翻unknown或仅凭flat放行。
2. 本轮已抓到真实HTTP ACK UNKNOWN根因，**并非节点没回**：一次IOC响应有两个execution-event statuses，第一项working是被吃的maker OID Dp7XJov3JrHFN1YmUBRreW4bZjLNcQBZxrmccP1Wsvgc，第二项filled才是自己的entry原OID GJU4.../totalSz.00001951/avgPx76942.63。当前submit_receipt只接受单项，下一步按精确持久原OID选择唯一自有事件、验证数量/方向/价格，其他maker事件不认领；仍保留原UNKNOWN记录，不覆写为新ACK、不重发。这些原回执在生产runtime_state `bulk-trigger-evidence:RID` 的submission-response中，工具已输出全部，无签名/密钥。
3. 真实WS普通IOC entry也以ot=market/filled上报，但REST是原limit/ioc；新Stop同Root market/filled如上。普通stream仍需基于精确原绑定+REST确认兼容，不能因这一类型表现持续gap，也不能仅凭WS修改请求/终态。
4. 正式资金费区间净额/明确accountbaseline、已拥有范围恢复、网格普通创建启动暂停恢复结束、maker买卖补单、保护归零重挂、真实持仓故障/小残仓、24h独立终局仍未完成。已有原普通累计量/fills、closed native-net及非零funding payment候选继续复用，不伪造全局history complete；普通执行保持未认证直到相关实现和真实验证齐备。24h资金/期限不可从本次10min小额自动扩张，先把可审阅的普通执行版本做实。

# 历史：v37原生止损已真实全平；原程序因stop→market识别等待，修复v38中

**不要再买入/重启原金融RID。** 唯一RID bulk-trigger-20261030-v37、原unit entropy-grid-bulk-trigger-20261030-v37.service/MainPID3287729，原policy approved1793369231830/expires1793369831830，cleanup到1793370131830，不能延长。原生stop OID **5Vu7edkreND9pjtDSzBFCJXekJ4ntTyhnShYNoYXcueq**，entry OID **GJU4f8bH6jGhj4tadZeBPZUgrJVbKQ2HXc3fqHnqn1g5**。entry原UNKNOWN始终保留、正面累计 .00001951，实买 .00001951 BTC@76942.63（slot640464735/seq18，ts1793369259212376427）；原止损原生触发并全卖同量@76829.85（slot640469011/seq19，ts1793369293419681487）。费用各.000525、总.00105；最新独立Native checked1793369370994 **全账户零仓零单/pending/locks0**，余额500.691276、累计fees-.0042/funding-.000018782。资金实际闭环已发生，但旧程序未识别不能伪称状态已finished。

**关键协议事实由这一次真实REST+WS证明**：触发后沿用原Root同OID，REST历史从stop/gtc变为 **market/ioc/filled**，保留相同原trigger {isAbove:false,px:'76833.01'}，side sell/reduceOnlytrue/originalSize=executedSize .00001951，price=vwap76829.85。WS先有同Root stop/resting，再stop/triggered，再同Root sell fill与market/filled；不是未知新子OID，不需要按价格时间猜归属。原普通entry WS也写market/filled而REST为原limit/ioc，此兼容点后续普通stream仍待处理。原v37解析器kind stop只认stop/gtc，故当前TRIGGER_CLEANUP/observed_qty0/finishedfalse/waiting BULK_BOUND_STOP_CHANGED，未多发RO退出。原controller仍可能继续只读等待至固定期限，必须先查原unit/账本，不换新金融身份。

真实诊断 **ROOT/data/run/grid-bulk-trigger-diagnosis-v37.json** 已取回本机data/run；完整公开WS原始帧 BULK/data/run/bulk-trigger-20261030-v37-raw-account-ws.jsonl，readonly unit entropy-grid-bulk-trigger-v37-readonly-ws.service 已启动，controller结束后自动收尾。诊断含REST原值与6条关键WS事件；原始WS文本在JSONL保留，不以诊断工具json.loads后的float作为精确费用证据。

本机新修复v38（不覆盖活动v37冻结源）：BulkBoundEvidenceReader对**已精确持久绑定的stop原OID**仅接受terminal market/ioc/filled、原trigger完全一致、sell/ROtrue、0<originalSize<=原固定size且executedSize相等、正price=vwap；返回已知累计/原trigger、非parked，不接纳其他ID或非终态。新增真实夹具tests/fixtures/grid_bulk/stop-trigger-mainnet-20261030.json及18个边界测试。

新增 bulk_trigger_settlement.settle_flat_trigger 是**纯查询收尾**，内部只有SimpleNamespace owner/network，无sign/prepare/send/cancel或密钥：原entry+root+所有既有exit必须正面终态累计已知；Root必须FILLED；精确原Native fills逐OID数量/方向相符、净0、费用与账户baseline差精确相等且<=.01；连续三次新鲜flat、双时钟>=5秒后仅追加NativeEvidence并将原诊断CLOSED。保留全部原nonce/action/receipt和原policy，不构造金融请求/不续期，可在原政策到期后以只读证据完成收尾。该函数只能诊断空core库，普通资格始终false。新增7项无签名集成测试；最初唯一断言失败是忽略journal原本会追加native_evidence_history，已修正验证原历史前缀和其余全部原字段保持。

新server helper planning/grid/tools/settle_bulk_trigger_v38.py 尚未执行：要求新v38 Linux proof、原financial unit inactive/MainPID0、Bulk observer inactive，GridRuntime独占写原证据/状态；纯公共HTTP白名单，无vault/signer；原RID和policy不变，OUT ROOT/data/run/grid-bulk-trigger-settlement-v38.json，数据库备份BULK/data/run/bulk-trigger-20261030-v37-readonly-settlement-v38.sqlite3。后续应先冻结v38并完成针对性Linux测试，再用新reader做原Root/entry和新鲜flat只读预证；确认后只停止旧financial unit（原代码不变，保留其未finished报告），通过有期限且ExecStopPost恢复Bulk observer的独立readonly收尾unit暂停Bulk observer、调用helper、恢复观察。**不启动新金融测试、不重发原买单、不更换/延长policy。** 原v37full2955已通过，v38仅需针对新分支/收尾的相关Linux，不能为了省事修改活动v37源。

本机最新测试会话24402：针对新Root/收尾/边界+原查询/保护执行，原前轮154pass；最近r2为160pass/1断言失败已改，r3尚未取回。先poll该handle再冻结。新v38尚无stage/PID/金融行为，完整Bulk普通网格目标仍active，正式v23/GRID0不变。HL/套利未重启。

# 历史：v37 全量2955通过；唯一有限主网原进程3287729已启动，先查原身份

全量原测试已出终局：2955pass/0fail/error/skip，test_exit0/source_unchanged，finished1793369065598，XMLSHAee1b66cfaa64e368b47ab6c3934025f3fcca35675edfe9fa650fc81ffa0feb94；旧PID3278472刚写报告时仍存在，不能重跑。Linux相关1051和503manifest均已验证，普通生产源仍未替换。

已按站立授权实际启动唯一新金融RID **bulk-trigger-20261030-v37**，unit **entropy-grid-bulk-trigger-20261030-v37.service**，MainPID **3287729**。原grant与launch已建立，launcher返回start_exit0。2026-10-30 checked1793369209069，原run.started_ms1793369183058、cli_started=false，仍在旧原单/排他Runtime预检，尚不能称买入/持仓/触发。必须先查该unit、原run、policy、SQLite bulk-roundtrip:bulk-trigger-20261030-v37 和原audit；禁止重投launcher或换RID绕过未知。初始第一次launcher在只读HTTP账户检查遇503，随后独立实查所有grant/approved/run/launch均不存在、RID账本0、unit notfound、原API恢复200，才继续同一尚未创建身份；该首次失败没有金融请求。

原controller3287729已正常暂停Bulk observer（之前2718590），10min固定policy要在预检后创建，额外5min仅退出/撤单；RuntimeMax1050s、ExecStopPost恢复仅Bulk observer。HL161833、watchdog161838、套利198980与Bulk web2298148无改动。另刚提交同RID公开WS只读取证unit **entropy-grid-bulk-trigger-v37-readonly-ws.service**，本机exec20260尚未取回结果；先查该原handle/unit/JSONL，不能重复启动。helper SHA5505c39c9c77d54a82f67c234130a8cb3428a65bfc8cd3ade624b815168acb90，RuntimeMax960s、内部900s，controller结束后自行结束，无资金/服务控制。

后续完成后独立审计OUT为ROOT/data/run/grid-bulk-trigger-native-v37.json；结果仍区分金融诊断清空与trigger child正式归属，不能因为flat就宣称网格资格。下文“尚未激活/全量26%”均为启动前历史，不是当前。完整Bulk上线目标仍active。

# 历史：v37 相关Linux1051已通过；同源全量原进程3278472

v37相关原PID3274061已terminal，1051pass/0fail/error/skip，test_exit0/source_unchanged，finished1793368447229，XMLSHA03a1d24b0d5dd01a55cdee6b37e26b1838e6ce57b9d7f5c2ad7764665e64bf53；报告data/run/grid-bulk-trigger-observe-linux-v37.json已取回本机。原全量已用同503manifest启动，PID3278472，stage/full-pid/full-stdout.txt，报告grid-bulk-trigger-full-linux-v37.json；最近实查live/26%，无终局。先查同一进程/报告，不重跑prepare。下文“相关live/34%”是启动时历史，不是当前。

本机额外准备了纯公开WS原始帧记录器 data/run/capture_bulk_trigger_ws_v37.py，已上传ROOT/data/run同名文件，**尚未启动**；无vault/SDK/signer/order endpoint/服务控制，单账户公开subscribe，原始文本0600 JSONL、512帧/4MiB/15min上限、controller finished后结束，最多3次连接。可在新金融unit启动且grant确立后立即启动独立有期限readonly unit，收集同一次真实触发的原始消息，不单独制造新交易。记录不是归属/coverage认证。原金融RID bulk-trigger-20261030-v37仍未激活（无grant/policy/unit/金融发送）；启动前仍要核查原身份。

2026-10-30用户询问HL后已只读核对原受保护24h终审通过，原85网格全CLOSED、无活动operation/未知intent，HL161833/watchdog161838/套利198980未重启。该状态答复不算Bulk代码进展；本目标轮已继续实质实现v37。

新增BulkBoundEvidenceReader正面原OID聚焦查询：所有原始行仍约束owner/lane/id/symbol/全局slot顺序/页界和opaque cursor，但只有目标OID作语义解析，过滤页永不签发完整coverage。普通原单不再被同账户triggered根单/未知market子执行阻塞；全账户history/recovery仍严格。精确根单triggered/triggerFailed保留NOT_FOUND_TRANSIENT、累计未知、不假认子单。去掉本次lookup覆盖后bound_read.py字节SHA精确等于v36父源。

新增有限BulkStopTriggerProbe/CLI：单次<=2USD买入前预挂mark下10bps固定RO止损，最多观察240s，10min+5min仅退出范围不改；最多5个RO退出、费用<=.01，原CID/OID/nonce/价格请求持久不变。新鲜native量且原entry累计正面终态才允许RO退出；flat时仍查每个原普通退出终态。允许只观察未知RO子单，不认领、不记账、不撤外单；flat三次且>=5秒、原root正面事件才能结束诊断。原triggered根单仍非完整执行终态，结果明确trigger_lifecycle_certified=false/live_qualified=false。原始无签名响应/账户/历史单独限额保留，不缓存签名/密钥，不改原授权hash。超时/取证上限只退出，不新增买入。

新增有界controller与独立unsigned audit，复用旧范围、排他Runtime/vault、仅暂停恢复Bulk observer，不触碰HL/套利；旧未发送请求先本地封存证明验证，其余原订单正面刷新。controller模板只在preflight后固定一次真实10min policy，不延续旧RID；audit区分financial_diagnostic_closed与正式止损子单/网格资格。**目前未创建grant/policy/unit，未发金融请求。** 本机新集成及相关204pass；最初fixture用了非原生金额简写导致7项audit测试拒绝，已改真实规范金额格式，原失败log保留。

候选 /root/code/entropy-grid/data/staging/bulk-trigger-observe-v37/source，继承v36并覆盖10文件、503项manifest。归档本机data/staging/bulk-trigger-observe-v37-r2.tar.gz，SHA9f0057fd3b82dff147fa73f36ffbff298dc0d2047a5183f6faee736d2e1a1056；已覆盖v36副本helper修正52f6...，未混入本机HL recovery。旧本机v37.tar.gz是在新fixture修正前准备且从未上传/执行，不用它。Linux离线相关原PID3274061，stage/pid；报告data/run/grid-bulk-trigger-observe-linux-v37.json。最新实查live/34%，无终局；先查原进程/报告，不能重复prepare或因观察超时重启。

本机待激活launcher data/run/grid-bulk-trigger-observe-launch-v37.py要求该相关及同源全量Linux通过、实际生产SDK0.1.30及v24签名原模块SHA未改、新鲜pristine独立账户、旧原身份已清理。新RID计划bulk-trigger-20261030-v37，**尚未激活**；执行前核查同RID原launch/grant/approved/run/unit存在性，不重复发送。完整Bulk目标仍active；普通v23/GRID0不变，后续仍需正式账户净额恢复/真实网格maker补单/保护子单精确归属与重挂/持仓故障/小残仓/24h终局，不把诊断完成当产品上线。

# 最新：v36 正面资金费已实际副本入账/重开去重；全量2907通过；普通实盘仍未开放

本目标轮有实质代码与真实新证据。FundingPage新增可选FundingPositiveEvidence，Bulk不再丢弃不完整页里的正面支付；FundingSynchronizer已接实际sync路径，验证account/market/source/observed bounds和各支付key/asset/精确amount/ts/source_seq，按真实grid lifetime唯一归属。重复/冲突/源变化/歧义/不同币种/事务注入失败均覆盖。支付、owner、幂等事件、原生正面证明与FUNDING_HISTORY_INCOMPLETE状态在同一事务提交；**不推进coverage、不清除缺口、不从position-net伪造payment、不开放交易或恢复**。随后完整历史同key重放不双记；归属前支付仅保留未归属证据，不收费给后策略。Optional默认None的HL既有路径保持行为。

**候选** /root/code/entropy-grid/data/staging/bulk-positive-funding-v36/source，495manifest，归档SHA0698286bc7c957e33da840d8310e098a6fe94ee426e774e87aef7696d79b4a17。仅base.py/Bulk.py/funding.py新功能：normalize去掉本次变更后3模块逐字SHA均等于v35父源，无混入本机HL recovery变化。相关1003pass/0fail/error/skip，finished1793328090523，XMLe0156fad2428da0a7c909a714813a738d62cbbfb0588e7759f8918603707e838；原PID2748397已terminal。**同源全量2907pass/0fail/error/skip**，finished1793328617761，XML1f78322e396ecc890ffbd883dd139ab95deaceafda514a8af2e7e178e4a8e479，原PID2752371已terminal。不要重跑原测试。

**真实API到实际Runtime副本已通过**：grid-bulk-positive-funding-native-v36.json，checked1793365436499，原probe RID仍已结束的bulk-funding-20261030-v33，不新交易。副本 /root/code/entropy-grid/data/staging/bulk-positive-funding-v36/funding-ledger-v36.sqlite3，明确synthetic历史会计 GID g-bulk-funding-replay-v36，不是普通实盘新grid。实际Bulk生产Python，用真实主网fundingHistory原payment键c26b77d296ad6029065cd40a45aaeedd8daf7d59444c5dd242dbc35b5d4e92f0/成本.000018781852505625004入账，首次1、重复0、关闭重开Runtime后0；支付/owner/证明event各1，Native精度不改为position-net .000018782。funding status仍okfalse/FUNDING_HISTORY_INCOMPLETE、cursor不存在、livequalifiedfalse。5次unsigned公共请求，无vault/签名/financial/服务控制，原25金融日志和5服务身份未变，Native前后flat/noorders/500.694526。副本integrityok，SHAcd7c4c3a521919a8cc501365cdea9d0c8c333fef12c45b0a114cfc364d6bbb2e。报告均已取回本机data/run（最后scp会话1648若文件未到需查）。工具已经结束，不能重复同OUT/重新建原副本。

首次副本helper在联网和支付写入前误用了不存在strategy_kind列，保留grid-bulk-positive-funding-native-v36-initial-error.json；实际核查原副本grids/payments/owners均0，原日志25未变，然后仅修正helper SQL，在同一原副本继续完成。**没有修改v36冻结source**；独立修正版在/root/code/entropy-grid/data/run/grid-bulk-positive-funding-native-v36-resume.py，SHA52f6fd647f49f860cd7c43c002b0c1fab985db14aa366144010edbd369102673。报告标记audit_resumed_after_schema_column_fixtrue。**下一freeze务必覆盖本机已修正 planning/grid/tools/ingest_bulk_funding_v36.py（同52f6...）**；旧冻结源里该工具仍44bccab...，不要再执行旧工具。修正元数据data/run/grid-bulk-positive-funding-helper-fix-v36.json明确只修工具、不改已测Runtime。修正原副本前曾确认其SHAe4a4a6a8.../0记录，未重置数据库或金融身份。

正式Bulk生产仍v23/GRID0，HL/套利/网站未重启。本轮只在独立副本验证已实现正式同步器代码，不能说已把诊断资金费写进生产或普通网格已上线。真实受保护跨整点、v33原财务收尾、v35终审与配套备份下文均继续有效且金融RID禁止复用。

**下一主线不要再重复整点探针/查空历史/等官方**：1) 将已有原普通单累计量与成交全序证据、Native当前/已平仓position.funding、账户累计counter与明确起始baseline接为可检验的账户/持仓区间净额对账；原生支付与应用净额不同精度必须分列，若采用净额修正需要显式独立调整/重叠防重，不伪造原生支付或FundingPage.complete。2) 使恢复使用完整的已拥有范围证据并保留全局历史未认证状态，缺失原单/外部仓单/账本差异仍阻止风险恢复。3) 普通真实grid创建/启动/暂停恢复/结束、maker买卖补单、Native止损触发及子执行归属/归零重挂、真实带仓故障、小残仓、24h/独立终局仍未完成。

可以独立推进下一次**原生止损触发/子单证据**小额受保护协议验证（新RID、真实带仓、自然价格触发后仅查询原身份/RO退出），不用重复funding时点；但先完成可审核代码与有界自动收尾，沿用已有每单<=2USD/合计<=4USD、fee<=.01、10min+5min仅退出/最多5个RO退出，不自动扩展新24h额度。当前无任何活动金融测试/定时器，无需重启本机网格或碰旧套利服务。完整Bulk目标仍active。

# 最新完成：v33真实非零资金费验证闭合；v35独立终审通过；下一步正式资金费入账/恢复接线

本目标轮实质进展：真实资金费正面记录、受保护跨整点持仓、一次买入与一次RO退出，以及误判修复/终局/配套备份均已完成。**完整Bulk上线目标仍active，普通生产仍v23/GRID0；不能把本轮当普通网格或24h完成，也不要重复整点探针。**

**唯一原金融RID bulk-funding-20261030-v33已结束不可重开。** 原controller service和timer均inactive；最后finished证明 data/run/grid-bulk-funding-v33-finished.json checked1793326752351，auditSHA5794673fafaee6cde2c2b1bd0b135400fcb73ab74c3c05e31bd9f24628cb99c4；HL/套利/网站身份与启动前完全一致，Bulk observer已恢复PID2718590。当前无活动金融测试或定时任务，不要重跑原launch/grant/policy。原首自动audit失败和controller.independent_audit_exit2都保留；之后独立修复版只读终审exit0，不覆盖原始证据。

**v35相关Linux原PID2727028已终结**：985pass/0fail/error/skip，test_exit0/source_unchanged，finished1793326494202，XMLSHAee602c228ef32c5934ad4f32b02a6fd4d06cfa71e55502ddad1627e4a0de1a2c，报告grid-bulk-fee-aggregate-linux-v35.json已取回。冻结 /root/code/entropy-grid/data/staging/bulk-fee-aggregate-v35/source，493项manifest；父v34 r2/973通过，父v33完整2867通过。v35只新增真实费用汇总兼容、夹具/测试和只读重审，不安装正式执行源。

**实际生产库只读重审已exit0**：grid-bulk-funding-native-v35.json checked1793326650577，scoped_observation_passedtrue，nativeflat、原stop FOUND_TERMINAL/CANCELED（累计未知保留）。原买卖均FILLED/累计同0.00001959，原精确fee .001051；20个保护在场样本跨UTC02:00，账户/持仓/closedposition funding净值均-.000018782。真Native payment cost .000018781852505625004，支付键c26b77d296ad6029065cd40a45aaeedd8daf7d59444c5dd242dbc35b5d4e92f0，slot635007334/seq271/time02:00:00.004485Z；applied净成本差.000000000147494374996保留，完整historycert仍false，0金融/密钥/服务控制，原25条日志未改。只读re-audit backup /root/code/entropy-grid-workers/bulk/data/run/bulk-funding-20261030-v33-independent-v35.sqlite3，integrityok，SHAe4a4a6a8c12fbbd67779d47b718fd3f828a4c7391d5a3840475c734657572939。这个工具已结束，不能重复同OUT/backup。

v35精确费用见证：2个原fee .000525/.000526精确和.001051，而native closedfee_cost .0010509999999999999。全部金额和复现binary64舍入用Fraction/整数，无通用epsilon，必须同原逐笔顺序精确重现native同bits且差额<最细输入费用单位半格；数量/价格/时间/身份仍完全匹配。原Native金额留在position，proof列exactfee及fee_reconciliation；无差异的老proof_id保持不变。真实closed proof_id5048b1b2544cfe43e758a8b5a89fd7d66b4adf84f526d6c6776e1c87db9cb08e。详情reports/2026-10-30-bulk-nonzero-funding-terminal-v35.md。

配套备份仍为 /root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261030T021240Z-25dccb6b：integrityok/seq0/3pairedfiles，DBSHA73ada9b77ec6830a7f9c0b56b00844d639242a0c8d414db7f7040858bc0bd5b1；仅opaque复制credentials不解密/不下载，独立nativeflat500.694526。report grid-bulk-funding-backup-v33.json已取回。原诊断、v33原失败/v35通过、v35Linux与最终finished证明均保留在本机data/run（最后finished scp会话52426需确认若文件暂未到），勿重跑金融或备份helper。

**下一执行主线应改变代码而非再探针**：当前 FundingSynchronizer.sync 在page不完整时直接拒绝入账，而BulkGridVenue.funding_history在不完整时丢弃正面records，故本轮真实原生支付尚未通过正式core资金费ledger入账。实现明确“正面支付幂等入账但不推进覆盖”的路径：保留账户/市场/原生paymentkey/slotseq/时间边界与grid归属，重复/冲突/重开/以后完整历史不双记；保留原生payment和applied position-net精度差，不制造完整coverage。再接入可检验的账户/持仓区间净额与已拥有原请求的恢复证据，不能翻FundingPage.complete或伪造全历史，也不能继续只用“不完整”空等官方。

后续完整要求仍含真正普通grid生命周期/资金预算/共享多币种、真实maker买卖补单、原生保护触发/归零重挂、持仓重启/断线、小残仓、至少24h与独立终局备份。已有独立Bulk主网真实操作授权继续有效，但沿用每次小额有期限/原身份/保护先在，不复用HL额度、不重跑结束RID、不恢复AI高频定时观察。下一正常24h资金/时间范围需结合已授权范围具体确定，不能悄悄用10min许可扩展。v34未发封存代码已测试，可用于下一候选收尾；原v33源不可覆盖。

# 当前：v33金融已CLOSED；真实非零资金费已取得；v35修复fee汇总误差，Linux进程2727028

本目标轮实质进展。**不能再把v33当待启动/有仓或重开**：RID bulk-funding-20261030-v33 原service已inactive/MainPID0/exit0，cli_exit0，probe CLOSED/finishedtrue/native_flat_no_orders/stop_terminal_verified。买/卖各0.00001959BTC（1次买入、1次RO退出），fee总0.001051；closed_ms1793325684789。原Bulk observer恢复active PID2718590，web/HL/套利原身份未变。原policy approved1793325461452/expires1793326061452，清理至1793326361452，从未延期。原买入UNKNOWN回执始终保留，原OID3Z5YyS2DUV1kpPaqzCdtodraS42WvPxcxQZyyNNVCsnw与退出BeJcggdn9dHCAG86oZmv7Z1SG6FNPKUPJrqawy47oboS已独立逐单FOUND_TERMINAL/FILLED、累计已知同量。20个真实带仓样本跨UTC02:00且stop_presenttrue，最后at1793325626547，account/position funding均-.000018782。

**真实正面资金费**：独立诊断grid-bulk-funding-diagnosis-v33.json（本机已取）HTTP200 fundingHistory有1条，slot635007334/sequence271/time1793325600004485766，size.00001959，rate.0000125，mark76699.755，payment -.000018781852505625004；account与closed position funding为-.000018782。保留原支付与实际累计不同精度，不猜统一舍入或伪造相等；完整coverage仍unknown，不能当全历史认证。净额符号已真实验证为负余额变化/正成本。**下一步不再重复同样整点小额测试**，从此真实支付/净额数据接正式入账、防重与恢复资格，继而普通网格补单/带仓保护/残仓/24h。

原自动AUDIT grid-bulk-funding-native-v33.json保留失败：AdapterError/scopedfalse。实际只读诊断已定位BULK_POSITION_LIFECYCLE_MISSING_OR_AMBIGUOUS：买fee .000525+卖fee .000526精确=.001051，native closed fees为-.0010509999999999999；其余open/close ns、qty、peak、volume、VWAP/slot均完全一致。不是资金单缺失，不新开仓或改原RID。原生平仓Pnl .000758，exactcashflow .0007585248均保留。Native账户flat/noorders/pending/locks，margin500.694526/fees-.00315/funding-.000018782/realized-.003305。

**v35候选修复** bulk_closed_positions：账本仍用Fraction精确原逐笔费用；只接受可由这些费用按原生顺序逐步nearest-even binary64重现同bits的native汇总，并要求差额小于最细输入fee单位半格；全程整数/Fraction计算，非宽松epsilon，不放宽数量/价格/时间/资金费/身份。原生汇总金额保留，proof另列fee_reconciliation和精确fee_cost；无差额时原proof_id不变。新增真实夹具funding-boundary-mainnet-20261030.json及12测试，硬件oracle仅在测试里显式逐步相加（Python3.12+sum改为补偿求和，不可作顺序fold oracle）。本机相关92pass。独立只读audit_bulk_funding_v35.py已准备，仍RID原v33；新OUT grid-bulk-funding-native-v35.json，不覆盖旧失败；显式保存reported payment cost与applied net差，历史cert仍false。

v35冻结 /root/code/entropy-grid/data/staging/bulk-fee-aggregate-v35/source，493manifest，父为v34已通过r2；归档SHA567ce7884a3dcc5bacb55e9908882a18c976a2e9cb2c7dcb9e88b2c49449f231。Linux原PID2727028，stage/pid，报告grid-bulk-fee-aggregate-linux-v35.json；最近live65%，未结束，不重启。准备器已在启动前校验全部实际测试文件存在且在manifest里。**Linux通过后**用Bulk实际.venv、PYTHONPATH明确v35冻结source，直接执行其planning/grid/tools/audit_bulk_funding_v35.py做原交易只读重新终审；不启动金融服务、不创建新policy，不修改任何v33冻结源。结果未知先查原OUT/进程，不重复审计写同一备份。

配套备份已完成且独立新nativeflat：grid-bulk-funding-backup-v33.json checked1793326360959，opaque credentials仅原样复制不解密，3配套文件SHA/integrityok/seq0，25原intent/cancel完整复核、5服务身份未变。路径 /root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261030T021240Z-25dccb6b，DBSHA73ada9b77ec6830a7f9c0b56b00844d639242a0c8d414db7f7040858bc0bd5b1。本机helper data/run/grid-bulk-funding-backup-v33.py已执行完成，不重跑。可只读取报告，禁止下载master key/credentials内容。正式Bulk仍v23/GRID0，完整目标active。

# 原v33已真实持仓观察，不能重发UNKNOWN买单

2026-10-30 UTC01:58:17实际SQL核查：原controller2715125仍活动，cli_started=true，固定policy approved_ms1793325461452/expires_ms1793326061452（清理至1793326361452）。原probe phaseOBSERVE/finishedfalse，qty0.00001959 BTC，fee0.000525。原生stop ACK_ACCEPTED/bulk_resting，OID58HrLfd9ZB8icSMRrfejMyXL2WDE3JoNconHv3sNErUM；原entry CID bulk-funding-20261030-v33:entry/OID3Z5YyS2DUV1kpPaqzCdtodraS42WvPxcxQZyyNNVCsnw 保留 MAY_HAVE_SENT/receipt UNKNOWN/bulk_ack_unproven，但实际原查询和真实fill已匹配持仓，**严禁另买或重发**。资金费baseline0/fees-.002099；首原观察样本 at1793325492821，qty一致/stop_presenttrue，account_funding与position_funding均0。目标UTC02:00尚未发生，不能称跨整点成功/非零支付/完成。继续只查同一原进程、账本和原审计结果；原始范围/期限不延长，v34修复不覆盖该v33源。

# 当前：原v33有限主网任务已实际启动；v34 Linux973通过

2026-10-30 UTC01:57:19实际核查：原service entropy-grid-bulk-funding-20261030-v33.service active/running，MainPID2715125；原Bulk observer inactive/MainPID0，由该有界控制器正常暂停。原run.json started_ms1793325421558，cli_started=false，尚在查询旧原单/备份，policy尚未固定且本轮原intent为0。**这只证明进程启动，不能声称买入/持仓/跨整点成功。** 先查同一service、原run、SQLite bulk-roundtrip:bulk-funding-20261030-v33 和原AUDIT，不能重复启动、换时间或重投原请求。正常目标UTC02:00；服务max1050秒，保护/范围不变。

v34 r2已实际终结：973pass/0fail/error/skip，test_exit0/source_unchanged，finished1793325357545，XMLSHA7d72c8f93f2d952c9dbab367df68f2db0c613975c6f5d5c196e6d0c885f37125；原PID2711710不再需等待。报告grid-bulk-probe-retirement-linux-v34-r2.json与原exit4/0tests报告均已取回，保留选择错误历史。v34只测试/冻结候选，不安装、不替换当前金融v33源或正式v23生产。后续新probe可用refresh_closed_originals执行封存校验与其他旧原单Native查证；已经结束的CID不可复活。完整目标仍active。

# 最新：v33 完整2867通过；原整点任务等待中；v34修复未发送原单封存

本轮为实质进展。已确认 v33 原全量 PID2702854终结：2867 tests/0fail/error/skip，test_exit0/source_unchanged，finished1793325059729，XMLSHA37fcf75ca255cca7bd81b7761fdbe5d99a767fc4b9b7f12661cae8db542c7ede，完整报告 grid-bulk-funding-full-linux-v33.json 已取回本机。不能把全量测试当实盘已完成。

原金融任务仍是下面唯一 RID bulk-funding-20261030-v33，timer最近实查 active/waiting，UTC01:57/北京时间09:57触发，目标UTC02:00；service尚未启动、run.json尚未生成，不重跑或换原身份。所有源/控制器仍使用 v33 原冻结文件，绝不复制本机新候选去覆盖。HL/套利/正式Bulk生产未动。

等待整点期间，本机新增 v34 bulk_probe_retirement：已结束且Nativeflat的有限probe把ALLOCATED/PREPARED且无receipt/native evidence/terminal resolution/成交/撤单的原请求，在独立runtime事务封存为ABORTED_NOT_SENT，并保存原记录和原campaign哈希证明；既有reserve/claim_send拒绝复活CID。require_idle只接受完整验证过的封存证明，不能凭PREPARED或原生查无记录放行；未知MAY_HAVE_SENT保持未知。普通core表非空拒绝此诊断封存API。支持单笔、双挂单和原已结束场景，原nonces/OIDs/消息和action不改。新增 refresh_closed_originals 先本地验封存，其余原单仍正面Native核对；调用者仍需独立pristine账户和排他runtime。尚未安装正式生产或用于原v33金融任务。

本机retirement+roundtrip+funding观察+resting/postOnly共92pass，其中10项新反例/重开：ALLOCATED/PREPARED两种真实组件未发收尾、重开Store/new startup、旧CID reserve/send拒绝、记录/nonce/hash/binding/campaign/证明破坏拒绝、unknown绝不封存。移除本次改动后3个原运行模块逐字SHA与v33父源完全相同，无混入HL变化。冻结 /root/code/entropy-grid/data/staging/bulk-probe-retirement-v34/source，490manifest，归档SHAafbc1a291c94c0f75a016e63828d6da8929452d6e7f0b622c180782ed4285195。

v34 首次测试选择误写不存在的 tests/test_grid_recovery.py，原PID2711168已明确terminal，报告 grid-bulk-probe-retirement-linux-v34.json 为exit4/0tests，源码未改/0金融发送。**保留原失败**，核实退出后在同一冻结源按v31已存在恢复测试清单+所有Bulk实际文件启动独立r2：原PID2711710，stage/r2-pid，47文件；报告 grid-bulk-probe-retirement-linux-v34-r2.json。最近PID仍live，stdout59%，尚不可称Linux通过，不因观察期结束重启。这里只是选择修正，不改运行代码。完整Bulk目标仍active：资金费接入、普通补单闭环/保护触发与带仓恢复/残仓/24h及终局仍须完成。

# 最新：v31 净额证据重开通过；v32/v33 已通过 Linux，一次性跨整点主网任务已排定

本目标轮有实质进展：v31 原真实副本净额审计已成功结束，v32 原测试进程已终结，完成 v33 控制器/独立审计开发与 Linux 测试，并首次排定新的有限主网验证。未将 Bulk 完整上线目标标为完成；正式 Bulk 仍 v23/GRID_REAL_DISPATCH=0，HL 和套利未改动。

**立即承接的原任务**：RID `bulk-funding-20261030-v33`；原 service `entropy-grid-bulk-funding-20261030-v33.service`，原 timer 同名 `.timer`。已实际查询 timer active/waiting，触发北京时间 2026-10-30 09:57:00（UTC01:57），目标资金费时点 UTC02:00，即 funding_at_ms=1793325600000。这是一次性日期任务，不是 AI 高频跟进。service 当前尚未启动，不能说已经成交/持仓/跨整点。**先查原 unit、原请求/账本和原报告，不重复创建或启动。** 原 launch `/root/code/entropy-grid/data/run/grid-bulk-funding-observe-launch-v33.json`；私有 grant/激活 policy/控制器结果位于 Bulk `data/run/bulk-funding-20261030-v33.{grant,approved,run}.json`。冻结 `/root/code/entropy-grid/data/staging/bulk-funding-observe-v33/source`，488项 manifest；controller 独立拷贝 `data/run/run_bulk_funding_observe_v33.py`，SHA a00c0cf3091e8d69b788c89ac92702d63418dc8de178f0a57162881e96a9179f。启动前暂态账户HTTP503曾使第一次准备在写任何授权/任务前退出；已实际确认5个原文件均不存在、service/timer not-found、账户恢复200后，才首次创建成功。

本轮继续既有独立Bulk真实操作小额授权：原生止损先确认，一次BTC IOC买入<=2USD（约1.5），手续费<=.01USDC，最多5个RO退出，10min加5min仅退出/撤单；不转账、不补仓、不改杠杆。首次实际开仓前仍全账户flat/noorders，原Bulk观察器仅此有界测试暂停/恢复，服务RuntimeMax1050s/TimeoutStop60s，ExecStopPost只恢复Bulk observer。私有模板approved_ms/expires_ms保持0，在旧原单重新核验与备份后才固定真实期限；目标整点永不顺延，若不足60秒或提前超过6分钟则拒绝。基线funding/fees只持久化一次；带仓原止损/数量/新鲜快照缺失立即停止hold并转已有RO退出。未知查原身份，不重发。

**独立最终证据**：控制器结束后自动调用经过冻结测试的 unsigned `planning/grid/tools/audit_bulk_funding_v33.py`；报告 `/root/code/entropy-grid/data/run/grid-bulk-funding-native-v33.json`。按本轮原普通单累计量对齐成交，再匹配一条已平仓生命周期，分别比对账户资金费差额、带仓position.funding、closed position funding净额和费用；保留真实fundingHistory及coverage标志，不把净0当非零支付，不伪造payment或普通资格。逐项查原stop终态、全账户flat/noorders/pending/locks、只读备份integrity/SHA和原journal一致；审计只允许unsigned account/exchangeInfo，无vault/签名/金融发送/服务控制。若原controller因强制期限未产审计，后续应先查原账本和原AUDIT状态，再只读收尾，不能复活原交易。

**已完成证据**：v31 `grid-bulk-closed-net-native-v31.json` checked1793323183245，8原普通单/4真实成交匹配2持仓闭合生命周期；fee .002099、funding净0、native realizedPnl -.004063、成交cashflow -.0040642275。实际生产Python在原synthetic accounting副本重开Runtime，2条独立净额证据/1事件/0payment，幂等通过；正式funding status、原21条intent/cancel、5服务身份不变。初次helper仅最后sqlite.Row与tuple比较失败，已独立证实内容一致后修正比较并在原DB恢复审计；未重建或金融重试。副本SHA b4fabaa8c19faef53aac863cb650e5a8f758f574d6e4afc91e6ba5a4f0f8ae0c，integrityok；report明确 audit_resumed_after_row_type_comparison_fix=true。v31 Linux925通过，不能把净0说成非零资金费。

v32 Linux187通过，原PID2693100已终结；v33相关208通过（0fail/error/skip，source_unchanged），finished1793324370877，XMLSHA b8e037e1f27f57a1ff9bfe828f174208f7921f9e217a423f6db59c5814469194。v33只在v32冻结父源上增加2工具和1测试，归档SHA b0f2e79aeca731b232e9627c2d82081af8b46797fc4e70e7b6849762fb0492d6；未混入本机HL recovery其他变化。完整网格测试另启动于同一v33冻结源，原pid文件stage/full-pid，报告 `grid-bulk-funding-full-linux-v33.json`；实际PID见工具输出，先查原进程/报告，不因观察期结束重启。报告下载到本机data/run。

下一步：等待并核查上述原任务的实际成交/持仓跨整点/净额与支付证据，随后把资金费接入完整网格的独立区间净额或原生payment来源，明确重叠防重与覆盖语义，继续普通网格真实买卖补单、保护触发/重挂、带仓故障/小残仓、至少24h与独立终局。当前尚无普通Bulk真实网格，不能用有限协议probe或副本回放替代完整目标。不等官方，不恢复AI周期检查。

# 进行中：v31 持仓区间净额读取/匹配/证据保存已 Linux925通过，真实副本验证启动

上一目标轮v30为实质进展；本轮从资金费真实正面数据继续，并非等待官方/空轮询。新增bulk_closed_positions：独立positions历史closeSlot/sequence分页、scope/iso/时间/精度/正数/总量边界、页锚点和游标校验，unknown完整性保留；通用BulkEvidenceReader.history仍不允许positions，只有unsigned transport只读类型增加positions。按独立原普通单累计证明，重建全闭合做多生命周期，以open/close微秒时点、closeSlot、maxQuantity、totalVolume、买卖VWAP和费用唯一匹配Native已平仓行；开放/反向/缺失/歧义拒绝。Native realizedPnl保留原值，成交cashflow分列不猜舍入公式。经济指纹按精确规范化值计算，等价小数格式不制造冲突。

BulkGridVenue.reconcile_closed_position_net与FundingSynchronizer.observe_position_net已实际接线：Core账号/市场/原CID/OID/requesthash匹配，最多16普通原单、90s只读总限；净额证据写独立runtime_state funding-position-net:<scope>:<position_key>并追加幂等事件，冲突拒绝。**不写funding_payments、不推进其cursor、不清除原funding错误、不放开普通实盘资格**，避免后补原生payment重复计费。这里是可核对的持仓区间净额证据，不是假装逐小时支付明细；真实非零资金费符号及带仓延续仍需实测。

本机相关166pass（HTTP测试沙箱内5项失败为loopback bind权限，允许回环且外网守卫运行后全通过）；最后只读总时限补充后相关46pass。所有改动normalize移除本次方法后对比v30精确原funding.py/Bulk.py/bulk_read.py SHA均一致，无混入HL其他代码。冻结/root/code/entropy-grid/data/staging/bulk-closed-net-v31/source，482manifest，6文件覆盖/新增，最终归档SHAcd2e8dcbe4f92ad537830ccfd7c05fda7ce0b58bfcb00ba14474bc8de3c2013e。Linux全部Bulk+funding+engine/ledger/recovery **925pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793322550795，XMLSHA59a6c69a9552ba006242e565ebc1e0132db219f8b0ae1b1b97b331e889c7436b，报告grid-bulk-closed-net-linux-v31.json；原测试PID2673460已结束，不重启。

**原真实副本验证已启动**：data/run/grid-bulk-closed-net-native-v31.py通过Bulk实际.venv在服务器运行，exec session见当前工具输出；报告/root/code/entropy-grid/data/run/grid-bulk-closed-net-native-v31.json，进度stage/native-progress.json，副本stage/closed-net-ledger-v31.sqlite3从已验证v30重建副本再在线复制，GID仍g-bulk-accounting-replay-v30（明确synthetic accounting，非新实盘grid）。helper两次实际Native重新核验，中间close/reopen Runtime，预期2净额证据/1事件/0payment，原formal funding status不变，fee0.002099、funding净0、native realizedPnl-.004063；所有HTTP只允许POST/account和GET/exchangeInfo，0用户密钥访问/0签名/发单/服务控制。先查原session/进度/结果，不因读观察超时重复创建副本。生产仍v23/GRID0，HL/套利不动。

下一步依真实结果完成净额证据重开验证，继续当前持仓funding字段/跨UTC整点小额受保护观察，验证非零支付与position关闭历史的延续，再接正式资金费来源/重叠去重与恢复资格。完整目标仍含真实普通网格买卖补单、保护触发/归零重挂、带仓故障/小残仓/24h；不能将旧成交副本或仅净0视为这些已完成，不能等官方停止执行。

# 最新已完成：v30有序恢复879通过与真实副本重启通过；取得原生已平仓资金费净额

上轮为实质进展，本轮也已改变代码并取得真实新证据；完整Bulk上线目标仍active，普通生产未开放，不是新的实盘grid/24h认证。

**当前可用候选**：/root/code/entropy-grid/data/staging/bulk-owned-recovery-v30/source，479项manifest，相关全部Bulk+engine/ledger/recovery **879pass/0fail/error/skip**，test_exit0/source_unchanged，finished1793321034793，XMLSHA31bf62d831c8ed60b9f8e3b1345380a6b9d9dd329e23a9c3a0e011ba7c069dda；grid-bulk-owned-recovery-linux-v30.json已取回。父v29原全量进程2645817亦已终结 **2794pass/0fail/error/skip**，finished1793320850794，XMLSHAfffc8459a1e05aaa7112e3a9c290b4a904aed8a081510d92ef20eb40b0d5e6ce；grid-bulk-owned-recovery-full-linux-v29.json已取回。这是v29全量+v30定向879，不能声称v30单次2794全量。所有原测试进程均终结，不重启。

v30实际生产运行库副本恢复 **exec79467 exit0**，grid-bulk-owned-recovery-native-v30.json，checked1793321259205：只读原DB在线副本stage/replay-ledger-v30.sqlite3，synthetic accounting GID g-bulk-accounting-replay-v30；两批先完整收齐8个普通原单终态与request_hash，回放原4笔真成交/2配对/fee0.002099/net0，三次recover backfilled[0,4,0]，第三次为关闭重开实际Runtime后的幂等验证。clone SHAade8cef6de0a0fa30430585ee18fd3a96b2ac3048f4534afdb1b6051466e5ada/integrityok；原journal与5服务身份未变，native仍flat/500.694838；0金融发送/0用户密钥访问/0服务控制，普通恢复仍因全历史/资金费未知阻断，coverage无推进。取回此报告的scp exec48861可能已完成，先查原会话/本机文件，不重跑原replay helper。

代码要点：每批<=4原单；只持久缓存正面FOUND_TERMINAL且已知累计量的普通订单证据（不缓存OPEN/不含stop），与当前核心OID/request_hash再次绑定；所有该grid普通单证明齐备前0库存/费用写入，齐备后按native slot/sequence全序回放。分批缓存支持重开Runtime/后续新原单；已入更晚成交时拒绝补入更老成交破坏FIFO，需有序账本重建。actor事务内RECOVERING门，普通路径默认不变，恢复时无新订单。不是全局历史cursor或资金费证明。**必须从v30完整冻结源继续，不直接复制本机整个recovery.py（其中有HL其他工作）**；本机data/run/grid-bulk-recovery-v30-isolated.py SHA6627a986317113ec00c5b34a76908bce4b4e847ada5f5d4038075d1ee2ace096由精确v25基线仅叠本次方法构造。Bulk.py SHA d6f7edc1eabf47b5698d3a2b4bc7624645976d1ddc311a28027d89c64a2280b2；v30归档SHA516c871fed59e3b73039a7afaee17c2ee952d96595c0a78cb4b6988636b9ed63。

**新资金费推进线索（不要回到等官方/反复空历史轮询）**：官方positions历史查询已实际HTTP200取得2个已平仓生命周期，grid-bulk-position-history-native-v31.json已取回，checked1793321143774。初尝试因根HL目录缺observation模块在联网前失败；随后用Bulk实际Python+明确v30source首次查询成功，0金融操作。当前BulkReadTransport.HISTORY_TYPES尚不允许positions且通用history解析期待slot而此类使用closeSlot，所以下一步应做独立原生closed-position reader/schema，不直接套现有fill解析。

两个正面样本：BTC v19 closeSlot630213107/sequence0，quantity=maxQuantity0.00001843、totalVolume0.00003686、avgOpen81439.08/avgClose81422.43、realizedPnl-0.000306、fees-0.00105、funding0、openNS1793287227163715423/closeNS1793287246188421467。v1 closeSlot597037868/sequence0，quantity=maxQuantity0.0000188、totalVolume0.0000376、avgOpen79794.79/avgClose79594.93、realizedPnl-0.003757、fees-0.001049、funding0、openNS1793021330579735767/closeNS1793021844275648022。均owner原账户/isofalse/closeReasonnormal；页面coverage仍unknown/minAvailableSlot缺失。两记录fees和-0.002099与原4成交/原生账户累计一致，funding净和0与账户累计0一致，native实现Pnl和-0.004063吻合账户累计。按qty*(avgClose-avgOpen)的精确值分别-.0003068595/-.003757368，各向零截断6位恰好等于原生PnL，解释已知差额；**只是两个单次买卖样本，未证明所有多次加减仓规则或全量舍入公式**。

接下来直接实现：独立closed-position解析、按原8单/4成交/时间区间/数量/volume匹配的生命周期净额证明；资金费可明确记为“原生持仓区间净额”并与账户累计核对，不能伪造逐笔payment或直接翻FundingPage.complete。再准备持仓跨整点的受保护有限小额观察，验证当前position.funding与关闭历史funding是否延续、净额符号/精度/真实支付。官方资金费每UTC整点结算已查，详见BULK-FUNDING-VALIDATION.zh-CN.md，现尚未启动该新金融请求。真实普通网格补单/保护触发/带仓故障/小残仓/24h仍须完成；生产仍v23/GRID0，HL/套利不动，既有小额主网授权有效，不复用HL授权、不重复旧RID、不恢复高频AI定时观察。

# 当前最新：v29真实副本恢复已通过；v30补齐跨批次全序回放，Linux验证已启动

上一目标轮和本轮均实质进展，完整Bulk上线目标active，未生产安装/未新增金融发送。下面v26/v27/v28失败与v29完整源隔离过程要保留，但不可把旧“仍运行/未执行”当当前。

v29 精确Bulk基线候选相关195pass，真实生产运行库副本验证已实际结束exec22046 exit0，报告/root/code/entropy-grid/data/run/grid-bulk-owned-recovery-native-v29.json，checked1793320467358。原DB在线副本/root/code/entropy-grid/data/staging/bulk-owned-recovery-v29/replay-ledger-v29.sqlite3建立了明确synthetic accounting reconstruction，不是真实新grid。实际Runtime两轮恢复补入原4笔真成交/fee0.002099/2配对/net0；重开Runtime第三轮0重复，原journal与5服务身份不变，native flat/500.694838。coverage未推进，历史与资金费未知仍阻断恢复，普通资格false。原全量PID2645817最近仍live82%无错误，终局报告grid-bulk-owned-recovery-full-linux-v29.json，先查同一原进程，勿重启。

发布前发现v29尚有跨批次顺序风险：若卖单先处于第一批而旧买单下一批，逐批直接入账会改变FIFO。**v29不发布**。已在本机实现v30：每批最多4原单，只有正面终态且累计已知的证明可持久缓存；缓存与当前核心OID/request_hash逐项匹配，所有普通订单证明齐备前0库存/费用写入。齐备后对所有成交按原生slot/sequence统一排序，经actor同一入账路径幂等回放。若漏掉更老成交且已有更新成交入账，拒绝乱序修补，需有序账本重建。缓存不是全历史cursor、不认证资金费或stop子单，普通资格仍阻断。事务RECOVERING门、无新订单/只保留demands不变。

本机核心74pass，新增5单两批+重开测试要求第一批0入账、第二批5笔整体按1..5回放，重复0；新增旧缺失成交拒绝乱序覆盖已有FIFO。v30沿用v29精确Bulk基线（不能直接复制本机整个recovery.py，其中还有HL其他工作），只覆盖3项：isolated recovery SHA6627a986317113ec00c5b34a76908bce4b4e847ada5f5d4038075d1ee2ace096、BulkGridVenue排名方法、测试。archiveSHA516c871fed59e3b73039a7afaee17c2ee952d96595c0a78cb4b6988636b9ed63，479manifest，服务器/root/code/entropy-grid/data/staging/bulk-owned-recovery-v30/source。data/run/grid-bulk-owned-recovery-linux-v30-prepare.py已执行SSH启动，本机exec session见当前工具输出；需查原stage/pid或grid-bulk-owned-recovery-linux-v30.json，不重启。选择所有test_grid_bulk*.py与engine/ledger/共用recovery，尚不可称通过。

准备的data/run/grid-bulk-owned-recovery-native-v30.py未运行，须v30 Linux通过后同样只读公共API+独立副本，验证真实4成交/fee/2pair及stage缓存重启去重；原v29副本不能复用/覆写为v30，也不能声称带仓故障通过。资金费跨整点安排见BULK-FUNDING-VALIDATION.zh-CN.md，尚待实现/启动。生产仍v23/GRID0，HL/套利不动；不等官方、不复用旧金融RID、不恢复AI周期检查。

# 当前执行：精确Bulk基线 v29 相关195通过，全量与真实成交副本恢复已启动

本轮实际进展：恢复流程已能在保持全历史阻断时，将经原订单累计量独立证明的普通成交按原生顺序幂等入账；支持4单批次/持久检查位置轮转、核心OrderRequest hash匹配、actor事务内RECOVERING门、无补单与无coverage推进。本机190相关通过，后续awaitable调用修正后新2文件32通过。详细实现见下方v26段（其“仍live”信息已过期）。

**必须使用精确v29候选，不直接复制本机整个recovery.py。** 本机recovery.py含HL后续三次只读重试等其他工作；最初v26隔离源混入该文件，2794全量2792pass/2fail，失败为旧单次重试测试与三次实现不一致；v27因缺测试文件0case/exit4，v28补入HL整体resilience测试后214中7fail，暴露其余HL配套源未同步。以上均已终结、源未变、0金融发送，不重跑/不称通过。没有覆盖/回退本机HL改动或修改线上HL服务。

正确v29从已通过的v25完整Bulk源精确构造：原recovery.py SHA先核对，再仅插入本次fallback与_backfill_owned_positives方法，调用recovery_read采用一次awaitable（兼容原基线）；actor去掉本次参数/事务门后SHA精确等于v25。isolated源本机data/run/grid-bulk-recovery-v29-isolated.py，SHA96a3396976630a6c674f3f69d3a7348d8dac02d1b12a098895a1b6345eaa882f。冻结/root/code/entropy-grid/data/staging/bulk-owned-recovery-v29/source，479文件manifest（继承v25/5覆盖），archiveSHAf4a024a6c1f458bb119680a9b4e5e48d0b21c01581262d250ba9d8b9ef687e87。只有运行本次新增功能，无其他HL版本升级。

Linux相关12文件195pass/0fail/error/skip，test_exit0/source_unchanged，finished1793297724751，XMLSHA8dd8bd07f889bfa727b4d2304db4915fc6f6b2d317052a4218ae7b4724d162c4，grid-bulk-owned-recovery-linux-v29.json。全量基于同一冻结源由一次性full-runner启动，**PID2645817**，元数据stage/full-pid，输出stage/full-stdout.txt与full-stderr.txt，终局/root/code/entropy-grid/data/run/grid-bulk-owned-recovery-full-linux-v29.json。先查原PID/报告，不因观测时间到重启；普通生产发布仍需全量结果。

真实只读副本验证helper data/run/grid-bulk-owned-recovery-native-v29.py已通过SSH启动，**本机exec session22046**，实际Bulk生产Python，仅公共API和原DB在线副本，不读vault、不签名/下单/启动worker。副本stage/replay-ledger-v29.sqlite3，synthetic accounting GID g-bulk-accounting-replay-v29，原8单requesthash不变、预期4笔旧真成交/2配对/fee0.002099，经2次Runtime恢复+一次重开应不重复且全局覆盖仍阻断。终局data/run/grid-bulk-owned-recovery-native-v29.json；任何失败先检查该原副本/报告，不重建原身份。不是实盘新网格或持仓故障证明。此时目标active，生产仍v23/GRID0，HL/套利未动。下一步原句资金费按整点小额验证的安排仍待实现，不能等官方或把副本恢复当完整上线。

# 进行中：v26 已接入原订单正面成交恢复，Linux 全量回归运行

上一目标轮属实质进展（v25代码、Linux296及真实8原单/4成交证明）；本轮从真实恢复接线继续。已实现RecoveryBarrier在全历史不完整且非分页扫描进行中时调用适配器reconcile_owned_fills：只选该grid已绑定的普通原订单，按4单有限批次检查，原request hash与核心OrderRequest逐项重建一致；验证整个证明和所有记录身份后才经实际actor幂等入账。检查队列位置独立持久轮转，绝不推进coverage历史cursor。actor新增recovery_only可选参数，在成交事务内要求RECOVERING，默认路径不变；因此并发控制变化拒绝，不生成补单，只保留refill demand。Bulk正面成交证明按native slot/sequence正序回放，修复原最新优先返回用于库存时的顺序问题。普通历史/Funding完整性与恢复资格门仍保留，不能据此称恢复已允许。

本机相关190通过（包含7项真实Runtime集成：原未知成交/费用入账、重复、缺失/变更/外部/控制竞争拒绝、5单分批与重开Runtime、恢复完整覆盖后正常通过；其他actor/recovery/HL兼容）。Linux冻结/root/code/entropy-grid/data/staging/bulk-owned-recovery-v26/source继承v25，仅覆盖actor/recovery/owned_fills和2测试共5文件，479manifest；archiveSHA09d818a8268a4583f9c6026224c5cbec0d794389a72a5ed680a7282862873551。**全量test_grid*.py原进程PID2338995已确认仍live，最近stdout82%无错误，尚无终局**。只查同一PID/报告data/run/grid-bulk-owned-recovery-linux-v26.json，勿因观察结束重启。此次不安装生产、不发单。

准备的实际生产运行库只读验证helper data/run/grid-bulk-owned-recovery-native-v26.py尚未执行：要求完整Linux通过后，用原DB在线副本/root/code/entropy-grid/data/staging/bulk-owned-recovery-v26/replay-ledger-v26.sqlite3重建一个明确标记的synthetic accounting grid，原8单nonce/action/requesthash原封不动映射core；2次实际Runtime恢复应逐单入4笔历史真成交与费用，第三次重开Runtime验证不重复。期间只读native无signer/order_transport，无凭证解密，不启动普通worker或新金融身份。结果应在data/run/grid-bulk-owned-recovery-native-v26.json；若clone已存在先查原状态，不能当真实新网格或持仓故障证明。

资金费已核对官方https://docs.bulk.trade/bulk-exchange/funding，按每小时整点结算；下一步具体有期限验证方案在BULK-FUNDING-VALIDATION.zh-CN.md（单次<=2USD原生保护先在、10min+5min仅退出，跨整点，<=0.01手续费，未知查原单）。尚未启动/未创建定时任务，不把净0或空历史当支付证据。完整Bulk目标active，当前生产仍v23/GRID0，HL/套利不动。

# 最新进展：Bulk v24 独立终审与配套备份完成；v25 原订单成交核验已通过 Linux 296 项

上一目标轮主要核对HL并确认Bulk v24原请求已结束，本轮实质推进：v24三个原订单已独立native查询全部CANCELED，普通两单累计量已知0，stop累计未知保持未知（撤销按用户启动政策接受）。Native零仓零单、无pending/locked、余额500.694838不变、fees0；4核心表仍0，原intent/cancel内容未变。entropy-grid-bulk-postonly-20261030-v24.service inactive/MainPID0/exit0；Bulk观察2322230/web2298148 active；HL161833/watchdog161838/套利198980身份未变。独立审计data/run/grid-bulk-post-only-native-v24.json已取回，checked1793295719788。私有配套备份/root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261029T174159Z-ba11719d，integrityok/seq0，数据库SHA3ad1d6abcd1c1a54276544ba9ba580795fd3f904a81c292861abd3ffa4982fb6，3个配套文件SHA与原intent/cancel重开核对通过。不得重跑v24原请求，不能把挂撤视作成交循环。

已新增bulk_owned_fills及BulkGridVenue.reconcile_owned_fills：只对显式原普通订单，按原CID/OID/nonce/request_hash/action和账户/市场/网络绑定，前后独立原生累计量必须已知且不变化，逐单精确比对历史成交量、方向、限价、费用符号；缺失/超额/变化拒绝，不以净仓抵消。Fee与cashflow用Fraction，不受外部Decimal精度影响；不返回HistoryPage、不推进普通历史cursor、不认证外部订单/资金费/止损子单。普通fills/funding完整性与生产资格门未弱化。

本机相关106pass；Linux冻结/root/code/entropy-grid/data/staging/bulk-owned-fills-v25/source继承完整v24后只覆盖bulk.py、新module与test（3文件），478项manifest核对，独立进程2329515已终结，296pass/0fail/error/skip，XMLSHA101c55ea2af6dc95c6d4bef476c93d6f2ba09e94b9993d4565c9fba7faabb131，finished1793296099114，报告grid-bulk-owned-fills-linux-v25.json。未安装生产/未金融发送。

v25实际主网只读核验已完成，原session63070 exit0，不再运行：grid-bulk-owned-fills-native-v25.json checked1793296191135；使用实际Bulk生产Python和冻结stage真实BulkGridVenue方法，所有8个原普通单（v1/v19各买卖两单、v20/v24各两挂撤）累计已知且前后不变，逐单总量匹配4笔真实fill，signedqty0、fee_cost0.002099与native margin.fees -0.002099一致。Native仍flat/500.694838，原journal及5服务身份未变，0金融发送/0用户密钥访问/未生产安装。proof_id b489d8e47db4d35e4259859d4a6470a0ba4e10ef833467cefffac41d702a03ae。历史原metadata仍unknown/completefalse，end/asOf630213107；新证明仅这8个普通单的成交量，不改account_history_certified/funding认证/正式资格。精确成交cashflow -0.0040642275，而native realizedPnl -0.004063，差-0.0000012275，记录差异但未认证其舍入公式；funding净累计0不能冒充完整payment历史。下一步把这类独立正面成交证据用于真实恢复账本，并补资金费/保护/完整网格有限验收；完整目标active。

# 最新核查：HL 已通过受保护24h终审；Bulk v24 原有限测试已闭合

2026-10-30 用户再次问 HL 考核状态。只读核对服务器原终审 grid-hl-v21-terminal-review-v1.json：g-e3debcab696d41ba protected_24h_observed=true，86430.321秒、8次恢复120.933–181.648秒；严格不间断仍false，release_qualified_by_review=false。当前SQLite全部85个grid为CLOSED、无queued/running operation，intent仅FILLED/CANCELED/REJECTED/EXPIRED_EPOCH。HL161833/watchdog161838/套利198980 active且未重启。这轮初始买入与最终退出2笔、无网格配对，净收益-0.020198；正式全功能上线资格不能等同于这一轮24h通过。 额外新鲜Native检查checked_ms1793295599180，11个DEX全部零仓零单、账本活动/未决/未知均0；报告服务器data/run/grid-hl-status-1793295599180.json。该次仅原生主永续marginSummary.accountValue为0，不是统一账户总资产证明，不复述旧195.818889为实时总余额。

顺带核对上一轮发送结果未知的 **原** Bulk unit entropy-grid-bulk-postonly-20261030-v24.service，已inactive/MainPID0；原launch/grant/approved/run均存在，禁止重启或重发。原run finished=true/cli_exit_code0，phase CLOSED/native_flat_no_orders=true/two_entries_observed=true，fees0/real_roundtrip=false；原生两笔BTC挂单tif实际为postOnly且maker=true，止损先在场，三个样本qty0，最终3次flat检查后撤stop。实际policy1793295411859–1793296011859，cleanup1793296311859，closed1793295512219，没有延长期限；Bulk观察已恢复PID2322230/web2298148 active。原run在/root/code/entropy-grid-workers/bulk/data/run/bulk-postonly-20261030-v24.run.json。此处仅核对原报告，**独立v24终局原订单/账户审计与备份尚待完成**，不能把两笔只挂撤单视作真实成交补单、持仓故障或24h通过。

v24源冻结/root/code/entropy-grid/data/staging/bulk-post-only-v24/source，476项manifest，Linux205通过；实际Bulk生产venv已按原lock安装bulk-keychain0.1.30，wheelSHA241ce45a32487f862537bd91e7736d6faf50c7c4e80d2c5e7c1f55e665e4b0d2，12例临时密钥离线签名通过，无服务重启。修复仅将明确等价alo/postOnly映射到ALO，拒绝slide/join，校验普通单绑定参数；新PostOnlyRestingAuthority保持旧GTC授权摘要与有限范围。普通生产仍v23/GRID0，完整Bulk目标未完成。下一轮先独立审计v24原身份及备份，再继续真实普通网格/历史有界对账；不要重跑launcher或把v24控制器指向活动可变源码。

# 最新：Bulk v23 已部署真实账户/网站资金预览，接下来推进正常执行和有界对账

上一目标轮为实质进展：Bulk v22 已有资金模型，本轮 v23 已把保存、检查、restore、RPC catalog/market/preview 和创建页接通，并实际部署。不是新的实盘24h轮，也没有金融发送。完整Bulk上线目标继续active，不缩成只读产品，不等待官方答复。

## 当前生产身份与部署位置（后续必须据此查实际源）

- Bulk工作目录仍 /root/code/entropy-grid-workers/bulk；DB data/grid-runtime.sqlite3/vault data/grid-credentials/socket data/run/grid-runtime.sock 均原路径。
- **当前代码源在 /root/code/entropy-grid-workers/bulk/releases/bulk-account-preview-v23**，188个运行文件由release-manifest.json约束，两个ExecStart均绝对指向该发布目录的grid_main.py/grid_web.py。原工作目录内entropy_arb旧副本未覆盖，不能编辑旧副本后以为已部署。金融验证继续显式引用独立冻结源/manifest，不从cwd推断。
- /etc/systemd/system/entropy-grid-worker@bulk.service.d/90-preview-release.conf 与 entropy-grid-bulk-web.service.d/90-preview-release.conf 只覆盖两个Bulk实例。GRID_REAL_DISPATCH=0；worker2298147/web2298148 active，均已查/proc argv/cwd/env。原模板未改；一次性发布unit entropy-grid-bulk-preview-release-v23.service现inactive/MainPID0/exit0，不重启此发布身份。
- HL161833/watchdog161838/旧套利198980原PID与启动时间未变，HL原66文件当前hash前后未变。Bulk原诊断intent/cancel内容SHA b222e7f782465d89685cb9ca27ad3953ddd16e6ba9bc02971106059257122fd8 未变，原grids/order_intents/operations/budget_reservations仍0，schema6；accounts表现注册原Bulk经济账户是预期变化。
- 两份原凭证文件hash未变；无需用户重新填账户。发布前Native零仓零单/余额500.694838，备份 /root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261029T170754Z-a5801298 integrityok/last_seq0。备份在服务器保留，未下载master key。

## 本轮实现与验证

BulkObservation.preview_venue只接收public_record，构建无signer/protocol/order_transport、write_enabled/live_qualified均false的BulkAccountVenue及资金模型；ConnectionAccounts保存与restore不访问网络，凭证轮换先移除旧只读接入，失败仍保留已保存凭证且catalog回退观察。非预览/已签名交易接入禁止被预览替换；不向Bulk复用HL资金划转或认证流程。已挂接账户不重复加入连接目录。前端默认做多/全仓并禁用未支持选项，切换回其他账户会恢复其选项；规模算出时显示预览可用，创建仍需独立资格。

本机相关Python809pass、JS27pass；Linux冻结 /root/code/entropy-grid/data/staging/bulk-account-preview-v23/source 继承v22完整源核对后覆盖8文件，474项manifest未变，相关848pass/0fail/error/skip（Bulk全适配器、账户、API/网站、multi_market、router、UI观察等）。没有因局部变更再重复所有不相关故障测试。报告grid-bulk-account-preview-linux-v23.json/xml已取回；本机新增HTTP测试在GRID_REAL_DISPATCH=1下仍拒绝未认证真实create，覆盖两币种/两spacing/持久save重放+重启、轮换失效、注入signer/错账户等反例。

实际Bulk生产Python3.11.6（其venv无pytest）独立临时HTTP烟测成功：只用线上账户公开元数据，不读取密钥、不写原vault，BTC/ETH20倍预览成功；真实HTTP create在临时DB两次CAPABILITY_UNVERIFIED，临时4表0。Native仍flat/500.694838。grid-bulk-account-preview-native-v23.json记录此运行库与冻结源身份。发布脚本依848proof、真实烟测、原账户AID/REV、新鲜Nativeflat、空账本检查和备份后才切换，失败可回退；实际没有回退。grid-bulk-account-preview-deploy-v23.json已取回。

公网 https://taoli.coinfetcher.xyz/grid/bulk/#/grid/create 已实际验证：匿名401、原登录200，create.js内容逐字节匹配候选经Nginx前缀替换，catalog原账户只有一行/12市场、preview_only=true。实际BTC等比和ETH等差各20倍/40total-20reserve/4格预览量0.00106466/0.03667136，普通需求约18、压力约38.68，均nonlive；没有真实create/start或资金请求。grid-bulk-preview-public-v23.json/对应helper已保存；用户名密码只从本机0600的grid-public-login.json读取，未打印/上传。用户已收到发布进度。详细 reports/2026-10-30-bulk-account-preview-release.md；完整BULK-LIVE-GOAL矩阵顶部已更新。

## 下一步原生对账线索（新证据，不能用旧障碍停住）

只读grid-bulk-history-window-native-v24.json已在服务器产生：fills/orderHistory从0查HTTP200+hasMore=true，asOfSlot分别630213107/630466759；空fundingHistory的asOfSlot0。再按fills锚点[630213007,630213107]查三类，均HTTP200/hasMore=false，但coverage仍unknown/minAvailableSlot空，空资金费此时asOfSlot跟随请求end。**指定有限区间本身并不能让官方metadata完整，也不能仅凭asOf证明索引新鲜。** 不要据空记录伪造全历史覆盖或等待同样无变化的轮询。

实际fullAccount margin还提供累计经济量：fees -0.002099、funding0、realizedPnl -0.004063、unrealized0、totalMargin500.694838；Nativeflat。本机v19真实持仓快照的position亦含funding/fees/realizedPnl。可直接推进基于原发单身份+原生累计成交量的自有成交核验，以及账户/市场累计费用与资金费的有界净额对账；不能把净额吻合冒充每条历史流水完整，或无证据修改FundingPage.complete/正式资格。

另已核对当前官方 https://docs.bulk.trade/api-reference/getAccount 和 /ws-account：普通订单原生tif列postOnly/postOnlySlide/postOnlyJoin，现候选bulk_budget.order_matches及bulk_bound_read零数量终态对ALO仍只比'alo'。后续普通maker验证前应兼容**明确等价**的alo/postOnly而拒绝slide/join语义被冒认；保留原请求/签名编号不变，并做独立小额主网正面观察。这里尚未改代码/尚无本账户真实ALO样本，不能称已认证。

目标仍包括真实正常网格生命周期、买卖补单循环、原生止损触发/归零重挂、带仓重启/断线恢复、小残仓、资金费、至少24h和独立终局。v19/v20历史金融请求已closed，不能重跑。已有本账户小额/有期限验证授权有效，资金操作仅CLI/API；不复用HL授权，不恢复高频AI定时观察。下一轮从以上实际发布源与原请求状态继续，优先补执行/对账，不再重复只读预览准备。

# 历史：HL v21 独立终审通过受保护24h；Bulk v22 杠杆数量模型与原生只读预览通过

用户追问 HL 是否通过考核后，已实际只读核查最新普通轮 g-e3debcab696d41ba（RID ordinary-observe-linux-20261027-v21），不是旧 g-490d1620d25b421a。原轮已 CLOSED/STOPPED/e18，原 cleanup 496ba620-66f5-43fc-9d8d-23fc50aa4db6 SQL/状态 succeeded、remaining0，不得重放。观察器末样本距原起点双时钟86430.321秒，8次成功恢复和对账，耗时120.933–181.648秒，均在原5min上限内；无监督stop_reason或未完成恢复，保护时限未改。本轮只有初始买入与到期退出两笔成交，无网格配对；net_pnl -0.020198 USDC。原终局11DEX零仓零单，备份完整；本次实际额外Native查11DEX仍flat，账户195.818889，账本活动网格/未决operation/未知intent均0，证据grid-hl-v21-status-check-20261030.json。

已定位原报告 protected_24h_observed=false 的具体误判：严格观察器正常到期后因曾断线返回1，而控制器保护观察判定仍要求observer.returncode0。保留原严格ordinary=false及所有原文件；新增独立审阅工具planning/grid/tools/hl_terminal_review.py，从实际原最终日志、原范围/双时钟/期限/成功恢复/保护监督/原cleanup与Native终审重评“允许安全恢复的24h运行”。本机相关82通过、Linux隔离22通过，反例覆盖提前退出、短时钟、健康故障、未知订单、恢复超时、残仓和备份未核。真实服务器核对3个原控制器/监督源SHA、原终日志等于原报告、SQLite备份integrity和seq5718、3份配套文件SHA；新grid-hl-v21-terminal-review-v1.json protected_24h_observed=true，普通严格不间断仍false，release_qualified_by_review=false。只认证这轮受保护运行及已闭合对账，不授予新资金权限、不改正式服务资格、不声称本轮发生新的补单闭环。用户已获简要报告。此次0金融发送，HL/看护/套利均未重启。详见planning/grid/reports/2026-10-30-hl-v21-terminal-review.md。

Bulk v22 已实现真实 range_model，不再等待官方答复：BulkLongRangeBudget按请求杠杆及原生账户/市场上限，为当前版本每个逻辑格位取max(每格目标量,该格存量+未成交买量)，旧版本库存/挂单额外计入；覆盖费用、最高观察/范围价格、保护价格压力损失及原生已持仓保证金底线。减仓卖单不抵扣资金；选中库存与账户投影订单指纹必须一致，未确定订单拒绝。共享账户事务仍仅扣一次完整其他grid预算；持仓margin+未成交买入最低预留不足也拒绝。初始IOC按真实退出分配映射格位，超量存量不能遮住其他空格未来买入。全部Fraction精确运算/向上舍入，负费率不给信用。此为应用资金分配/压力模型，不伪造完整交易所接单公式，也不放开生产资格门。

共享sizing只对由代码声明、已证明每项需求随数量不下降的模型使用二分；其它既有模型保持枚举，固定恢复数量只校验不缩水。数量1e-8的上亿候选<35次计算；边界资金不足返回BUDGET_EXCEEDED，不误报交易规则错误。本机整套grid2845pass（在最后零预算叶子修复前），最后相关150pass覆盖最终修复；独立Linux冻结 /root/code/entropy-grid/data/staging/bulk-range-budget-v22/source 继承v21校验完整基线后覆盖8文件，472项源manifest前后未变，整套冻结grid2726passed/0fail/error/skip，报告grid-bulk-range-linux-v22.json/xml。不能混同本机另有HL候选的完整源。测试进程后降低nice15，已完成，不占资金。

实际主网只读v22成功：从原Bulk本地账户接口确认原AID/REV，临时独立SQLite+无protocol/order_transport/signer，BTC/ETH分别用新鲜行情和原生账户/市场杠杆上限计算20倍、40总/20预留/4格/±3%预览。BTC量0.0010592、ordinary17.9998827974784/adverse38.6810593734784，原生账户/市场max40；ETH量0.03657064、ordinary17.999997095493504/adverse38.681425428293504，原生max25。两者observed/非live，临时grids/intents/operations/reservations均0，无密钥访问或金融发送。证据grid-bulk-range-native-v22.json；此次只是分别预览，不冒称两个真实网格已同时运行。首尝试本地账户HTTP503在建立临时账本前失败，后确认接口200再首次执行成功；无金融重发。Bulk原worker2239205/web3222058继续active，原账本4表仍0。候选未安装生产，HL/套利版本未动。

下一步Bulk直接推进账户页/创建页只读预览实际接线（ConnectionAccounts当前仍仅catalog、restore跳过非HL；新资金模型尚未自动接入网站），随后正常网格资格与原生保护触发/带仓恢复/补单循环/24h终局。已完成v19/v20请求不可复活；完整目标仍未完成，不要把v22预览或HL终审当Bulk普通实盘已上线。无需等官方答复或再次索要已有小额验证许可；保留每笔有限预算/期限/原身份查询及现有生产门槛。

# 历史：Bulk 挂单预算底线已接入账户资金校验，Linux v21 115通过

2026-10-30（本地日期）代码候选：inventory_layout.portfolio_risk_context把每网格reserved_amount及selected_grid_id从真实SQLite上下文一并导出。BulkAccountBudget._capacity在确认原生仓单与账本一致后增加_pending_floors：现有OPEN/PARTIAL/ACCEPTED和PLANNED/BLOCKED_SLOT买单，按未成交数量*限价/网格请求杠杆计算本地预留底线；每网格预留不足即拒绝，即使native marginUsed0/availableMargin仍约500。减仓单不抵扣买单预算，终态单不计新增占用，选中网格修改资金时检查新config.capital.total。Fraction精确比较，显示值向上舍入，不再从全账户cap重复扣这一底线（Runtime已事务扣除整份其他网格reservation）。未提供reserved_amount不猜余额，拒绝缺失证据。reservation_evidence明确native_order_admission=false。

新增回放夹具tests/fixtures/grid_bulk/resting-account-mainnet-20261029.json，来源v20真实样本及原报告SHA，账户封套为测试重建，非新的主网证明。测试包括native零保证金但本地预留不足拒绝、足额后不重复扣费、修改预算、PLANNED/BLOCKED_SLOT、减仓无抵扣和循环小数精度；multi_market原实际Runtime测试确认SQLite导出80/80和other_reserved80、selected_grid_id正确。

本机与Linux组合（budget/multi_market/resting/roundtrip）均115passed；Linux21.28s，0failure/error/skip，0金融发送，无生产安装。报告data/run/grid-bulk-pending-budget-linux-v21.json，冻结目录/root/code/entropy-grid/data/staging/bulk-pending-budget-v21/source，继承v20全部源码先核完整manifest再覆盖5文件。此项是资金预算的必要底线，尚未计入所有已持仓、未来逻辑格位、费用和压力损失，不把它当完整原生准入或完整数量模型；range_model仍需实现。下一步直接补齐长网格杠杆数量模型和存量预算重验，利用已有主网40/20倍持仓及双GTC真实证据验证，不等待官方答复；不得宣称正式Bulk已上线或24h通过。v19/v20均已清仓撤单，原身份不能重启。

# 最新：Bulk 双挂单主网观察已闭环，确认可用保证金不扣未成交挂单

新RID bulk-resting-20261029-v20 已CLOSED/finished=true，独立unit entropy-grid-bulk-resting-20261029-v20.service inactive/MainPID0/ExecMainStatus0；不得重新启动同RID。专用Bulk观察本轮暂停PID2213098后恢复PID2239205 active，其余服务未由脚本重启。首轮准备在读取本地账户接口时503，已核对grant/run/policy/launch均不存在且0发单，接口恢复后才按同RID第一次创建授权、安装并启动；不是未知金融请求重试。

实现BulkRestingBudgetProbe：两笔BTC GTC buy各<=2USD、合计<=4USD，一笔原生固定止损覆盖两笔总量；维持原10min+5min退出范围、费用上限0.01USDC，不改杠杆/转账。开仓60秒窗口到期永久转清理；若意外成交先停止继续买入、撤剩余买单，按实际归属成交只减仓退出，最多5个退出原请求。原请求未知只查原身份。base roundtrip增加entry_ids/original_ids扩展点，取消RID使用完整相对CID，避免entry:1和exit:1取消身份撞号；idle恢复识别新模式的第二笔原身份。单笔原回归保留。

本机与Linux组合79 passed（7双挂单+33单笔+39订单证据/绑定读取），Linux14.54s/0failure/error/skip，外网禁用，无资金；冻结运行源/root/code/entropy-grid/data/staging/bulk-resting-admission-v20/source，测试报告data/run/grid-bulk-resting-linux-v20.json。正式普通网格仍未开放；此私有有限测试不算正式grid或24h。

实际顺序为：stop OPEN -> 单买单 -> 双买单，3次全账户样本均qty0且保护在场。样本pending买单数量0/1/2，对应挂单notional0/1.2003975225/2.4011665112 USDC，三个native availableMargin均500.694838、marginUsed均0。这证明本轮小额GTC挂单时不能靠交易所可用余额减少来替代本地预留；不证明大额/多市场准入边界。两笔entry最终原生CANCELED、累计已知0；stop CANCELED、累计未知按用户政策接受；无成交/手续费，余额未变。原policy approved1793289178735/expires1793289778735/退出宽限1793290078735，实际closed1793289279386，没有延期。

独立终局data/run/grid-bulk-resting-native-v20.json（checked1793289331891）确认原三单终态、全账户零仓零单、无pendingOnFill/lockedAmounts、BTC杠杆40、totalMargin500.694838，观察服务active；在线备份SHA8d1c8502d319d250be49c1c5a1c39656164ff7169c2c0b0054e11574353ed379。控制器原结果bulk-resting-20261029-v20.run.json已取回。本轮不把empty-account撤单观察冒充持仓保护故障或真实补单成交闭环。下一步把本地挂单预留及已有库存/逻辑格位纳入共享资金模型，继续多币种真实接入及正式恢复/24h验收；完整杠杆目标保留，不等待官方答复。

# 最新：不再等待官方；Bulk 主网20倍校验与受保护买卖已闭环

用户最新明确要求“你验证就好，我的目标是尽快上线”，覆盖此前必须等待官方答复的执行安排：保留完整杠杆首版目标，用已授权独立主网小额、有限期操作直接验证。旧撤单继续按用户人工检查和信任明确回执的政策处理，不重复索要同类授权、不恢复高频AI检查。

已执行新RID bulk-roundtrip-20261029-v19（不得重启/重发）：新设置校验器bulk_leverage_probe与受保护roundtrip，本机及Linux组合45passed（12设置+33roundtrip），临时身份官方SDK离线验证了settings签名字段与主/测试网隔离。独立stage /root/code/entropy-grid/data/staging/bulk-leverage-calibration-v19，unit entropy-grid-bulk-roundtrip-20261029-v19.service现inactive/MainPID0/ExecMainStatus0。原Bulk观察PID3802260仅本轮暂停，结束已恢复PID2213098 active；网站、HL和套利未被脚本重启。原SQLite在线备份后，原订单只读查证写入独立Bulk账本native_evidence字段，保留所有原nonce/回执，新的idle检查成功。settings请求使用同一原生nonce域、发送前持久MAY_HAVE_SENT、未知不重发；设置40->20和恢复20->40均收到肯定回执并实际读到新值。

真实流程：原生stop先OPEN，买入0.00001843BTC（约1.50USD，单次开仓上限2USD），持仓样本stop_present=true，20倍设置下native lambda0.05、notional1.50092215225、marginUsed0.07504610761250001；一次reduce-only IOC退出同量，fee总0.00105USDC；3次flat检查后撤stop，CLOSED/finished=true/real_roundtrip=true，无deadline超时。原固定policy approved_ms1793287199419/expires_ms1793287799419、交易退出宽限至1793288099419，从未延期。控制器结束finished_ms1793287286581，已恢复40倍。完整history仍unknown，不能以此称24h或全杠杆/多市场模型已认证。

独立只读终局审计data/run/grid-bulk-calibration-native-v19.json（checked_ms1793288321689）：原entry/exit均FOUND_TERMINAL/FILLED且累计已知同量，原stop FOUND_TERMINAL/CANCELED（累计未知按用户最新政策接受），交易所零仓零挂单、无pendingOnFill/lockedAmounts、杠杆40、totalMargin500.694838。相对前基线500.696194本轮净减少0.001356USDC（包含手续费）；观察服务active。终局SQLite备份SHA6076c639cdee5a3302314d133f119fd4ee1af13565e282a0183ff3320d252957。证据grid-bulk-calibration-linux-v19.json、bulk-roundtrip-20261029-v19.run.json/.leverage.json已取回本机。单次只读终局查询曾自动审批超时，按允许重试后成功，没有重投交易。

下一步：将40倍2.5%旧样本与20倍5%新样本用于资金模型校验，继续真实挂单资金占用、多单累计与多币种共享预算验证；支持完整杠杆目标不缩减成1倍。保护触发/恢复/补单循环/至少24h及正式部署仍待完成，不将本轮协议测试当完整上线。运行源v19保持冻结。本机helper随后补了目录fsync增强持久性，12设置测试通过，但该微调未覆盖v19 Linux/主网源或生产安装；不要混淆源版本。

# 最新：用户确认首版必须支持完整杠杆，待 Bulk 明确规则后开放（执行安排已由上文更新）

用户已回答资金预算方式问题，明确选择“首版就要求支持完整杠杆能力，等待 Bulk 明确规则后再开放”。此选择覆盖下面待选择状态：不将约1倍足额预留模式替代首版目标，不重复询问同一预算偏好，不绕过未明确的完整杠杆准入规则开新网格。已有主网操作授权继续有效，但开放以规则明确、实现及真实验收通过为前提。后续需要 Bulk 对 planning/grid/reports/2026-10-28-bulk-api-questions.md 中账户杠杆下限、曲面范围外行为、挂单组合准入与相关历史问题提供可验证说明；收到答复后继续实现、部署及实盘验收。用户接管旧单人工检查与信任明确撤单的政策保持有效，旧stop历史不再重新作为新轮启动阻塞。不恢复高频AI轮询。

# 最新：首版资金预算方式待用户选择（已由上文决定）

已通过异步问题询问产品偏好：先提供按全部开仓名义金额足额预留（约1倍）的保守首版并后续扩展杠杆，或首版必须完整杠杆且待官方明确规则。不是再次索要真实操作授权；已有主网授权继续有效。尚无答复，不擅自将全目标缩成1倍版本或把预算估算标为原生准入证明。

已检查实际接入点：BulkMarketVenue.risk_model_for → BulkAccountBudget.range_model，preview/operations/LiveRiskGuard共享数量模型；Runtime事务统一扣除other_reserved，应沿用同一账户预算锁，不能在每市场重复制余额。未来保守模式也必须覆盖已有库存、工作开仓单及所有逻辑格位、退出费用和范围损失；减仓挂单不能预先冲减开仓预算。以上仅接口设计复核，未新增代码或生产/交易动作。旧stop人工清理政策继续有效。

# 最新：真实小额保证金样本与账户杠杆倒数完全一致

重新分析已保存的2026-10-26主网真实持仓样本（未开新仓）：BTC qty0.0000188，native lambda0.025、position leverage40，账户 notional1.495014258/marginUsed0.03737535645，二者比值精确0.025且等于1/40。脱敏、含原始文件SHA的复算证据data/run/grid-bulk-native-small-margin-replay-v1.json。2026-10-29 13:51:42UTC官方公开riskSurfaces BTC HTTP200，九状态首buy/sell mmrO/E均0.02，最小notional50000/杠杆1、liveRegime0、原生leverage字段仍notional_0/notional_half。数据不同时间，不能认定配置相同或已有完整复现；提示账户杠杆倒数下限是需要核实的候选规则，不能把2%首节点直接用于小额准入。问题已追加官方清单，0金融发送、生产未动；正式range_model仍未开放。旧stop人工清理政策继续有效。

# 最新：Bulk 资金模型的风险范围上界候选通过

补充 bulk_risk.RiskGrid.rate_upper_bound：检查价格/名义金额与杠杆矩形内全部相交单元节点，以 origin/equilibrium 较大值覆盖任何衰减时刻；BulkRiskSurfaces 再对全部九种状态取上界。使用 Fraction 精确插值权重，最终 Decimal 60位向上舍入；不对原生范围之外做外推。修复初次测试输入格式/内部 Decimal 参数问题后，本机risk/risk_feed/budget108 passed；初次普通权限测试的本机回环 bind 被拒，升级仅为本机模拟网络后通过。Linux隔离v18同组合108 passed/52.31s，0金融发送、无生产部署，data/run/grid-bulk-risk-range-linux-v18.json；候选位于/root/code/entropy-grid/data/staging/bulk-risk-range-v18/source。

此计算只是正式资金模型的维持率上界组件，range_model仍未接通/认证。保存的BTC/ETH主网风险面最小名义节点50000、最小杠杆1，小额范围仍没有确定原生外推规则；不能以此组件宣布能实盘，清算储备、开单风险规则和历史/保护/恢复/24h仍需验证。说明已追加planning/grid/reports/2026-10-28-bulk-risk-admission-evidence.md。用户接管旧单清理的最新规则继续有效，旧stop历史不能再次作为下一轮诊断的阻塞。

# 最新：用户明确接管旧单清理；新轮启动不再要求旧撤单的完整成交量

用户最新指示：止损后由用户保证全部订单撤掉，下次网格启动前用户人工检查，程序可以信任收到的明确撤单记录。此指示覆盖之前把旧 stop 未知累计量一律视为新轮阻塞的规划；无需再次请求相同授权或等待官方补齐旧 stop 成交才继续新轮验证。未获得确定回执的超时/未知请求仍按原身份查询，不重复发送。

已修改 bulk_roundtrip_cli.require_idle：同账户/部署/原请求与原生订单绑定一致的 CANCELED 证据即可关闭新轮启动检查，适用旧普通单及 stop；已核验的 legacy stop terminal_resolution 也可使用。不要求过去累计量为零/已知，不改历史数量、nonce 或原始回执。新轮本身继续读取原生账户、要求零仓零单；不改变活跃策略的成交归属、账务历史覆盖与资格审核。

本机相关72项通过；Linux独立 v17冻结源覆盖两文件，禁外网测试72 passed/12.80s、0金融发送，报告data/run/grid-bulk-cancel-policy-linux-v17.json。真实原订单只读+生产SQLite backup副本检查：两笔stop FOUND_TERMINAL/CANCELED且 cumulative_known=false，原entry/exit FILLED/累计已知，原生账户零仓零单，新的 require_idle 成功；证据data/run/grid-bulk-cancel-policy-native-v17.json（checked_ms1793283817313）。生产账本/运行服务未修改，候选在/root/code/entropy-grid/data/staging/bulk-cancel-policy-v17/source，不声称已部署正式网格。旧 stop 不再是下一轮诊断的待解阻塞；普通网格的资金风险模型及真实保护/恢复/24h验收仍未完成。

# 最新：Bulk 只读接口恢复，历史覆盖仍未知

2026-10-29 13:27:24 UTC 线上原独立账户只读复核：fullAccount HTTP200 返回 positions 0、openOrders 0、availableMargin 500.696194；orderHistory HTTP200 返回4行、fills HTTP200返回2行，二者 coverage 均 unknown 且无 minAvailableSlot。orderHistory asOfSlot 597042203、fills asOfSlot 597037868；历史快照位置未超先前记录，不足以认定是否索引滞后或是否完整。生产账本 grids/order_intents/operations/budget_reservations 均0，entropy-grid-worker@bulk 与 entropy-grid-bulk-web active；0金融发送。原 stop 零累计/子单关系及风险准入仍无可靠证明，继续阻止新的交易；0 仓单是该次 API 返回，不应据此推断历史完整。无官方答复。

# 最新：更正 Bulk 官方文档支持入口

重新检查 https://docs.bulk.trade/api-reference/introduction 页面内 Support 链接，实际指向 https://t.me/junbug01；上次回复自行给出的 t.me/bulk_support 未经验证，不能当官方入口。未发送任何消息。主网查询最近一次 2026-10-29 00:59:01 UTC 仍 HTTP 504；旧止损执行证据及资金准入问题无新证明，等待官方答复，不将其视为可安全交易。

# 最新：Bulk 官方只读接口返回 504

2026-10-29 00:58:19 UTC，线上使用 aiohttp 只读复核：Bulk 本地账户服务 HTTP 200，官方 mainnet exchangeInfo GET 与 fullAccount POST 均 HTTP 504；因此不能确认当前仓单余额，也不能从响应断言故障归属。线上 entropy-grid-worker@bulk.service 与 entropy-grid-bulk-web.service active，生产账本 grids/order_intents/operations/budget_reservations 均 0。未发送交易、未改生产。相比前次账户 403，市场及账户两个只读请求此次均为 504；原 stop 历史与资金准入问题仍未解决。下次仅在具备可执行线索时恢复只读核查，不能用账本零任务断言交易所账户零仓单。

# 最新：Bulk 账户只读查询 403，暂不能确认当前仓单

本轮只读：线上 entropy-grid-worker@bulk.service 与 entropy-grid-bulk-web.service 均 active；Bulk 生产账本 grids/order_intents/operations/budget_reservations 均 0。通过官网 mainnet API 的 fullAccount 查询返回 HTTP 403，不能据此判断账户现有仓位、挂单或余额，也不能归因交易所本身；使用另一个只读客户端的尝试未返回可核验证据。未发单、未改服务。官方最新文档仍说明止损触发会提交减仓子单，未提供此旧 stop 子单归属或历史完整性证明；风险面仅是单市场配置，不能单独证明账户级网格准入。既有旧 stop 未知成交与资金风险准入阻塞仍在，不可启动新交易。需恢复只读账户查询并取得可靠历史/风险语义；此前小额主网授权有效。

# 最新：Bulk 旧 stop 栅栏绑定 Linux v16 通过

本轮 progress：基于 v15 manifest 校验的冻结源仅覆盖栅栏和测试，Linux v16 roundtrip/order-evidence/bound-reader exit0、70 passed、0 failures/errors/skipped、14.06s；0金融发送、生产未安装。data/run/grid-bulk-history-linux-v16.json/xml，XML SHA f8fc94fbea088cb464c9142a49a4fe90ec45fa1c35b84e73f89e27a2dac17d9e，两个源 manifest 与70 case核对一致。测试正例人工提供已知零累计，不证明真实旧 stop 状态；真实历史未知、原止损子单与风险模型仍未解决，不能据此发新单。

# 最新：Bulk 旧 stop 栅栏绑定检查收紧

本轮 progress：在 v15 候选之上，旧 stop 的零累计证据除原账户/网络/请求 hash 外，必须与已撤销原订单身份绑定、原生撤销类型一致、历史起点为非负整数且一致；旧普通单历史起点也必须真实存在。篡改撤销类型拒绝的离线测试及 roundtrip 31 passed，相关组合此前70 passed。此修改尚未 Linux 验证或生产安装；测试正例人工构造，真实 stop 的零累计仍无证明，不能把 ID 匹配视为完整历史。

# 最新：Bulk 旧 stop 栅栏 Linux v15 通过

本轮 progress：v14 冻结源只覆盖旧 stop 栅栏和其测试，Linux v15 roundtrip/order-evidence/bound-reader exit0、70 passed、0 failures/errors/skipped、12.04s，0金融发送、不安装生产；data/run/grid-bulk-history-linux-v15.json/xml，XML SHA 226a5b9dac26d344fc4686adc380ef5155207661e5cfd1f05ce8680631be9c80，核对源码 manifest、70 case、stop 栅栏测试存在。测试正例人工提供零累计证据，不是主网证据；真实 stop 数量未知，仍无法批准后续网格开单。原始普通订单证据只在独立副本，生产未修改。

# 最新：Bulk 旧 stop 栅栏候选不再将撤销视作零成交

本轮 progress：修复 v14 候选旧 stop 分支仅检查撤销身份、未检查累计成交的缺口。候选 require_idle 现在还要求同账户/部署的原身份与 request hash 一致、CANCELED 原生证据及已知累计成交为零；未知、非零或不一致继续拒绝。离线 roundtrip/order-evidence/bound-reader 70 passed，测试正例为人工构造证明，非真实主网证明。当前实际 stop 的累计成交仍未知，现有 reader 不能凭原历史记录自动证明零，因此旧 stop 仍阻断。尚未 Linux 回归或写入生产；0真实下单。

# 最新：Bulk 旧成交证据写入账本副本并重启验证

本轮 progress：Linux v14 源 manifest 完整核验，通过 SQLite backup 将生产 Bulk 账本复制至独立副本；对原 bulk-roundtrip-20261026-v1 entry/exit:0 做只读原生查询，两单均 FOUND_TERMINAL/FILLED/累计已知，在副本用真实 BulkIntentJournal.record_lookup 保存，重复写入去重且原 nonce/receipt 等字段不变；关闭重开副本后两个终态证据仍在。新 require_idle 检查仍返回 BULK_ROUNDTRIP_UNRESOLVED_PREVIOUS_INTENT，不认定所有旧单已清理；先前已查上一 roundtrip stop 已撤但成交数量未知且无 terminal_resolution。副本 SHA a77df40bb9011a7acea2ef6d960a3628b5898085299d93238e947169984edf89，脱敏报告 data/run/grid-bulk-v14-native-clone-check.json。0金融发送、生产账本未修改，正式历史覆盖及 stop 执行仍未证明；用户已授权小额主网验证，但不能以副本证明作为放开 stop 栅栏的依据。

# 最新：Bulk 旧普通订单证据栅栏 Linux v14 通过

本轮 progress：v13 冻结源仅覆盖 roundtrip_cli 与测试文件，独立 Linux v14（roundtrip、order evidence、bound reader）exit0、70 passed、0 failures/errors/skipped、11.40s，0金融发送、生产未安装；data/run/grid-bulk-history-linux-v14.json/xml，XML SHA 1ff0a4d6223d9199191310b8a9d6667c3a1eb736ce193a2e8d345ce799f00fdd，文件 manifest、70 case 和新增 case 已核对。澄清：新规则只处理普通单，原先 legacy stop resolution 分支仅检查原根单撤销绑定，并不证明累计成交量；本账户当前 roundtrip stop 没有 resolution，仍不通过当前栅栏。不得将既有 stop 分支误述为全场景累计成交证明。尚未写生产证据或发新订单。

# 最新：Bulk 原订单证据的启动前栅栏候选

本轮 progress：本机小额诊断 require_idle 增加严格检查：旧普通单仅在持久 native_evidence 是相同账户/部署、相同原 request hash 与订单绑定、FILLED 且累计成交已知并等于原数量时可视为已终态；原 MAY_HAVE_SENT 回执、nonce、原请求保持不变。新普通单规则不适用于 stop；现有旧 stop resolution 分支按原实现检查身份及撤销状态，并不证明累计成交量，当前 roundtrip stop 没有该 resolution，仍被阻断；旧证据缺失、绑定/数量/摘要不匹配均拒绝。真实 BulkIntentJournal + 离线 sender 测试验证证据落盘前拒绝、完整成交证明后允许及篡改拒绝，同时未解决旧 stop 继续阻断；roundtrip 本机31 passed。修改仅候选、未 Linux 测试、未写生产 journal 或发送真实订单。下一步冻结两项候选、验收持久证据并继续核实 stop 的未知累计成交，不得静默放行。

# 最新：Bulk 旧成交隔离 Linux v13 通过；旧日志证据尚未落盘

本轮 progress：比对 v12 隔离基线后仅覆盖 roundtrip 实现和测试两文件，Linux v13 的 roundtrip、订单证据和绑定读取回归 exit0、69 passed、0 failures/errors/skipped、10.80s；0金融发送、生产未安装。data/run/grid-bulk-history-linux-v13.json/xml，XML SHA ca5138d40bbb4dfe352fc237073ba1aca53121e3c137ec27f1cfacb823b82768，核对两个源文件 manifest、69 case 和2项新增 case。旧记录隔离不能证明历史覆盖完整。

只读检查生产 Bulk 原 journal：旧 probe stop 的原身份有 terminal_resolution；上一 roundtrip 的 stop ACK_ACCEPTED 无 terminal_resolution，entry 和 exit 原记录 MAY_HAVE_SENT，无持久 native_evidence。虽然此前独立只读查到 CANCELED/FILLED/FILLED，stop 累计数量仍不明，不能把只读结果当已落盘、忽略旧记录或直接放松 require_idle。下一步需设计与检验持久证据写入、保留原 nonce/receipt、碰到未知成交保持保护和阻断；新主网授权仍有效但不自动改变风控。无金融发送。

# 最新：Bulk 有历史成交账户的下一轮诊断候选

用户已授权已配置独立 Bulk 账户真实验证。本轮进展：发现原小额诊断要求从未有过成交，而此账户上一轮已有成交，不能直接复用；本机候选改为将起始可观察的旧成交按原生 identity 和内容摘要保存，后续只剔除内容完全一致的旧记录，变化或新发现的外部成交仍拒绝；记录原 history coverage，不能把 unknown 标为完整。30 项离线 roundtrip 测试通过，覆盖旧成交不计入新一轮及旧记录变化拒绝。本机未部署或发送金融请求，尚未 Linux 验证；旧请求 durable journal 中仍有 MAY_HAVE_SENT 与 stop 成交量未知，需要审计和有证据的安全恢复，不能放宽 require_idle 直接下单。该候选只为小额诊断，不是普通网格风险资格或实盘上线。

# 最新：用户授权 Bulk 主网真实验证；只读前置核查

用户明确说“可以用主网进行验证，我给你的账户可以进行真实操作”，这是新的 Bulk 独立主网真实验证授权；不索要密钥、不默认无限金额或放松验收。2026-10-29 只读检查已配置账户，交易所 fullAccount HTTP200，零持仓、零挂单，可用保证金500.696194 USDC；原 bulk-roundtrip-20261026-v1 的 entry 与 exit 原身份独立查到 FILLED，stop 查到 CANCELED 但累计成交数量未知。Bulk 生产 worker 与网站 active；生产账本 grids/operations/order_intents/budget_reservations 均0，无运行网格或待处理操作。脱敏证据 data/run/grid-bulk-mainnet-authorized-precheck-20261029-v1.json。只读核查没有发送金融请求。

原真实 stop 累计数量未知、实际 fills/funding 覆盖 unknown、Bulk range 风险模型仍未认证，候选 v12 虽离线886通过仍未安装生产；不能将授权解释为绕过这些安全条件或直接开启常规网格。准备新的小额、有期限、原身份可追踪的主网验证并确认持仓保护、退出与未知状态收尾，再按明确证据推进；先在隔离环境验证实现和运行服务约束，不重复发送旧请求，不影响套利及 HL。测试网原 faucet may_have_sent 与主网账户分离，不重发。

# 最新：测试网水龙头发送超时不重复离线验证

本轮 progress：新增 tests/test_grid_bulk_testnet_faucet.py，使用虚构密钥与完全离线 transport，对实际派发脚本验证发出前持久记录 may_have_sent、模拟超时后记录未确认、再次调用同一脚本不重新发送且不改原记录；本机1项通过。该候选仅测试且未在服务器运行，不证明原请求结果；原测试网请求保持 may_have_sent、没有重发。Bulk 主网准入不变。

# 最新：测试网水龙头回执判定收紧

本轮 progress：本机测试网派发候选仅在 HTTP200、正确 order 信封、单个 deposit 项及有限正数金额时记录 receipt_received；空对象、零额、布尔/NaN、混合状态及错误信封均不认证，9个离线判定样例通过。脚本未对服务器原请求重新运行，不能补回此前丢失的响应；原 phase may_have_sent 保持，既不重发也不创造新身份。Bulk 正式实盘条件不变。

# 最新：Bulk 测试网未知请求恢复问题已整理

本轮 progress：复核官方 faucet 文档明确支持纳秒唯一 nonce，原请求采用纳秒不能据此认定格式错误；由于原 HTTP200 正文未保存，账户404也不能证明原请求永不执行，原请求保持 may_have_sent，不重发。已将具体只读结果与原请求结果查询/终态证据问题加入 planning/grid/reports/2026-10-28-bulk-api-questions.md，未发送给外部；无金融发送、无服务改动。需要官方可靠接口/证据继续同一测试网身份验证，不能以测试网替代主网实盘前置条件。

# 最新：Bulk 测试网原身份再次未查到

本轮 progress：对原测试网身份只读 fullAccount 与 activityHistory，均 HTTP404 ACCOUNT_NOT_FOUND，证据 data/run/grid-bulk-testnet-faucet-verify-v1.json；不证明原请求未执行，不重发、不创建新身份。之前响应正文没有保留，无法回溯具体拒绝原因；本机派发脚本补充后续请求的有限状态标签诊断（不记录响应正文），不修改或重发当前请求。0金融发送；后续需要可靠原请求/测试网事件证据，才能判断是否可安全继续。

# 最新：Bulk 测试网水龙头原请求结果未确认

本轮 progress：在服务器对已持久准备的独立测试网 faucet 请求，在发送前 fsync 记录 may_have_sent，严格核对原 payload hash、testnet 域2、账户/签名人/接收者一致及测试网 URL；仅发送一次，HTTP200 但未取得预期 deposit 收据，phase 仍 may_have_sent。立即对同一原测试网身份只读 fullAccount，HTTP404 ACCOUNT_NOT_FOUND；只能说明该次读取没有查到账号，不证明请求绝未执行，禁止重复 faucet 或生成新身份。原请求及私有锁位于 /root/code/entropy-grid/data/bulk-testnet-isolated-v1，脚本 planning/grid/tools/dispatch_bulk_testnet_faucet.py；0主网交易/未使用用户密钥，未知响应正文未保存，不得推断原因。下一步只读查原身份或测试网状态，处理确认/故障后才继续。正式 Bulk 实盘门槛不变。

# 最新：Bulk 独立测试网身份及请求已准备

本轮 progress：在 Linux 独立目录 /root/code/entropy-grid/data/bulk-testnet-isolated-v1 使用新生成测试专用身份，目录0700、密钥及请求0600；按 testnet 域2 准备单个 faucet 请求，确认动作仅 faucet 且 account/signer/接收者为同一新身份；记录 phase prepared_not_sent 与原 payload SHA256 328810d09ba6157527826b68c61675d5afcf9dfee32c10ff12d3f064fc36cf06，准备脚本 planning/grid/tools/prepare_bulk_testnet_faucet.py。没有发送请求、未读取用户主网密钥、不打印密钥或地址、未部署服务。该目录一旦存在不得重建或覆写；后续先校验原身份与原请求并设计崩溃后查询/无重发，才执行测试网 faucet。测试网结果不等于主网准入或资金授权。

# 最新：Bulk 测试网水龙头签名离线验证

本轮 progress：Linux 已固定的 bulk_keychain.prepare_faucet 以临时内存密钥构造相同身份与 nonce 的 testnet/mainnet 消息，分别验证域尾字节2/1、签名消息不同；testnet 的 sign_prepared 与 finalize_transaction 离线往返通过。临时密钥丢弃，无网络交易、无密钥输出或保存。下一步需先为测试网创建隔离持久身份和原请求日志（结果未知时可查原身份），验证主/测试网域及目标地址后再请求测试网模拟资金；不得使用已有主网账户/密钥或将测试网结果当主网证明。真实历史覆盖、风险模型和带仓/终局仍未完成。

# 最新：Bulk 测试网公开市场可访问

本轮 progress：核对官方 API Overview 确认测试网独立地址及签名域、faucet 仅测试网；Linux 现有 bulk_keychain 公开接口包含 prepare_faucet。服务器对官方测试网 exchangeInfo 单次无签名 GET 返回 HTTP200、17个市场，证据 data/run/grid-bulk-testnet-public-reachability-v1.json；0金融发送、未创建账户或请求 faucet。下一步可在独立隔离测试账户设计测试网成交/故障验证，先明确签名域、证据和结果未知处理；测试网不能代替主网历史、风险、真实带仓及资金授权。此前被自动审批拒绝的交易对手查询未执行。

# 最新：Bulk 组合候选 Linux v12 回归通过

本轮 progress：entropy-grid-bulk-history-linux-v12.service 已终结 exit0，886 passed、0 failures/errors/skipped、207.99s、1 既有 aiohttp warning，0金融发送、不安装生产；data/run/grid-bulk-history-linux-v12.json/xml，XML SHA 70ecdd5a391b5436b49da1a3c83f68373f990f62aaf1e45b870cfaa3ec6d4546，核对 886 个 XML cases、部分成交及字节上限四个关键 case、v11 候选检查点全部文件与 v12 source manifest 一致。仅说明离线回归；官方状态语义、真实历史覆盖、风险模型、带仓退出、独立资金授权及24h终审仍未完成。生产无部署。

# 最新：Bulk v11 候选检查点与 v12 回归

本轮 progress：组合候选代码检查点 data/grid-code-checkpoints/grid-code-20261029-bulk-history-v11.tar.gz SHA 1e9bc7dc011186175ae14112d91aa26ec6547e7d8ae49ba47ceee6fac6ea8613；14 个候选文件逐项匹配 v11 source manifest，不含凭证，不是完整部署包。v12 原 unit 最近只读 active/running PID1172362，约8%输出，尚未终结；不能称全量通过或重复启动。真实覆盖、风险模型、部分成交语义、带仓退出和独立资金授权不变；无生产部署及金融发送。

# 最新：Bulk 组合候选 Linux v12 全量回归运行中

本轮 progress：继承 v11 源并校验完整 manifest，不覆盖任何文件，恢复 v6 全量测试选择（含后来已合并的部分成交和历史边界测试）。entropy-grid-bulk-history-linux-v12.service 已启动并 active/running PID1172362，外网及金融发送禁用，不安装生产；结果尚未知，后续查询同一 unit 与 data/run/grid-bulk-history-linux-v12.json/xml，不重复启动。v11 71 / v10 172 是不同选择的既有通过结果，不代表 v12 通过。真实历史覆盖、风险模型、部分成交语义、带仓退出和独立资金授权仍未完成。

# 最新：Bulk 未知结果遇部分成交 Linux v11 通过

本轮 progress：核对 v10 基线订单证据测试与本机仅新增一项；继承完整校验的 v10 冻结源，隔离 v11 仅覆盖该测试文件并运行订单证据、执行、bound reader。entropy-grid-bulk-history-linux-v11.service 终结 exit0、71 passed、0 failures/errors/skipped、6.50s，0金融发送、不安装生产；data/run/grid-bulk-history-linux-v11.json/xml，XML SHA d9ab2f0d8c945ef8f5d1ad9329fa37cac931c608526600a9366b599ceddc2398；核对 manifest、71 cases 及新增 case。仍不证明真实交易所部分成交语义；真实历史覆盖、风险模型、带仓退出及独立资金授权仍未完成。

# 最新：Bulk 原请求结果未知遇部分成交的离线验证

本轮 progress：新增实际 Bulk execution → bound reader → durable journal 测试，模拟原请求超时后历史出现 partiallyFilled；原身份、nonce 和记录保留，查原单及再次调用 submit 均仍为 UNKNOWN，未重复签名或发送。order evidence、execution、bound reader 组合在允许本机回环环境 71 passed；首次沙箱内 70 passed/1 failed，失败是测试服务器本机 socket bind PermissionError。新增断言尚未 Linux 验证，不代表真实主网行为；线上未部署、无金融发送。真实历史、风险模型、带仓退出和独立资金授权仍待解决。

# 最新：Bulk 部分成交保守处理 Linux v10 通过

本轮 progress：核对 v9 冻结源与本机四文件差异仅为部分成交处理及对应测试；构建隔离 v10 后仅运行 reader、bound reader、venue、stream、recovery。entropy-grid-bulk-history-linux-v10.service 终结 exit0、172 passed、0 failures/errors/skipped、15.99s，0金融发送、未安装生产；data/run/grid-bulk-history-linux-v10.json/xml，XML SHA 27a54dae8fd199378c7909be3776d3cdac7d9b3af729df7c59ec1929d90a02ed，4文件 manifest 和 172 case（含2个新测试）已核对。是范围内离线 Linux 验证，不是全套或真实语义证明；真实历史覆盖、风险模型、带仓退出、独立资金授权与24h仍未完成。

# 最新：Bulk 部分成交暂按终态未核实处理

本轮 progress：鉴于官方 API 与 SDK 文档对 partiallyFilled 是否有剩余挂单存在分歧，候选 Bulk reader 在原订单历史为该状态时保留已观察数量下界，但返回 NOT_FOUND_TRANSIENT、cumulative_known=false，不认定终态；同时已绑定订单私有流收到 partiallyFilled 时标记 gap，进入对账。开放订单与终态历史冲突仍拒绝。离线 reader、bound reader、venue、stream、recovery 组合本机 172 passed。此为保守暂行处理，可能使合法部分成交停在待核对；不证明交易所实际语义，必须待官方/真实证据核实后完善，不降低实盘标准。尚未 Linux 回归或生产部署，无金融发送。

# 最新：Bulk 已发布 SDK 部分成交谓词互相重叠

本轮 progress：Git 两次失败后，从 PyPI 仅下载、不安装或运行官方文档所指 bulk-client 0.1.2 wheel，SHA256 f911bc688583c9974e0610f0a9035199941a3351c705589d6c793788c498daae（与 PyPI 元数据一致）。静态检查 bulk_api/common/enums.py：partiallyFilled 映射枚举4，is_terminal 为 value>2，is_placed 为 0<value<5，因此两者均为真。此为已发布版本本身的实现证据，不等同最新源码或交易所现行实际终态；原官方文档分歧问题清单已补充，准入不放宽。未发送账户信息或交易，未部署生产。

# 最新：Bulk 官方 SDK 浅克隆两次失败

原 HTTP/1.1 重试会话 23106 已终结 exit128，Git 报 empty reply / expected flush；之前 HTTP/2 克隆也失败。官方源码未获取，不据此判断部分成交语义。现有官方文档矛盾已记录待确认；未发送账号信息或金融交易。此前交易对手只读对照受自动审批拒绝且用户尚未授权，未绕过。

# 最新：Bulk 官方 SDK 浅克隆 HTTP/2 失败，重试进行中

上一原会话 72645 已真实终结 exit128，原因 HTTP/2 framing layer；未获取源码。独立目录以 HTTP/1.1 发起一次公开只读重试，原会话 23106 最后查询仍运行，未有结果，不重复启动；待终结后查看实际提交和状态实现。两次均未发送账户数据或金融交易。实盘准入不变，自动审批拒绝的交易对手查询未绕过。

# 最新：Bulk 官方 SDK 源码只读获取中

本轮 verified wait：为核对官方部分成交状态定义，启动公开 GitHub Bulk-trade/bulk-client 的只读浅克隆至 /private/tmp/bulk-official-sdk-review-20261029。原 exec 会话 72645 最后一次查询仍运行、尚无结果；不得因为等待超时重启或称源码已获取。后续查询原会话，完成后检查固定提交及 SDK 状态实现；不改变网格准入、不发送账户数据或金融交易。被自动审批拒绝的交易对手查询仍未执行。

# 最新：Bulk 部分成交冲突 Linux 验证通过

本轮 progress：确认 v8 Linux 基线 test_grid_bulk.py 与本机只有部分成交冲突参数化差异；由 v8 manifest 校验的冻结源构建 v9，仅覆盖该测试文件并运行 reader/bound reader。entropy-grid-bulk-history-linux-v9.service 终结 exit0、77 passed、0 failures/errors/skipped、4.58s，0金融发送、生产未安装；data/run/grid-bulk-history-linux-v9.json/xml，XML SHA b8b2e6e21deac8fdae658668e38a677360929c08273fd8f13b3eef4d7e36a4e8，核对文件 manifest、77 case 和 partiallyFilled case。此模拟只证明历史终态与挂单同时出现时拒绝冲突；官方两份文档真实语义分歧未解决，历史覆盖、风险模型、真实带仓与资金授权依然未满足。被自动审批拒绝的对照查询未发送。

# 最新：Bulk 部分成交官方描述不一致

本轮 progress：官方 placeOrder 文档将 partiallyFilled 列为终态，但官方 Python SDK HTTP 文档描述其余量仍挂单；尚不能判断是否 SDK 描述过时或语境不同，已加入 planning/grid/reports/2026-10-28-bulk-api-questions.md 待官方核实。现有 lookup 在同一订单同时出现终态历史与挂单时拒绝冲突；离线新增 partiallyFilled 的该竞争场景，本机 Bulk reader/bound reader 测试在允许本机回环的环境 77 passed。首次沙箱运行 73 passed/4 failed，输出被超大测试内容截断，不能断言全部失败原因；随后回环权限下同一组合 77 passed。此测试不证明实时交易所部分成交语义，不改变已有状态解释、不开放实盘；新增测试未做 Linux 验证。交易对手对照自动审批拒绝仍有效，未发送。

# 最新：Bulk 历史缓存字节上限 Linux v8 通过

本轮 progress：从通过的 v7 冻结源及完整 manifest 构建 v8，只覆盖新增测试文件并仅运行 checkpoint 测试；entropy-grid-bulk-history-linux-v8.service 已终结 exit0、27 passed、0 failures/errors/skipped、6.03s、0金融发送、生产未安装。data/run/grid-bulk-history-linux-v8.json/xml，XML SHA a796b08dfc05e62da8cdeda75e713882c34ecc4b3ab6932dd0766b55557bcafb，XML 27 case、字节上限测试存在且文件与 manifest 校验通过。不能据此认定历史超过16MiB可恢复或主网覆盖完整。交易对手查询仍因自动审批拒绝及无用户答复未执行，不得绕过。

# 最新：Bulk 历史缓存字节上限验证

本轮 progress：本机新增真实 Bulk market view 的离线字节容量测试，故意让第一历史页提交、第二页达到容量上限，验证保留原已提交页和原游标、无完成标记、不将上限错误当瞬时可恢复；checkpoint 测试文件 27 passed。测试人为降低上限，不证明真实 16MiB 外历史可恢复；新增测试不在 Linux v7 冻结范围，未部署生产。此前交易对手对照查询的自动审批拒绝仍生效，未获用户新授权，没有绕过或发送账户数据。

# 最新：Bulk 交易对手对照查询被自动审批拒绝

本轮拟从已有真实成交记录提取交易对手地址并发送至 Bulk 公开账户接口作为只读历史对照；自动审批拒绝，理由是该地址来自私有账户记录，向外发送属于未经授权的敏感数据外发。命令未执行、没有发送查询；不得通过变换手段绕开该拒绝。公开文档示例此前返回404，真实网格账户此前覆盖 unknown，均不证明其他账户情况。若要继续该特定查询须先取得用户明确授权并遵循审批；不得泄露密钥或声称查验已完成。Bulk 准入不变。

# 最新：Bulk 公开示例账户对照未取得有效历史

本轮尝试用官方文档公开示例地址对照同一历史 slot 窗口。本机两次只读查询均 URLError；从服务器分别查询 fills/fundingHistory 得到 HTTP404、非历史响应，无法判断原因，也不能推断其他账户的覆盖或示例账户存在与否。data/run/grid-bulk-public-history-control-v1.json 与 grid-bulk-public-history-control-linux-v1.json 保留原观察；0金融发送、无密钥、未部署。该对照未解决真实账户 coverage unknown，实盘准入不变。

# 最新：Bulk 关键 API 证据问题整理

本轮 progress：复核官方账户历史文档明确提供 coverage 和保留下界字段，但此前实测 v3 两个窗口返回 unknown 且未给下界；未把示例或空页当完整证明。已根据真实脱敏结果准备可复现的官方技术问题，覆盖历史完整性、止损根单与子单/成交身份、开单风险一致快照与准入模型，见 planning/grid/reports/2026-10-28-bulk-api-questions.md；未向任何人发送消息，也未披露账户标识。该材料只为取得后续证据，不改变准入。无生产部署或金融发送。

# 最新：Bulk 新增边界 Linux v7 通过

本轮 progress：entropy-grid-bulk-history-linux-v7.service 已终结，exit0、67 passed、0 failures/errors/skipped、23.10s，0金融发送、未生产安装；仅测试 recovery 和 checkpoint 两文件。data/run/grid-bulk-history-linux-v7.json/xml，XML SHA db359d93f52c0503cc23590466c747a9c64989dc7616cabb5b08c426416f61ea；核对 XML 67 case，包含未知退出不重发断言和历史页上限测试，两覆盖文件摘要与 source manifest 一致。v6 的 880 是不同冻结版本的全量结果，不能将两次相加声称 947。

代码检查点 data/grid-code-checkpoints/grid-code-20261028-bulk-history-v7.tar.gz SHA 3e642840d9c39945cefc7afaf7702eeb5cb191b5fb013a19b37e5d493f9c6804，11 个候选文件均按 v7 源 manifest 核对，不含凭证；仅为候选差异包，不是完整部署包。生产未部署，真实历史覆盖、风险模型、触发子单与带仓退出、独立资金许可及真实24h仍未完成。

# 最新：Bulk 退出修复 Linux v6 通过，新增边界 v7 测试中

本轮 progress：entropy-grid-bulk-history-linux-v6.service 已终结，exit0、880 passed、0 failures/errors/skipped、321.03s、1既有 aiohttp warning，0金融发送、未生产安装；data/run/grid-bulk-history-linux-v6.json/xml，XML SHA 4a3fbcb0870fcc7bfc6bb5aa58da17cae1f92275404946e20995146d0aeaadec，核对 XML 880 case 与两个覆盖文件 source manifest 均一致。v6 包含止损变化排队重查和两项触发测试，但不含其后追加的未知退出断言和历史页上限测试。

独立 history v7 在完整校验 v6 源 manifest 后仅覆盖两个测试文件，运行 recovery 与 checkpoint 两个文件而非重复全量；entropy-grid-bulk-history-linux-v7.service 已 active/running，PID 953872，0金融发送、不安装生产。后续只查询该原 unit 与 data/run/grid-bulk-history-linux-v7.json/xml。历史真实覆盖、风险模型、触发子单与带仓退出、独立资金许可及真实 24h 未完成。

# 最新：Bulk 开单风险证据缺口梳理

本轮 progress：对照官方 riskSurfaces、Margin 和实时风险引擎文档与现有 bulk_risk_feed/bulk_budget，记录可用字段和开单容量缺口，见 planning/grid/reports/2026-10-28-bulk-risk-admission-evidence.md。公开风险面不代表账户与各市场输入的同一撮合快照，文档描述组合风险与累计挂单检查但未给出足以复现整个准入的细节；现有拒绝 BULK_RANGE_RISK_UNVERIFIED 不变，未新称找到完整模型。Linux history v6 原 unit 最近只读 active/running PID948098，约81%输出，未终结，不得称通过或重启。未部署生产、未发送金融请求。

# 最新：未完成平仓对账的页面提示

本轮 progress：网格详情页对 NEEDS_ATTENTION 且手动结束平仓处于 reconcile 的状态，明确提示可能仍有仓位或挂单、需要核对原订单和成交，避免重复提交。前端本机 node 测试 7 passed，覆盖该状态的渲染及无页面写请求；页面改动尚未上线，不证明交易所真实仓位或自动退出成功。Linux history v6 原 unit 最近只读 active/running PID948098，约49%输出，未终结，不能声称通过；本次网页修改和上一轮追加断言不在冻结候选内。无金融发送或生产部署。

# 最新：Bulk 退出重查不得重复提交未知平仓

本轮 progress：扩展带模拟持仓和 triggered 止损的离线测试，增加原平仓意图 UNKNOWN、后续恢复报告被测试替身设为成功的情况；真实退出 scheduler 仍保持 reconcile、attempt=0，原意图保持 UNKNOWN，不新增签名或发送。相关本机组合 77 passed；此为人工构造的恢复成功报告，不证明主网或真实子单最终可核验。Linux history v6 原 unit 最近只读 active/running PID 948098，尚未终结；本轮追加断言发生在 v6 冻结之后，不在其测试源内，不得由 v6 结果推断该断言通过。生产未部署，无金融发送。

# 最新：Bulk 退出对账重试 Linux v6 隔离回归进行中

本轮 progress：只读比较服务器已通过 history v5 的源，确认本机 lifecycle 相对基线仅含 Bulk stop changed 的精确重查改动，recovery 测试文件仅新增两项止损触发测试。基于 v5 完整 manifest 校验构建 history v6，仅覆盖这两个文件；entropy-grid-bulk-history-linux-v6.service 已启动并实际 active/running，PID 948098，测试环境沿用既有禁止外网及金融发送的 guard，不安装生产。尚无通过结果，只查询原 unit 与 data/run/grid-bulk-history-linux-v6.json/xml，不重复启动。原 v5 878 pass 不覆盖新修复；历史真实覆盖、风险模型、触发子单与带仓退出、独立资金许可及真实 24h 仍未完成。

# 最新：Bulk 止损状态变化后退出对账重试

本轮 progress：真实 runtime 离线模拟已有 0.1 ETH 持仓与原绑定 stop 收到 triggered，发现手动 stop_flat 的恢复返回 OPEN_ORDERS_UNAVAILABLE / BULK_BOUND_STOP_CHANGED 时原实现未排队后续对账。现仅对这一精确状态排队持久 reconcile，保留原手动滑点；执行仍要求完整对账和此前退出终态，不因错误本身允许平仓。模拟状态持续未解决时调用真实 scheduler，确认不新增签名/发送、不把持仓置零。Bulk recovery、flat finality、terminal wait、账户栅栏、退出调度及操作组合本机 77 passed。此为本机候选，未 Linux 回归或部署，不能证明真实带仓、原生子单关联或最终自动平仓；历史覆盖、风险模型和独立主网授权等仍待解决。未触碰线上金融服务。

# 最新：Bulk 止损触发事件离线恢复检查

本轮 progress：依据 Bulk 官方订单接口，triggered 是非终态，只表示条件根单触发；partiallyFilled 则是终态。官方条件单说明 stop 触发后提交 reduce-only 市价单（有指定限价时为 GTC 限价单），但未证明主网子单身份关联或终态完整性。新增真实 decoder → Bulk venue → runtime 恢复离线测试：对原已绑定 stop 根单输入 triggered，验证事件无成交、无覆盖证明，stream gap 后策略退出 RUNNING，原生状态仍为 triggered 时恢复失败且不标记 FILLED、不新增签名发送。tests/test_grid_bulk_recovery.py 本机 40 passed。测试为模拟，未证明真实带仓触发或保护退出；新增测试不在 Linux v5 的 878 项中，生产未部署、无金融发送。真实历史覆盖、风险模型、触发后子单及安全退出、独立资金授权仍是 Bulk 正式实盘前置条件。官方：https://docs.bulk.trade/api-reference/placeOrder 和 https://docs.bulk.trade/bulk-exchange/conditional-orders 。

# 最新：Bulk 历史恢复 Linux v5 隔离回归 878 项通过

本轮 progress。原 entropy-grid-bulk-history-linux-v5.service 已结束，exit0、878 passed、0 failures/errors/skipped、241.21s、1既有 warning，0金融发送、未安装生产。data/run/grid-bulk-history-linux-v5.json/xml，XML SHA 6ca08db061cd0bd57216cd2a5cd8835a11c248372bcfac1b359089d596988fb3；实际 XML 878 cases，按 v1/v2/v3/v5 覆盖顺序重建后的候选文件与最终 source manifest 摘要一致。v4 的 874 pass/1 fail 原报告保留；v5 本地回环测试延长等待，不改变生产时限，不能据此断言 v4 原失败根因。v5 覆盖进程异常退出与游标过期恢复，不含后来增加的容量上限和 funding 游标过期测试。

代码检查点 data/grid-code-checkpoints/grid-code-20261028-bulk-history-v5.tar.gz SHA b086fb48a15d94ee9cc1852bacfd2e11ef75c2104d0a75bc327a075baa245018，按最终 manifest 核对，不含凭证、候选未部署。近期两组主网只读历史窗口仍 coverage unknown，不能将测试通过作为真实覆盖证明；风险模型、持仓安全退出及独立 Bulk 资金许可、真实循环和24h/终局仍未完成。旧套利与 HL 未触碰。

# 最新：Bulk 较新窄历史窗口仍缺覆盖证明

本轮 progress：对已保存独立主网账户的较新 slot 597042200–597042203，用 2 次无签名只读请求分别查 fills/fundingHistory；均 HTTP200、0行，但 coverage=unknown、无 minAvailableSlot。证据 data/run/grid-bulk-history-window-v3.json，0金融发送。它排除了仅因为此前查询的是较旧窗口而导致历史覆盖未知的简单解释，但不能推断所有窗口或账户持仓；不绕开正式完整覆盖要求。

Linux entropy-grid-bulk-history-linux-v5.service 最近只读 active/running PID935962，约90%输出，尚未完成；只查询同一 unit 和报告。风险模型、持仓安全退出、真实覆盖、新资金授权和真实实盘验收仍未完成，无生产部署。

# 最新：Bulk 主网历史覆盖复核仍为 unknown

本轮 progress：对已保存独立账户执行 4 次无签名、无金融发送的只读主网历史窗口查询，原生 orderHistory、fills、fundingHistory 均 HTTP 200，但 page.coverage 仍为 unknown，未提供 minAvailableSlot；较新开放终点的 orderHistory 查询返回4行但同样 unknown。详情 data/run/grid-bulk-history-window-v2.json。仅证明这几个窗口当前缺少完整覆盖证据，不代表账户当前仓位/余额或其他窗口情况；不能将空页、200或4行当作完整历史，保留正式实盘的覆盖要求。

entropy-grid-bulk-history-linux-v5.service 最近只读 active/running PID935962，输出约73%；尚未结束，查询原 unit 与报告，不因等待重启或推断通过。此前新上限测试未包含在 v5 冻结源；线上生产未部署、无资金写入。完整 Bulk 上线目标继续，包括真实持仓/触发子单、风险模型、可信历史、独立小额授权及实盘终局。

# 最新：Bulk 历史分页达到上限时保持未认证，Linux v5 仍运行

本轮 progress：通过真实 Bulk market view 离线测试降低页数上限并耗尽，验证 fills 返回 incomplete/truncated、无记录、无 coverage_through、不再声称 scan_pending；再次调用只回放已缓存页，不自动继续网络扫描，通用恢复不会将 FILL_HISTORY_TRUNCATED 当可重试。相关 checkpoint/recovery/venue/funding/stream 本机组合 115 passed。该测试是上限拒绝证明，不代表超过实际 100 页/16MiB 的历史已可恢复；需要更大历史另行设计，不能静默放宽。

原 entropy-grid-bulk-history-linux-v5.service 最近只读 active/running PID935962；新增上限测试不在 v5 冻结源中。v5 未终结、不可称通过；后续读取该原 unit 与 JSON/XML，不因等待重启。无真实资金发送、生产未部署；Bulk 风险、带仓退出、真实历史及资金授权仍未完成。

# 最新：Bulk Linux v4 WebSocket 本地测试失败；v5 调整测试时限后运行中

本轮 progress。entropy-grid-bulk-history-linux-v4.service 已结束，875项中874 pass/1 fail、exit1、249.89s、1 warning，0金融发送、未安装生产，原报告 data/run/grid-bulk-history-linux-v4.json/xml，XML SHA f98b1e7c02dc6cd9d99015049abdf668de8a30b5f0a720418251cfeeceb91372。失败为 test_actual_websocket_client_rebuilds_on_disconnect_and_transmits_only_public_subscriptions 本地回环首次事件2秒超时；日志无足够证据断言根因。不得把单测通过或新轮结果覆盖 v4 原失败。

只调整该离线测试首次连接/pong/断线等待时限，保留真实本地连接、断线、重连、订阅和旧快照失效断言，生产时限和策略不变。本机相关 WebSocket、游标过期和 checkpoint 27 passed。v5 从已通过 v3 冻结源和 manifest 构建，精准覆盖4项：测试等待调整、两项历史测试文件与仅 Bulk cursor-expired 变化的隔离 stream_recovery；不混入本机其他 HL 改动。entropy-grid-bulk-history-linux-v5.service 启动 exit0，最近 active/running PID935962；外网/金融发送禁止，生产未安装，结果尚未知；查询同一 unit 与 grid-bulk-history-linux-v5.json/xml。Bulk 完整历史、风险、带仓退出及新资金授权等尚未满足。

# 最新：Bulk 资金费游标过期重扫本机通过；Linux v4 已出现失败但尚未结束

本轮 progress：用真实 FundingSynchronizer 和 Bulk 市场视图离线构造 10 页后 cursor expired，确认失败时资金费覆盖游标不推进、恢复调度允许原起点重扫、最后完整覆盖才推进 cursor；checkpoint/recovery/venue/funding/stream 本机组合 114 passed。该测试及之前游标过期调度修复不在 Linux v4 冻结源中。

原 entropy-grid-bulk-history-linux-v4.service 最近查询 active/running PID929678，输出已出现 F；进程尚未结束，失败用例、原因及总数未知，不得声称通过、重启或提前归因。后续查询同一 unit 和 grid-bulk-history-linux-v4.json/xml，保留原失败报告，再据实修复。无金融发送/生产部署，Bulk 实盘条件未完成。

# 最新：Bulk 历史游标过期后可在原起点重新恢复

本轮 progress：发现 Bulk opaque cursor 过期虽然清除分页暂存，但恢复调度此前将错误视为不可重试；现对 fills/funding 的精确 BULK_CURSOR_EXPIRED 标记允许原历史起点的有界退避重试，不能将过期游标当覆盖，其他错误/仓位 drift 不因此放行。真实 RecoveryBarrier 离线测试构造10页后游标过期，确认重新从原 slot100 读取，最终完成对账且无新增金融发送；checkpoint/recovery/venue/funding/stream 组合 113 passed。本机新增此代码和测试不包含在当前 Linux v4 冻结源。

entropy-grid-bulk-history-linux-v4.service 最近只读 active/running PID929678，尚未结束；下一轮查询原 unit/报告，不得用启动/进度当通过或重启。新补丁需独立 Linux 回归，普通实盘未开放，真实历史覆盖、持仓安全退出、风险及资金授权仍未完成。

# 最新：Bulk 历史恢复 Linux v3 873 项通过；异常退出测试 v4 运行中

本轮 progress。entropy-grid-bulk-history-linux-v3.service 已结束，exit0、873 passed、0 failures/errors/skipped、211.34s、1 条既有 warning，0金融发送、未安装生产。原报告 data/run/grid-bulk-history-linux-v3.json/xml，XML SHA 99361b391f92af2410f9d686dd5005c3b1b952b6619819d90e71235b5986f3b7，XML 873 cases 已核对；覆盖 FundingSynchronizer 提交失败、正常重启续读及真实 RecoveryBarrier 分批测试，未覆盖之后新增的两个进程异常退出测试。

v4 从 v3 冻结源经 manifest 验证构建，仅覆盖 checkpoint 测试文件，entropy-grid-bulk-history-linux-v4.service 启动 exit0，最近 active/running PID929678；外网金融发送禁用，未安装生产，原 unit 与 data/run/grid-bulk-history-linux-v4.json/xml 为后续唯一结果依据。不能因等待超时重启。Bulk 原生风险模型、真实主网完整历史、持仓触发子单和安全退出、资金授权、24h/终局仍未完成。

# 最新：Bulk 页暂存进程异常退出离线验证；Linux v3 原任务仍运行

本轮 progress：新增独立子进程在页提交后 os._exit 与 SQLite 更新事务中 SIGKILL 两种离线测试。重开数据库后前者保留已提交原游标，后者保留前一已提交游标；后者修正杀进程信号与测试线程竞争后连续5次通过。初版测试曾出现一次失败：writer 发出 SIGKILL 后可能仍先返回并提交，不能断言 kill 时一定处于未提交事务；修正后阻塞 writer 防止其返回，不能把初版失败直接解释为数据库事务缺陷。相关 checkpoint/recovery/venue/funding/stream 本机组合 111 passed。

原 entropy-grid-bulk-history-linux-v3.service 最近查询 active/running PID925628，输出约82%，未结束；以上新增子进程测试不在其冻结测试源中，v3 结果不能证明它们。不得重新启动原任务；后续读取实际 JSON/XML 后，再单独验证新增测试及 Linux 环境。未真实下单、未部署生产，Bulk 实盘风险、带仓故障、真实完整历史与资金授权仍未完成。

# 最新：Bulk 历史恢复 Linux v2 870 项通过，v3 含新增端到端测试运行中

本轮 progress。原 entropy-grid-bulk-history-linux-v2.service 已结束，exit0、870 passed、0 failures/errors/skipped、219.61s、1 既有 warning，无金融发送、未生产安装；data/run/grid-bulk-history-linux-v2.json/xml，XML SHA 537a7915e854b02b4bd09989542d60da06b48ab2083cebcd4b73306aa96fc016，实际 XML 870 cases。它验证坏页清理和瞬断保留，但不含随后增加的资金费事务失败、正常重启续读与真实 RecoveryBarrier 分批测试。

新 v3 从已验证 v2 冻结源和 manifest 构建，仅覆盖两个测试文件，entropy-grid-bulk-history-linux-v3.service 启动 exit0，最近 active/running PID925628；外网/金融发送禁止，生产未安装。查询该原 unit 与 grid-bulk-history-linux-v3.json/xml，测试未结束不称通过、不因等待重启。真实账户历史覆盖、进程强杀、带仓/子单、完整风控、主网授权及 24h 尚未完成；Bulk 未正式上线。

# 最新：Bulk 资金费分页跨运行时重启续读通过本机测试

本轮为 progress：真实 FundingSynchronizer + Bulk 市场视图离线测试中，11页窗口扫描前10页后关闭并重开 SQLite/同步器，持久暂存以原游标 C10 继续，完整覆盖后才提交 cursor；相关本机组合 108 passed。此为正常关闭/重新打开的重启模拟，不等同进程强杀、真实交易所覆盖或带仓故障。Linux v2 原 unit 最近查询仍 active/running、PID921038、输出约57%；新增测试不在其冻结候选，不能用 v2 结果证明本次测试。未部署生产、未发送金融请求，Bulk 实盘风险门与授权要求仍保留。

# 最新：Bulk 资金费提交失败后完整重扫的事务测试通过；Linux v2 仍运行

本轮为 progress：新增真实 FundingSynchronizer + Bulk market view 离线故障注入，在完整历史读完后用 SQLite trigger 使 funding_payments 提交失败；确认事务回滚、覆盖 cursor 未推进，移除故障后重新读取原窗口并仅入账一次。checkpoint/recovery/venue/funding/stream 本机组合 107 passed。该测试证明提交事务失败后原窗口可安全重扫；不证明进程强杀、实际交易所页覆盖或真实主网资金费。

原 entropy-grid-bulk-history-linux-v2.service 最近只读 active/running PID 921038，输出约49%，尚未结束；本轮新增测试不在已冻结 v2 内，不能用其结果证明新测试。查询同一 unit；不得因等待超时重启。候选未生产部署，无金融发送，Bulk 风险/新资金许可及其他实盘条件仍未完成。

# 最新：Bulk 历史分页 Linux v1 868 项通过；坏页补丁 v2 正在回归

本轮为 progress：原 entropy-grid-bulk-history-linux-v1.service 已终结，test_exit 0、868 passed、0 failures/errors/skipped、196.80s、1 既有 warning；0金融发送、未安装生产。data/run/grid-bulk-history-linux-v1.json/xml，XML SHA 8bbc4d2e2d99fbaa99f97dce649ac63847d1fdeeb9ca453889af12a61cd52417；XML 868 case 和 8 项 candidate 源与 manifest 均已核对。该 v1 不含本机后续坏页清理/瞬断保留补丁及两个测试。

已从原 v1 逐项 manifest 验证的冻结源构建 v2，只覆盖 bulk_read.py 与 test_grid_bulk_history_checkpoint.py；原 entropy-grid-bulk-history-linux-v2.service 启动 exit0，最近只读 active/running PID 921038，外网/金融发送禁止、未安装生产。结果未知，后续查询原 unit 和 data/run/grid-bulk-history-linux-v2.json/xml，不能因观察超时重新运行。104/106本机组合、v1 868 各自不同版本不相加。目标仍为完整 Bulk 正式上线；真实持仓、触发子单与完整历史、风险模型及新 Bulk 资金授权尚未完成。

# 最新：Bulk 分页续读遇坏页清理、瞬断保留；Linux v1 原任务仍运行

本轮为 progress。修复 history_checkpointed：确定的原生解析/覆盖错误、游标失效或快照错误丢弃当前扫描代，避免坏页在每次 replay 中永久阻塞；瞬时网络/存储失败、checkpoint 竞争或容量上限保留已有页面，不把失败认定为完整覆盖。新增坏字段后可重新扫描、网络瞬断保留原页等离线测试；与 recovery/venue/funding/stream 组合实际 106 passed。

Linux entropy-grid-bulk-history-linux-v1.service 原 unit 最近查询 active/running PID 917264、约 66% 输出，测试尚未终结；该冻结候选不包含本轮坏页处理补丁和新增两项测试。不得按进度推断通过或重启原任务；终结后保存真实报告，再基于验证的源构建新候选验证本轮改动。所有代码未安装生产，无金融发送；风险模型、止损触发子单、历史真实覆盖和新 Bulk 资金许可等仍是实盘上线前置条件。

# 最新：Bulk 历史恢复 Linux 独立回归已启动

本轮为 progress：核对发现本机 recovery.py 与 stream_recovery.py 混有另一组 HL 修改；独立测试候选继承 Bulk v6 已验证源，仅移植两处明确 Bulk 分页 pending 判断，未混入该 HL 修改。新增 checkpoint、reader、market view、资金费和测试等总计 8 项覆盖。`entropy-grid-bulk-history-linux-v1.service` 启动 exit0，最近只读为 active/running、PID 917264，禁止网络和金融发送、不安装生产，继续查询同一 unit 及 data/run/grid-bulk-history-linux-v1.json/xml；结果尚未知，不能以启动成功代替通过。继承 v6 源 manifest 核对并保留 851 原报告；本机相关 104 pass 只证明本机组合。HL/旧套利未部署或重启。

待实际测试终结后核对 XML/manifest 并修复任何失败；完整账本崩溃与并发末页、真实历史覆盖、止损子单安全退出、风险模型及新的 Bulk 资金许可仍未完成，不将该回归视为普通实盘合格。

# 最新：Bulk 持久分页候选已接入本机恢复和资金费同步

本轮为 progress：本机 Bulk market view fills/funding_history 使用 checkpointed_history，超过每轮页预算且仍有空间时标记 scan_pending；恢复和资金费同步仅对明确 pending 发起后续重试，不把普通 incomplete/unknown 当可恢复证明。部分窗口不返回账本记录、不推进覆盖；完成页必须经过原 reader 全量 replay。真实 FundingSynchronizer 离线测试验证 11 页第一次不推进 cursor、第二次完整才提交；相关 recovery/venue/funding/stream/checkpoint 组合 104 passed。100 页/16 MiB 达到上限不自动假装继续，继续 fail closed。

这些改动只在本机候选，未完成 Linux 隔离全回归、真实主网 history coverage、跨重启 ledger 应用/并发末页竞态的完整端到端验证，也未部署线上。原 v6 的 851 pass 不覆盖新增分页改动。Bulk 普通实盘和资金授权仍未开放；HL 与旧套利未触碰。

# 最新：Bulk 市场视图新增实验性持久分页，继续等待恢复流程接入

本轮为 progress。BulkGridVenue 新增显式 checkpointed_history 实验接口，使用实际账户 journal 的 SQLite 和 market/account scope；不改变现有 fills/funding 正式调用，不向账本写入。离线新增两实例争抢同页（仅一个 version 成功）、缓存内容顺序受损时 replay 拒绝且不读后续、实际 Bulk market view 两轮续读测试。checkpoint/reader/bound reader 组合 89 passed；没有金融发送或生产部署。

注意这是实验接口，完成扫描只代表 reader 的历史覆盖证明，不是 ledger 落盘或普通实盘资格。故障时重试调度、完整 ledger 幂等与崩溃边界、长窗口容量与官方真实历史覆盖、Linux 独立回归未完成。正式恢复未调用此接口，旧资金授权已消耗，Bulk 风险门维持关闭。前述 86、67 等旧结果是不同版本，不能相加。

# 最新：Bulk 历史页跨重启回放原型已通过离线测试，尚未接生产恢复

BulkEvidenceReader 增加独立 history_checkpointed 实验路径：限定账户/owner/市场/类型/窗口，回放全部暂存页通过原始历史解析与覆盖检查，续读原 opaque cursor；游标过期或快照改变时丢弃该代暂存并报错，unknown/partial 仍不认证。完成或分页终止会清掉暂存；若账本消费之前崩溃，下一次需重扫，不能称账本原子提交。现有正式 fills/funding_history 未接入新路径，线上未部署，Bulk 风险和主网授权不变。

离线 checkpoint、原 Bulk reader 和 bound reader 合计 86 passed，涵盖分批续读/重启、快照改变、游标过期和不完整覆盖；尚需覆盖并发竞争、超上限、分页状态损坏、真实主网覆盖、runtime retry/账本幂等、Linux隔离回归。注意若调用者因 FILL_HISTORY_TRUNCATED 而不重试，单次续读机制不会自行推进，接入前必须设计有界调度与资金费同步行为。

# 最新：Bulk 历史暂存加入快照边界与容量限制，尚未接入恢复

本轮将 BulkHistoryCheckpoint 暂存原型扩展为按原 start/end/as-of/floor 固定快照，拒绝变化、异常分页和循环游标，并限制每页 5000 条、每扫描 100 页/16 MiB；限额或冲突时事务不推进。checkpoint 和现有 Bulk 基础测试组合实际 67 passed。本功能仍仅离线原型：未绑定运行时 reader、未对所有内容回放验证、未原子提交账本，也未证明真实主网覆盖；finished 不代表覆盖完整。未发送交易、未部署线上。接下来实现持久分页与 reader 的集成、断线/过期游标和并发恢复测试，保留实盘风险门。

# 最新：Bulk 历史分页持久暂存原型，尚未接入恢复

本轮新增 entropy_arb/grid/adapters/bulk_history_checkpoint.py 和 tests/test_grid_bulk_history_checkpoint.py：按账户、owner、市场、类型、slot 窗口分隔，支持 SQLite 持久原始页、generation/version 比较写入、游标循环拒绝和跨重启续读；2 项离线原型测试通过。此组件只暂存原始页，finished 表示分页结束而不表示覆盖已验证；尚未绑定 BulkEvidenceReader / runtime、未证明快照边界与覆盖、未限制数据体积或处理竞争重启和过期游标，不可用于实盘、不推进账本，也未部署服务器。下一步需增加容量限制、对每页验证与快照绑定、完整扫描 replay、完整覆盖后原子账本提交，以及故障/并发及 Linux 回归。目标仍为完整 Bulk 实盘上线，风险门和旧资金授权约束不变。

# 最新：Bulk 历史回补发现超过页数预算后的活性限制

本轮检查 bulk_read.history 和已有覆盖/分页测试，确认默认 500 条/页、10 页上限导致超出预算时持续从同一起点重扫，无法自然走到窗口末尾；现有行为仍安全地报告 incomplete，不推进账本覆盖。持久分页扫描必须绑定原账户/部署/市场/历史类型/窗口/快照/游标，完整证明后再与账本游标原子提交；不能把当前不完整记录当正式历史。具体审计与实现约束见 planning/grid/reports/2026-10-28-bulk-history-recovery-audit.md。没有金融发送、线上部署或服务变更；完整 Bulk 目标仍未完成。

# 最新：复核 Bulk 官方风险说明，继续保持实盘准入关闭

本轮对公开官方 Margin、Real-Time Risk Engine 和官方 SDK 历史分页文档进行只读核对，记录在 planning/grid/reports/2026-10-28-bulk-official-margin-review.md。官方对订单校验确认涉及可用权益、包括挂单的累计杠杆与风险降低交易，且不是简单初始保证金公式；维持保证金和清算风险的组合公式不能单独推导完整开仓准入。尚缺实际边界、全部挂单组合与主网参数，BULK_RANGE_RISK_UNVERIFIED 保持关闭。SDK 文档覆盖信号与当前 fail-closed reader 方向一致，历史空页/unknown 仍不能证明覆盖。没有金融发送、部署或服务变更；该文档核对不代表 Bulk 实盘验收完成。

# 最新：Bulk v6 Linux 隔离回归 851 项通过，普通实盘尚未开放

上一轮为 progress。实际原测试 entropy-grid-bulk-parked-linux-v6.service 已结束，test_exit 0、851 passed、0 failed/errors/skipped、176.42s，1 条既有 aiohttp warning；0 金融发送、未安装生产。data/run/grid-bulk-parked-linux-v6.json/xml，XML SHA 08eab53f8f200be1fefb2d26620435efd8bc05033dd5c61914215c9cfd2b6b02；实际 XML 851 case，候选 13 项源码/测试与本机、Linux manifest 摘要全部一致。

v6 额外核对 claim 时核心 intent 的完整请求 hash、账户和市场、当前 epoch/状态及未决止损；内部构造与核心账本减仓/数量/价格/方向冲突的请求已离线复现并拒绝，暂停交错亦拒绝。claim 后最终网络门仍须独立检查，不能称交易所 I/O 与本地状态原子。v5 为此前版本 848 passed，v4 837 pass/8 fail 的原失败记录保留，各报告不能相加。纯源码检查点 data/grid-code-checkpoints/grid-code-20261028-bulk-parked-v6.tar.gz，SHA 81450e689e7eb859d13abf3ba7d7e01aee6458c5f895e523a3c6a0bd24f72af1，13 文件无凭证。

Bulk 范围风险、原生止损触发子单、持仓故障及安全减仓退出、完整成交/资金费覆盖、共享限流、真实网格循环、24 小时及终局独立审计仍未证明。旧 Bulk 单次资金授权已消耗，不能开始新实盘资金动作；继续独立开发和只读证据收集，必要时在准备好可审查的测试后再获得明确授权。旧套利、线上 HL 和 Bulk 生产未因本候选部署或重启。本目标未完成，不标 complete。

# 最新：Bulk 未发送终态、保护退出并发已修复候选；Linux v5 原测试进行中

本轮为 progress，完整 Bulk 上线目标仍未完成，未新增 Bulk 真实资金许可，未部署/重启生产或发送金融请求。

- 已复现固定止损在发送前拒绝后被误当待核验终态，永久挡住同账户其他市场。候选在同一 SQLite 事务核对核心 intent、原生账户/网络/请求 hash、原始发送状态、回执与成交，再写原身份不可重用的 ABORTED_NOT_SENT tombstone 和 REJECTED；任何不一致仍保持未知或账户栅栏。原 CID/nonce/订单身份不可重投。重启、竞争重新 claim、迟到终态/成交、伪造或冲突、附加与 claim 交错、事务回滚均有离线案例。
- 发现旧 pre-I/O wait 判断和核心状态回退之间存在竞态，候选用同事务复核并回退 PLANNED，原生 claim 要求真实网格核心记录处于 DISPATCHING；证明不足进入 UNKNOWN。固定止损因短暂等待保留原计划时重用原身份，不创建第二笔。账户级未决固定止损也包括 DISPATCHING/UNKNOWN；普通单 claim 时同事务复查账户栅栏，签名前也沿用原 dispatch gate。交易所 I/O 与本地检查并非跨系统原子，真实带仓安全退出未证明。
- 手动退出遇 PROTECTION_CUMULATIVE_UNVERIFIED 会持久进入原有对账队列；对账未通过不能发送减仓，重启保留队列。退出重试沿用原滑点（缺失的历史手动记录阻断），generation/version 防止旧任务覆盖新记录；scheduler 的操作登记事务和 bump 事务均核对原 epoch，已有进行中操作不被旧任务中断。依旧不可把对账等待或排队当作平仓成功；Bulk 原生止损触发子单、并发 reduce-only 与完整成交覆盖尚无证明，实际未知终态的完整退出仍阻塞，范围风险依然拒绝。
- 本机 628 项 Bulk 测试通过、1 warning（GRID_REAL_DISPATCH=0，隔离模拟）；此前 292 项本机组合通过但不含之后新增两项，应分别记录、不相加。Linux v4 原报告 exit1，845 项中 837 pass/8 fail，0 error/skip、186.14s；8 项均为新 core 必须存在后测试夹具未构造核心 intent，实际报告 grid-bulk-parked-linux-v4.json/xml，XML SHA 95dea1f2ab512c2ed4a3ea64e7b72601195cba9ef4ea9f2c34c00fc2384b910f。已在受影响两个测试文件补真实 core 夹具，单组35项、本机全 Bulk 628 项通过，不能覆盖 v4 原失败。
- Linux v5 从原 v3 817 项通过的冻结源和 manifest 构建、只覆盖 13 项本轮明确文件；禁止外网金融发送、不安装生产。systemd entropy-grid-bulk-parked-linux-v5.service 启动请求 exit0，最近实际 active/running PID 349784；尚未完成，只查询原 unit 和 data/run/grid-bulk-parked-linux-v5.json/xml，不因等待重启。代码检查点 data/grid-code-checkpoints/grid-code-20261028-bulk-parked-v5.tar.gz，SHA 62d6663850c6d6a419335b68ff0aef0739ca958c4bde28a304ba510dc22247da，13源码/测试，不含凭证；该检查点创建时测试尚未完成，不能当 Linux 通过证据。
- 下一步确认 v5 实际报告及失败明细，若通过再核对源码/测试覆盖；继续完整风险模型、触发子单与保护退出并发证据、共享限流、带仓故障和真实网格循环，按原计划单独获得 Bulk 小额主网授权后方可做新交易。HL v21 原 25h 服务按既有授权独立运行，不重启、不重复启动。

# 最新：HL v21 已在运行，不重复开启；Bulk v3 隔离测试通过

用户再次提出“开启新一轮”。2026-10-28 06:37 北京时间只读查询线上原 v21：g-e3debcab696d41ba 为 RUNNING/ACTIVE/e5，原启动已提交且 SQL 成功；当前监督和独立终局服务 active，到期 timer 等待，截止 10 月 28 日 23:38:46 北京时间。观察从 10 月 27 日 22:44:49 开始，至查询时不足 24 小时；observer 报告曾两次恢复，耗时约 121 秒和 120.9 秒，均记录对账成功，严格连续观察已失效，受保护恢复观察及独立终审均未完成。最新样本是引擎 lease 证据，不冒称实时交易所独立查询。账户上已有在途策略，未重复下单、停止、修改或重启；现轮保持既有期限和风控。状态文件 data/run/grid-v21-current-status.json 和 grid-ordinary-live-linux-v21.json。

Bulk 隔离 v3 原 unit 已正常结束：817 passed、0 failed/errors/skipped、174.98 秒，1 既有 warning，test_exit 0，0 金融发送，未安装生产，XML SHA 9fdcf50420eb76ed1dcd194671f585c6d620e5fe7d45b71b680309d802f44f73。报告 data/run/grid-bulk-parked-linux-v3.json/xml。该结果不表示 Bulk 正式实盘就绪：真实范围风险、完整安全退出、持仓故障和资金授权等仍未满足。此前 v2、v1 结果分别保留，不相加。

# 最新：Bulk Linux v2 809 项通过；终局迟到隔离并入 v3 正在回归

上一目标轮为 progress。本轮新证据：bulk-parked-recovery-v2 原Linux报告 exit0/809 pass/0 fail/error/skip/161.52s、1既有warning，XML SHA 55e381dfebb5beb8760f3d81f2e7532641d465e07ef67ed3e832aa11001c95d8。原v1 767/1失败不被单项重试覆盖，不能相加。v2不包含随后检查出的 stop_keep 零仓终局旁路及已关闭策略迟到终态的账户级栅栏，不得拿它证明这些新改动已验证。

新增停留仓位结束零仓路径的同事务终局检查：固定止损未核验时 partial，保留市场claim/预算；非零仓保留原保护。已关闭策略迟到未核验终态时，同账户所有市场的新增风险与新/扩大预算预留被拦截，减少预算和reduce-only仍有其他原有安全检查；不能凭单点发送门测试声称完整安全退出可用。原 stop_flat 未核验时仍可能阻断真实减仓，完整修复依赖真实持仓、全部挂单/成交覆盖与原请求终态证明。最新本机整合17组247 pass，模拟证据不是实盘验证。

已创建独立Linux v3回归 unit entropy-grid-bulk-parked-linux-v3.service，启动请求exit0，源使用v2经过809项测试的冻结manifest加23项精准源码/测试覆盖，禁止外网和金融发送，CPU限50%；先查询本unit、其报告和XML再判定结果，不因为观察超时重启。Bulk生产未安装，资金权限未扩展，HL/旧套利未重启。

本地官方证据尚不足开仓准入：getAccount 的 availableMargin=totalMargin−marginUsed，且 marginUsed 为维持保证金；不能作为初始开仓容量。原始风险样本BTC/ETH首notional节点50000，现有信息无法推导节点以下曲线；liquidator 的 apply_reserve、price_to_sweep 会影响公式但当前主网配置未获证明。引用本地 data/grid-sdk-sources/bulk-audit-20261025/getAccount.txt、openapi.yaml 及官方 bulk-client risk/liquidator 源，不把SDK默认或维持保证金公式当开仓资格。BULK_RANGE_RISK_UNVERIFIED仍生效。

# 最新：Bulk 终态保护、看护隔离与账户校验已整合；Linux v2 正在隔离回归

本轮取得实际进展，目标继续为完整 Bulk 实盘上线。v1 Linux 实测 767 pass/1 fail，2秒 WebSocket 回环超时，单项后续重跑 pass，但 v1 总回归不通过；原报告保留。新增终态义务同时保存订单终态与成交未核验标记，历史终态回补扫描、原身份/数量/终态一致的核验证据、唯一代次防止并发误删，恢复状态提交与检查同事务，最后开仓发送门再次检查。模拟覆盖重启、恢复提交竞争及发送前后原身份；MAY_HAVE_SENT 不得重发。保护看护启动/运行时逐网格隔离异常；平仓最终检查、CLOSED、市场claim释放和预算归零在同事务，义务未核验时返回partial并保留预算。Bulk账户预览按原生快照和杠杆上限核验，真实范围风险模型仍拒绝。

当前本机整合相关191 pass（13组，见本轮运行），原生终态义务与完整安全退出仍需真实协议证明；受保护持仓遇未知终态时 stop_flat 的恢复屏障仍可能阻塞减仓，不能称已完成实际平仓。历史覆盖、触发子执行、资本风险模型、共享优先限流、小残仓、带仓故障、真实网格循环、24h及终局审计均未完成。Bulk新资金权限尚无，禁止真实下单。

Linux冻结 v2 候选 `/root/code/entropy-grid/data/staging/bulk-parked-recovery-v2/source` 在已失败v1隔离源码和精确本轮改动上构建；测试 unit entropy-grid-bulk-parked-linux-v2.service，原实际 PID 339881、CPU限50%、无交易资金/外网、生产未安装；只查该原unit及 grid-bulk-parked-linux-v2.json/xml，不因为等待超时重启。候选从服务器v1 source manifest逐项核验，未夹带本机另一组HL恢复变化。启动时仅已证实unit active，结果未知。原报告 v1及单项重试保留不改、不相加。HL v21及旧套利依原运行版本与流程，不在本轮重启。

# 最新：Bulk 固定止损终态漏对账已复现并修复；整合候选仍在测试

上一目标轮取得实质进展。本轮继续遵循已授权范围：Bulk 一次性主网测试许可已消耗，无新资金许可；本地研发不发送金融请求，不部署、不重启HL/Bulk/旧套利。HL v21最近只读为 RUNNING/ACTIVE/e1，保护及截止计时依原控制器执行，不能把未满的观察当24h。

Linux 原 bulk-parked-recovery-v1 已终结而非仍运行：768项中767 pass、1失败（公开行情 WebSocket 本地回环测试2秒超时），exit1、未超时、无金融发送，XML SHA 436fd5d10088dfdb8e368f81d7ba4f6ed7c585e4d30f70ace29fcf8148c1a4c6。原相同源码失败用例独立诊断1次pass（3.77秒），但不能将其推断为完整Linux回归通过，也不能确定限CPU是根因。冻结来源 grid-bulk-parked-linux-v1.json/xml；诊断 grid-bulk-parked-failed-test-v1.json。下一候选v2本机已组装但还未跑，不覆盖v1、不得声称通过。

离线已真实复现 fixed_quantity 止损 terminal=CANCELED 但累计成交未知，第一次恢复报 gap 并持久为 CANCELED，第二次旧代码漏掉已终态订单而误通过。新机制在 mark_terminal 同事务保留 terminal 和带唯一代次的未核验义务，兼顾 UNKNOWN/cancel 路径；RecoveryBarrier 在完整成交覆盖前提下核验原订单、已入账累计、终态及绑定；对历史无marker终态也扫描，成功记录核验指纹，比较marker代次避免并发误删。runtime 最终恢复状态事务再次检查，executor 最终开仓门再次拦截 pending；减仓门本身不拦，但完整stop_flat仍依赖恢复，未知义务会阻止减仓，不能宣称已解决真实退出。回归覆盖连续恢复、真实离线SQLite重启、已取消终态未知、原ID/数量/状态冲突、并发标记、marker ABA及最终开仓门；联合本机148 pass。交易所执行结果、子单归属与历史真实证据仍未证明。

额外隔离改动：保护看护对单市场 backend/clock 故障隔离并有持仓时持久请求退出，其他策略继续；固定止损冻结时 proof 异常保存点回滚而不回滚网格冻结。新增 Bulk preview_account_setup 按原生账户/市场上限及快照证明，非 HL 杠杆假设；独立假风险模型测试，真正 BULK_RANGE_RISK_UNVERIFIED 不变。全局资金容量、共享预算、IP 限流、完整历史/子单、带仓故障、小残仓、真实网格循环和24小时验收均未完成。并行复审正在处理 known-not-sent 需求保留、启动时保护看护隔离，以及 closeout 对终态未知的安全处理；结论与本机改动需重新验证后才能组成新Linux候选。

# 最新：HL v21 已运行并经原生核验；Bulk 固定止损恢复改动待 Linux 回归

用户“开启新一轮”授权 HL 独立账户同限额新轮。RID ordinary-observe-linux-20261027-v21，GID g-e3debcab696d41ba，策略 https://taoli.coinfetcher.xyz/grid/#/grid/g-e3debcab696d41ba 。原 start operation e0d1dda3-787b-499b-b495-dfcb566c331a 已提交，最新独立核验 RUNNING/ACTIVE/e1，原 start SQL succeeded，实持 0.0156 ETH，4 笔普通挂单加 1 笔原生止损已确认，未知意图0，敞口名义48.46192 USDC；不得重复提交原请求。独立到期 timer active，2026-10-28 23:38:46.004 北京时间；观察起点2026-10-27 22:44:49.325，最早24小时2026-10-28 22:44:49.325，尚未通过24小时。控制器及独立终审进程 active，cutover exit0；启动证明 grid-v21-final-start-review.json。40 USDC 含 20 reserve，ETH 多、cross3、4 等差、2277.9–2569.0，止损 2259。上一轮重建前预览失败异常未记录具体类型，根因未确认也未修复；当前沿用通过 1688 项完整 Linux 测试且源码 66 项未变的执行版本。新监督174项及流程52项通过；启动时交易所仓位/挂单/保护已独立核验，24小时仍待实际观察。启动前 11 DEX 两次零仓零单、备份完整，旧套利与 Bulk 进程维持原样；未新增 Bulk 资金授权。

Bulk 本机固定止损恢复补丁尚未部署：只有原条件簿和原编号查询双重证明未触发且成交为零，才允许 unknown cumulative 的 parked 固定保护进入共同恢复；完整成交、资金费、仓位及私有流屏障仍必须通过。真实带仓恢复/触发子执行尚未证明，服务范围模型仍阻止完整 rebuild。当前本机组合 JUnit 774 pass/0 fail/error/skip（该报告在后续故障隔离补丁前；新补丁相关40项本机通过），data/run/grid-bulk-parked-recovery-local-v1.xml SHA a17e03f8df0a234cc3b10d3836223286923f29703030b4babcf13dec461a9393；不与此前742相加、不当 Linux 测试。新增演示样本来自已记录的历史原生快照，并非本轮主网持仓证明。审查发现保护 backend/时钟失败可回滚同账户冻结，已用事务 savepoint 防止保护证明异常撤销冻结，3类故障注入及相关组合40项本机通过；独立 Linux 冻结候选 bulk-parked-recovery-v1 运行中，systemd entropy-grid-bulk-parked-linux-v1.service；限制CPU且禁止网络，未知结果先查原任务不重启。 Linux 候选使用v6底板且只移植本次恢复/栅栏差异，避免夹带本机独立的HL恢复改动；冻结测试后本机追加了部分写入再抛错的回滚断言（40项通过），该新增断言不包含在正在运行的v1候选中，需另测后才可宣称该断言通过Linux。当前预览还硬读 HL leverage，Bulk 风险模型有效后会 KeyError；待以原生证据接口解决，不能伪造账户设置或开放风险门。

# 最新：Bulk 固定数量保护后端与独立看护已完成隔离验收；Linux742项通过，普通实盘仍未开放

本轮是在原完整Bulk目标上继续progress，未标complete/blocked。新增bulk_protection_manager.py，fixed_quantity元数据已接通OrderJournal/BulkWireRules/BulkExecution/ProtectionManager及暂停保留；最终发送重新核验Native容量、原请求与共享intent、私有流/公共risk/引擎权限/独立看护，5秒证明同时受墙钟和单调时钟约束。新止损确认后才撤旧止损，未知原单/原撤单不重发。native-protection-lease独立30秒、含进程会话/epoch/配置/凭证/数量；过期、看护丢失或明确止损危险本地持久请求退出，不把排队当已清仓。短暂查询失败及可受保护的对账等待不续期旧证明，有限跨恢复epoch不延长时间；不复用重启前证明，账户适配器/凭证尚未加载也不能漏过旧固定保护。

最终候选 `/root/code/entropy-grid/data/staging/bulk-fixed-manager-v6/source`，实际Linux742pass/0fail/error/skip/155.55s（1既有aiohttp warning），grid-bulk-fixed-manager-linux-v6.json/xml，XML SHA 9e539e04feeb1d75401bae72f6c4b9045f1938ddc7259354134aa10d737029a7。原测试进程137263已正常结束，本机handle39667 exit0，不重启此轮测试。14项本机变更源码/测试SHA与最终Linuxmanifest一致；SDK3项与已签名36组的v2证据一致。此前v5为740、v4为738、v3为735、v2为717，各自冻结结果保留且不相加。本机最终相关组合69pass；739/743等广回归为之前冻结状态，不能与最终742混算。Linux沿用已冻结的共用engine测试，本机当前另有3项新增案例，两者范围不同；报告仅证明所列Bulk与共用保护测试，不冒称全仓库检查。v1旧stop-only测试仍假设通用通道永远拒绝stop导致1失败，功能扩展后改测试保留有限探针更窄白名单；SDK v1占位符替换语法错误0cases，不算通过。

SDK v2真实Linux0.1.30临时未入金key36组实际签名/双官方Rust oracle/发送格式+每组6项篡改拒绝，socket.connect禁止，0金融发送，无用户key访问。grid-bulk-fixed-manager-sdk-v2.json，三项SDK相关SHA仍与最终本机相同。最终相关组合本机69pass，另有最新实际Linux742作为最终组合证据。生产Bulk未安装/重启，GRID0，旧一次<2USD金融许可已消费，本轮不含新资金授权。没有Mac资金writer/AI定时。

最终只读隔离 grid-bulk-fixed-manager-isolation-v6.json：HL66源码未变，runtime3835010/watchdog3835012、Bulk worker3802260/web3222058、旧套利1223049均原PIDactive，旧套利cwd/root/code/entropy；Bulk普通grids/operations/order_intents=0。当前HL原g-6e72b21910b84934已CLOSED/STOPPED/e18，不再是下文启动时e1或隔离v4的e13。只读追查原STATE/SQL/原独立proof：七次受保护恢复成功，第八次重建UNCLASSIFIED_REBUILD_ERROR；原观察SUPERVISION_GRID_STOPPED，不足24h，ordinary/protected均false。原cleanup582b36ab-2ea6-4739-9194-cfdfc0154aab在SQLsucceeded/remaining0（STATE.cleanup.running仅初始回执）；原native-audit+backup均exit0。grid-ordinary-live-linux-v20-proof.json在独立查询时11DEX零仓零单、原stop reduceOnlyCanceled、两成交匹配，净-0.358265USDC，备份integrityok。当前SQL所有本轮意图终态、库存0。没有新HL金融动作，不重放原cleanup/不自动开新轮；重建错误根因尚未定位，不当成一次502或24h成功。

已保存code-only检查点本机及server：data/grid-code-checkpoints/grid-code-20261027-bulk-fixed-protection-v6.tar.gz，SHA baedbc7e7e70a611f817bff0fc66942e87bdf849d4beddb8c77326f7b78b6c6b，父5f3bec3303e807ac1f7ba359e2803d7152823fe88095a610cece5ac2a0f93831。18项源码/测试/说明，不含凭证；metadata grid-bulk-fixed-protection-checkpoint-v6.json。报告planning/grid/reports/2026-10-27-bulk-fixed-protection.md；HL收尾附带证据grid-hl-v20-close-observed-during-bulk-v6.json、close-summary及原proof/finalization，未写原金融报告。

文档BULK-FIXED-PROTECTION.zh-CN.md、完整BULK-LIVE-GOAL.zh-CN.md矩阵已更新。完整初始/范围风险、IP共享优先限流、完整历史/触发子执行（recovery.py当前unknown-cumulative分支还只认可whole_position，须对固定parked stop及触发执行补可靠语义，禁止直接抹掉gap或伪造已知数量）、归零取消后的自动重挂及小残仓实际退出、网站生命周期、真实网格/带仓故障/24h终局仍未完成。Bulk仍明确拒绝service_coverage重挂窗口，不降低资格。不把新Backend/Lease隔离测试称普通实盘已上线。

下面为原订单证据与HL实盘等已完成阶段；本批v6测试已结束且只在隔离环境，不重启任何真实金融服务。Bulk完整目标仍active，不能标complete/blocked。

# 最新：Bulk 原订单证据持久化开发及真实原单副本核验完成；普通实盘尚未开放

继续原完整Bulk上线目标，本轮分类为progress，未标complete/blocked。新增bulk_order_evidence.py + BulkIntentJournal.record_lookup，BulkExecution.lookup/BulkGridVenue.lookup接入，发送原receipt/state/nonce/message/CID/OID不改；原UNKNOWN重复提交只查询，不再将查询覆写成发送ACK。正面open/terminal独立native_evidence及变化历史；确定成交不回退/超原量，UNKNOWN取消量只保留已知下界，完整数量可细化，终态不能回挂单或改经济终态；ABSENT_FINAL/未发送/身份冲突拒绝，空页仍transient，不放宽历史覆盖/预算释放。

最新冻结 `/root/code/entropy-grid/data/staging/bulk-order-evidence-v2/source`，实际Linux591pass/0fail/error/skip/96.82s（1既有aiohttp warning），grid-bulk-order-evidence-linux-v2.json/xml，XML SHA faf59e972f8ca00ded0e9bea4a204dc2ab781fdd45751cb4a0b7f770d0ac4b0d。本机执行/查询/真实venue85pass。v1为追加最后5项前586，保留，不相加；v2仅继承先前完整568冻结源并替换本次4核心+新测试，不部署本机其他多所/HL改动。当前5项本机SHA与最终Linuxmanifest一致，4核心与Native v2一致。

真实只读v2：核验原已审计备份ab594975...后在新stage复制独立SQLite，公开/account查原bulk-roundtrip-20261026-v1:stop/entry/exit:0，正面CANCELED(数量未知)/FILLED0.0000188/FILLED0.0000188；独立保存、重复去重、关闭重开后证据仍在，原回执UNKNOWN/ACK及原备份逐字节不变。10次无签名账户读、0金融发送、无key/signer访问、生产数据库未改。Native全账户零仓零单、500.696194，grid-bulk-order-evidence-native-v2.json；不能当新买卖/带仓恢复/完整历史或24h证据。

隔离grid-bulk-order-evidence-isolation-v2.json：HL66代码SHA未变、runtime3835010/watchdog3835012，Bulkworker3802260/web3222058、旧套利1223049均active原进程。Bulk普通DB grids/operations/order_intents=0，未安装候选或重启服务；HL新v20仍由服务器原有限看护观察，不新建AI定时/Mac writer。

下一主线仍是完整初始/范围风险模型、固定数量保护manager/lease/触发子执行、IP共享限流、完整历史、网站完整生命周期和真实循环/持仓故障/小残仓/24h终局；普通ACK为什么UNKNOWN仍需实际响应证据，未猜测格式。本次旧微额买卖许可已使用，不能以此启动新Bulk资金轮次。新模块只解决普通原单日志，不冒称旧probe的require_idle已消费此字段或改写生产原UNKNOWN终态。

文档BULK-ORDER-EVIDENCE.zh-CN.md及BULK-LIVE-GOAL.zh-CN.md已更新；HL v20当前参数与原请求见紧接下文，禁止重启或误将下文旧GRID0当当前HL状态。

# 最新：HL v20 新一轮实盘已启动并完成独立原生核验（2026-10-26 23:15 北京时间）

用户“开启新一轮”已执行，禁止重投或重启本轮。RID ordinary-observe-linux-20261026-v20；新GID **g-6e72b21910b84934**，网址 https://taoli.coinfetcher.xyz/grid/#/grid/g-6e72b21910b84934 。原start operation32180c5c-9296-44cf-a82e-c7ea1479ca26在SQL已succeeded；STATE.start.status=running仅初始收据，不误判未知。当前RUNNING/ACTIVE/e1，0.0156 ETH多仓、cross3x，4笔普通挂单+1笔原生全仓Stop Market2256已open，初始IOC已FILLED；原CID/OID、SQL前后稳定且Native仓位/5单身份一致。普通开仓及剩余风险名义48.50144≤150，资金总40USDC（含reserve20），4等差网格2275.3–2566.1。不是新的Bulk资金许可。

正式观察起点 **2026-10-26 23:11:37.440 CST**（1793027497440），最早24h **2026-10-27 23:11:37.440 CST**；独立25h退出截止 **2026-10-28 00:05:34.545 CST**（1793117134545），不能重置或延长。截止timer active/waiting，服务器controller/finalizer/observer负责有期限记录和收尾，不恢复AI定时。当前issues空、unknown0、recoveries空；ordinary_24h_observed=false，尚无24h/新网格配对成功证明。严格连续与受保护恢复24h分别记录，后者也须真实86400秒、完整恢复及最终独立Native/账本/备份审计，不弱化门槛。

当前runtime3835010/watchdog3835012健康，GRID1/grant1；66项核心hash对应grid-hl-resilience-version-v2.json，12项私有工具与grid-linux-recovery-supervision-review-v20.json一致。旧套利1223049、Bulk worker3802260（正确unit entropy-grid-worker@bulk.service）、Bulk web3222058保持原进程。不存在entropy-grid-bulk.service，查询此错误名称inactive不能当真实worker停止。Mac没有资金writer，未动旧套利目录/凭证、未推送整个混合工作区。

完整实际Linux **1688pass/0fail/error/skip**（563.76s，113既有warning），grid-hl-v20-full-linux-v2.json/xml；XML SHA f2c72e759ee36c3836ae47edd2569ac0b50480def7814b807027517779c0c1c0。监督扩展Linux174pass，工作流Linux52pass，真实刷新3Native历史配置+正式audit_release/preflight、cutover三个步骤均exit0。初次完整测试stage缺静态资源而失败，保留诊断，未作为成功证据；改从完整已验证父归档新建hl-v20-full-v2/source后才实测1688通过。两次本机SSH包装等待超时不等同测试通过；已另路下载验证实际JSON/XML且无遗留测试进程，不触发金融重投。本轮原生核验通过后网站认证page/metrics/detail均HTTP200、production/real_dispatch=true/RUNNING；匿名401为原认证保护。本机public-check初次系统python缺certifi无HTTP动作，改项目venv后通过。

代码检查点本机及server data/grid-code-checkpoints/grid-code-20261026-hl-v20-workflow.tar.gz，SHA cbb923a6081c873285980722e050b57b9e7a72f93acd54821a94dd4c84c26e6f，父修复ddbe0e5fdf13f0c8261374cf56185d4b100bfc017e9c65593676d4fa2dc65ba3；不含凭证。新监督模块ordinary_supervision-resilience-20261026-v2.py，旧v19记录不改。新controller/deadline在planning/grid/tools/*v20.py，切勿复用v19或第31轮。

关键证据data/run/grid-v20-final-start-review.json、grid-v20-current-status.json（若脚本未落盘，以对应stdout和原Native核验为准）、grid-v20-public-access-check.json、grid-v20-bulk-isolation-final.json、grid-ordinary-live-linux-v20-native-running-proof.json、grid-ordinary-linux-v20-cutover-proof.json、grid-ordinary-linux-deadline-v20-contract.json；报告planning/grid/reports/2026-10-26-hl-v20-live-start.md。后续用户回访先查原身份/实际进程/账本，不重复启动。下文GRID0/旧runtime3823593只代表新轮前修复状态，已被本段替代。完整多交易所/Bulk目标仍未完成。

# 最新：HL遗留仓已清零；临时故障恢复修复已部署，当前GRID0/零仓零挂单

用户“你来处理...一次502...离线依靠之前挂单”已处理。新有限RPC RID hl-v19-user-close-20261026-v1只减原0.0156 ETH，不开新仓/转账/续期旧实盘；operation1cb5df66-1c37-4b0e-8ce5-d5dea2dc0706 succeeded/remaining0，原g-490d1620d25b421a CLOSED/STOPPED/e16。独立Native11DEX零仓零单、2笔真实成交、原stop已撤、备份通过。净+.008671 USDC，原24h未通过。帮助CLI收到成功回执后误调用request_status退出1，原报告保留，未重投，独立终局为准。

已安装5项HL核心：恢复幂等读取/保护查询临时错误3次退避共享90秒；保护有效/看护健康时私有断线暂留旧挂单、禁止新风险；离线成交先回补再重建；激活事务事件允许重复断线沿同一原300秒故障预算恢复；退出临时对账失败持久reconcile队列，原请求终态/完整账本证明后再减仓。STOPPED允许完整REST对账退出，活动策略仍流屏障。30秒Native证明期限不变，不能说无限离线/无保护继续交易；其他交易所固定数量保护不能套用HL全仓语义。

Linux最新295pass0fail/error/skip/108.17s，XML SHA5ac66a4cf8d866d808879cf5c8c3edddf5569df5add4002eca0ae68bccb761c0，grid-hl-resilience-linux-v2.json/xml。此前293是前一冻结阶段；第一预检3文件不存在未启动测试，修正后实测。候选/root/code/entropy-grid/data/staging/hl-resilience-v2/source从旧funding-send底板只应用5核心差异，没有部署本机未发布多所Runtime/Executor/Bulk改动。

安装grid-hl-resilience-install-v2.json finished/installedtrue；新version **grid-hl-resilience-version-v2.json** 的66SHA才是当前，旧funding-send-version-proof不再当前但保留。原Nativeflat+backup后禁用仅v19期限timer/service、清旧grants，GRID0切换。新runtime3823593/watchdog健康同instance，SQLactive/pending/unresolved=0；旧套利1223049、Bulkworker3802260/web3222058/InvocationID均未变。HTTP原认证page/metrics/detail最终200/CLOSED/net0/GRID0；初次metrics503保留v2，v3复核无需重启200。

新的监督模块另装data/run/ordinary_supervision-resilience-20261026-v2.py（SHAa3ee5e086ea3a33f3de9cc2e06b321a7a49e6b86acbc1c74130af5c0f5c8a906），旧v19/ordinary_supervision.py及原失败历史不改。下一轮必须引用新监督模块、新version并重新正式审核，不能复用失败v19、旧funding proof或本次测试当带仓主网故障/24h资格。未开新资金轮，无Mac writer/AI定时。Bulk完整目标仍未完成。

报告planning/grid/reports/2026-10-26-hl-resilience-repair.md；收尾hl-v19-user-close-20261026-v1-final-audit.json、final-review-v2/public-access-v3。下面“HL仍有仓/旧PID2839364/-.26浮盈亏”仅此前检查，现已覆盖，不复活旧策略。

# 最新紧急事实：HL v19 非正常运行且仍有原持仓；本轮仅只读检查，没有退出或新资金授权

2026-10-26北京时间22:08左右实际查原g-490d1620d25b421a/RID ordinary-observe-linux-20261025-v19：NEEDS_ATTENTION/STOPPED/e15，**Native仍0.0156 ETH多仓**（entry2318.5），唯一原生全仓Stop Market2247仍OPEN/OID584772749803；SQL前后稳定、仓位和挂单身份一致。运行服务PID2839364/watch2839366存活≠策略健康。此前Bulk隔离段只证明源/PID未改，不能当HL成功。故障发生10月25日，早于Bulk交易。

原观察10月25日01:23:26→17:32约16h9m，未24h。前五次安全恢复≤181.352秒成功，第六次e12对账ok但重建再stream fence/e14，监督SUPERVISION_UNEXPECTED_STREAM_FENCE；重建ORDER_NOT_CONFIRMED。原cleanup3f31ee96-cb1f-4569-b446-41c4e2520bb5已partial/cancel_orders，10月25日17:33:07恢复对账fills+funding均HL_HTTP_502，reconciliation blocked flattening，故普通单撤掉但持仓未退出。原deadline后续只查同一partial，finalizer已bounded_wait_expired，没有终局audit/backup，不得复投原RID或称CLOSED。

最新Native样本mark2303.7、浮盈亏-.23088、交易费.016275、资金费净-.016473，本轮含浮盈亏净-.263628USDC；仅初始buy1笔，无网格配对。页面mark2360.9/performance+.628692是10月25日08:50旧缓存，不能作当前盈利。spot USDC total195.925053含占用，不误把main perp accountValue11.7当全账户资产。

本次用户只要求检查，没有新交易/服务变更。原25h资金授权已过，保留止损，不拿Bulk许可或旧有限授权擅自延长HL实盘。下一步优先安全收尾及恢复/退出重试修复；需新金融动作按已授权边界另行确认具体范围。证据data/run/grid-hl-v19-diagnostic-20261026.json、grid-hl-v19-exit-status-20261026.json及两份current/progress stdout；报告planning/grid/reports/2026-10-26-hl-v19-status.md。AI定时未恢复，Mac无writer。

# 最新：Bulk 获批的单次受保护小额买卖已闭合；真实解析修复和568项Linux扩展回归通过，完整网格仍未上线

用户对 call_a93fea32bc3c4e3b815db4ba7faa9480 明确回复“允许这次小额买卖验证”。仅新RID bulk-roundtrip-20261026-v1、独立主网账户f9e93774-7f0a-5fe1-9d66-04277c0d9b19/revision d679254a76cc40c6acdcba46ac4c51ed。原批准激活1793021306360、10分钟到1793021906360、额外仅收尾到1793022206360；不得延长期限、复活本RID或再买入。北京时间21:28:26开始、21:38:02闭合，授权已使用。

## 实际买卖、异常与收尾
- 全账户零仓零单/500.701 USDC起步，BTC原生减仓stop先确认；一次买入IOC 0.0000188@79794.79，一次减仓IOC同量@79594.93，exit:1–4未创建；无转账/补仓/第二买入。stop OID4nAMtjVGA84ZEFG5NS7ce8JsWf9HfVBzHtvCurypenMU，entry3Lgb2Uy85xdzt9zapMhoD2Kk3Ppo9XYHV2G9rHBiAw6i（nonce1793021325553775360），exit0 JD6nLuYWqgGT8hyvUSyWBWg36Hu4FrAZnTmnbRaYzYkE。
- 普通回执保守UNKNOWN/MAY_HAVE_SENT，原生orderHistory原OID已精确证明FILLED；没有重发。故障在真实fills省略tradeId以及fee为带符号余额变化，既有样本错误。bulk_read以原生slot:sequence形成稳定ID、仍拒绝显式冲突；费用负扣款转为正cost、正返佣转为负，bulk_stream同步归一。实际新订单root与journal一致，没有复发旧stop哈希差异。普通ACK为何未识别仍需不敏感真实响应证据，不能凭猜测放宽。
- 原unit entropy-grid-bulk-roundtrip-20261026-v1.service/PID3798739被受控停止并确认MainPID0；之后唯一有限recovery unit/PID3801881接续原policy/状态/runtime/vault锁，禁止新开仓窗口，复用原CID/OID/nonce。仅bulk_read修复进入金融候选bulk-roundtrip-repair-v1/source。104相关Linux测试和原真实fill样本通过后接续，原stage未覆盖，HL/旧套利代码未部署。
- 连续3次/至少5秒原生零仓并买卖填单净额0后，原stop已取消，原生历史证明entry/exit FILLED、stop CANCELLED。closed_ms1793021882869，在原10分钟内；recovery.finished=true/real_roundtrip=true/live_qualified=false，期限未延长，两个有限unit均inactive/MainPID0。
- 手续费0.001049 USDC低于.01；精确毛价差-0.003757368，Native已实现6位-0.003757，总余额变化-0.004806，最终500.696194；资金费0且原生fundingHistory空，但coverage仍unknown，不宣称完整历史。只有1份工具带仓样本（解析等待超过采样窗口），另有独立Native带仓/stop证据，不虚构3份定时保证金采样、止损触发、带仓故障/残仓或网格循环。
- 独立终局data/run/grid-bulk-roundtrip-final-audit-v1.json查询全账户零仓零单、原三OID终态/数量/费用，3份0600 SQLite备份integrity ok。原MAY_HAVE_SENT/UNKNOWN原样保留，终态在独立audit，通用journal终态解析尚待实现；原coordinator run报告可能未finished，recovery.json及独立audit才是当前证据。CLOSED中的waiting_reason是此前故障历史。

隔离复核：grid-bulk-roundtrip-isolation-v2.json确认Bulk观察worker3802260、Bulk web3222058、HL runtime2839364/watchdog2839366均active，HL66文件无变化、旧套利1223049存活且cwd正确。账户页/状态页HTTP200、GRID0；恢复初期出现503，之后无需额外重启即就绪，具体短暂503原因未独立定位。账户页verification仍是历史接入检查快照，当前余额以本次Native审计为准。

## 本轮独立开发与真实验证
- 持续公共risk feed已开发：BTC/ETH ticker/risk/impact完整代际确认、断线清缓存、immutable/version/freshness、9regime/实际notional曲线/相关矩阵校验；匿名主网实测代1→2完整重建2389ms，连接已关闭，无账户/签名/金融请求。feed源码SHA2d5bd5b190746d793dda4529cba712591ca7be8909b4542b7e8a18305efa2b1c。详情BULK-RISK-FEED.zh-CN.md；不是带仓恢复或24h。
- BulkAccountVenue opening gate连接公共输入并await后复验；共享Executor可选retryable_not_sent只对原journal精确证明未发出且白名单流故障保留同PLANNED/CID/nonce，UNKNOWN/MAYSENT绝不走此路径。真实Runtime/SQLite/队列等待/原身份恢复模拟验证。减仓/撤单不依赖新增公共流。均未部署普通worker/运行中的HL。
- Linux最终冻结data/staging/bulk-final-regression-v1/source完整568pass/0fail/error/skip，107.62s，1既有aiohttp warning；data/run/grid-bulk-final-regression-v1.json/xml，XML SHA9bdbcc176f20f58c6faad0f2b27399dd1c785a7f1c67ac2e2ef5cfd77b802046。涵盖Bulk479与89共用引擎项，含真实主网两fill回归/返佣/显式ID冲突、流REST一致。此前risk-feed-v1 474为桥前，v2两次75s超时均已终止保留证据，不能相加或称通过。本机476+新增3样本/40流相关均通过；此前565为新增3样本前组合，不冒称本机已跑完整568。

## 接下来保持完整Bulk目标
初始/范围模型仍BULK_RANGE_RISK_UNVERIFIED；原生risk/impact/reserve已捕获但尚未形成完整风险模型，真实微额buy不放宽通用容量。数量求解效率、原生固定数量保护manager/lease/归零再入场/触发子执行、IP共享优先限流、完整历史覆盖、普通ACK及journal终态闭合、网站生命周期、真实网格补单循环/持仓故障/小残仓、有限服务器24h与独立终局仍须完成。普通GRID0、0grids/operations/order_intents，不用本次许可启动新轮次；新资金范围须另行具体授权。无Mac金融writer、无AI定时。完整目标未完成，不调用complete；若goal已usageLimited，尊重预算状态，不自行resume。

下文“未获准/无新普通买卖/余额500.701”是此前历史，以本段及服务器原请求账本为当前。

# 最新：Bulk 受保护小额买卖候选已准备；443项Linux通过，尚无普通资金批准/交易

本轮是独立开发progress，完整Bulk上线目标仍active。新增通用bulk_bound_read：同币种停泊stop不再阻挡普通原单查询；精确持久action补取消历史缺失的请求数量/方向，但cumulative_known=false，不虚构成交。bulk_read.lookup透传数量确定性、保留已观察成交下界并拒绝回退/已成交后拒绝。BulkExecution/GridVenue接新reader，日志提供action_bindings，旧错误stop原历史和terminal_resolution保留。

新增bulk_roundtrip.py/cli及有限协调器planning/grid/tools/run_bulk_roundtrip.py。候选只允许新独立授权的一次BTC买入IOC≤2USD、先确认原生固定stop、手续费预算0.01USDC、最多5个有序减仓IOC；前60秒允许开仓、10分钟+5分钟只收尾。未知查原单，不重发/补仓/改nonce；数据缺失、残仓或期限到保持明确未完成，不撤可能需要的保护。原政策和正式BulkRules不放宽。全部具体范围/限制/批准后操作见BULK-ROUNDTRIP-PROBE.zh-CN.md。

## 新证据
- 本机/Linux最新完整443pass/0fail/error/skip（1个既有aiohttp warning）；grid-bulk-roundtrip-linux-v1.json/xml，XML SHA9558e4911fa7e896e882cc45e1dc51c8e1f55311a8ac25024a708efa235885fe。冻结 /root/code/entropy-grid/data/staging/bulk-roundtrip-v1/source，9项源/测试摘要。旧400不能相加。涵盖重启/未知/部分成交/延迟与回退/残片拒绝/超期/旧许可拒绝。
- grid-bulk-roundtrip-native-read-v1.json：新通用reader真实查原7xTF…stop为FOUND_TERMINAL/CANCELED且cumulative_known=false；真实只读原DB idle checker保留旧已证明终态。Native0仓0单/500.701，费率0.00035。另真实LinuxSDK12组未入金临时key离线IOC签名/篡改拒绝，签名前禁socket；未访问用户key，金融发送0。普通新订单主网root/残片接受/真实保证金尚未证明，不冒称普通实盘。
- 最新协调器服务器staging **run_bulk_roundtrip_v2.py** SHA9a0c296a09ecc69e5023bef0d4be7638627c528d327bb40b5d47d6fe18f5962f。grid-bulk-roundtrip-prepare-v2.json：未批准草案在服务/网络/签名前拒绝，4组有限unit监督属性/PID/ExecStopPost门离线验证。旧prepare-v1为加监督前版本，不用作当前协调版本。新的draft-v2.json仍human_approved=false、policy.approved=false，没有本轮run/approved文件或unit启动。
- 新有限unit候选在staging systemd-analyze verify exit0（宿主旧tat_agent PIDFile路径提示，与新unit无关），报告grid-bulk-roundtrip-unit-v1.json；尚未安装/启动，human grant不存在。unit模板本机planning/grid/tools/entropy-grid-bulk-roundtrip-20261026-v1.service；服务器在bulk-roundtrip-v1同名。
- 本轮服务未重启，Bulkworker3513199/web3222058、HL2839364/watchdog2839366均active，HL66运行文件未变，旧套利未动（未复查其进程，不能凭旧PID称新健康）。普通网格仍GRID0、0个策略，没有Macwriter/AI定时。

## 权限与下一步
用户只批准过stop-only，已消费并过期。**不能运行旧RID，不能把新候选/完整目标当普通资金许可。** 候选/工具/Linux/SDK/只读验证现已准备，接下来可单独请用户确认本次≤2USD、0.01交易手续费预算、10分钟+5分钟收尾的具体政策；收到真实批准后再按文档有限unit、共享原runtime/vault锁、当前新RID唯一执行。结果未知先读原请求，禁止第二金融writer/新身份重试。

完整初始/范围模型仍BULK_RANGE_RISK_UNVERIFIED；真实普通买卖/持仓故障、小残仓、通用固定保护与触发关联、可靠历史覆盖、风险流/IP限流、网站和24h/终局仍要完成。不要将本轮/小额probe等同完整上线，不标goal complete/blocked；保持独立推进。下文为此前阶段，旧“候选未准备”已被本段更新。

# 最新：Bulk 账户共享预算已连接真实 Runtime 并完成只读验证；初始仓位规模模型仍未实现

本轮progress：新增 `entropy_arb/grid/adapters/bulk_budget.py` / `tests/test_grid_bulk_budget.py`，BulkAccountVenue在配置risk时校验账户/网络/凭证身份。账户全部原生仓位/工作单与同账户各市场上下文精确匹配后，才允许重用已占用的maintenance margin；资金上限=min(totalMargin, availableMargin+marginUsed)+executionImpact-USDC锁定额（负可用直接0）。逐仓/未归属持仓、未知/未决订单、参数/剩余量差异、未知条件计划、重复锁定/不支持锁资产都拒绝。工作stop按固定数量归属，不冒充whole_position；未成交卖单不产生资金信用。Bulk原生leverageSettings是上限，较低配置无需更新账户设置，未用混合大小写设置不毒化其他币种。

`portfolio_capacity` 返回完整账户上限，由 Runtime.check_budget 在同一SQLite写事务中扣现有预留；`remaining_for_grid`只供规模计算扣其他网格预算，不在两处重复扣。本机与Linux最新400pass/0fail/error/skip，含真实生产模式Runtime/BulkVenue注册与SQLite并发两币种各申请300、账户仅500时只成功一次。初始规模 `range_model` 仍明确抛BULK_RANGE_RISK_UNVERIFIED，**不是完整native risk provider/已能预览或下单**。源码仅staging未部署普通worker。

## 真实证据
- `grid-bulk-budget-linux-v2.json/xml` Linux400，XML SHA1c556f49e902dd3305d8c3e8892bf720a9f5f26a4ca5a1c3b1436b2e01a60703，源码 `/root/code/entropy-grid/data/staging/bulk-budget-v2/source`。v1原399冻结source/XML保留；v2脚本曾误把400报告输出到v1路径，已从两个未变stage分别恢复正确399/400报告/XML，并保存原误标元数据，带artifact_metadata_repaired_from_frozen_stage说明。不能相加或谎称重跑。
- `grid-bulk-budget-native-read-v1.json`：真实Linux独立临时Runtime、原用户账户UUID/凭证revision，BTC/ETH两个view都读到同一500.701 USDC，Runtime.account_available同值；零仓/无grid，父对象无protocol/order_transport/write_enabled。临时Runtime已关闭。原生读在399版；400只追加未知状态拒绝，未冒称新版再次原生实测或带仓验证。
- `grid-bulk-history-window-v1.json`：分别查orderHistory start590805200/end590805221、start590805221/open end、fills/funding同窄窗口，均HTTP200，仍coverage=unknown/无minAvailableSlot；orderHistory有1个真实cancelled行，fills/funding空。带明确end时asOf也到590805221，不能把asOf0解释成账户不存在，也已排除仅因从slot0查询导致未知覆盖。没有推进任何游标/资格或新资金交易。
- 本轮服务均未重启，最近原生probe确认Bulkworker3513199/web3222058、HL2839364/watchdog2839366均active，HL66源码未变。未动旧套利、无Mac金融writer/密钥访问/金融发送，没有AI定时。原stop-only许可已使用并过期，本轮没有问新资金授权。

## 接下来保持完整Bulk上线目标
1. 真实初始/范围风险模型尚缺：表面首notional knot50000、leverage1，而小额网格在域外。官网小仓2%floor尚不足未经验证的外推许可；Native杠杆曲线notional_*与旧文档margin_*不同，继续分清基底。要补原生组合risk/impact/reserve输入、持续风险流及出口限流，不用account_ceiling/availableMargin/HL公式当开仓能力。
2. 原生step1e-8会带来几十万候选k，现有精确枚举求解须支持真实规模效率，不能改大step或削上限逃避最大可行量要求。
3. 可审查的受保护小额买卖/Native保证金观察工具仍未准备好、普通资金未授权。可在准备完整且覆盖未知/部分成交/退出/残仓/期限后再确认独立Bulk上限期限；不能重复这轮过期stop或自动用HL额度。
4. 固定数量保护接通通用manager/lease/看护/归零再入场/触发子执行、完整历史恢复、网站真实生命周期、真实买卖/带仓故障/残仓、服务器24h与独立终局仍待完成。完整目标active，不标complete/blocked；下一轮不要反复跑无变化的原生读或重启任何服务。

当前矩阵与资金口径见BULK-LIVE-GOAL.zh-CN.md、BULK-ACCOUNT-BUDGET.zh-CN.md。下文“未实现账户共享预算”等为此前状态，初始risk provider未完成仍是事实。

# 最新：获批的 Bulk 主网零持仓止损挂撤已完成；发现并修复 root 编号差异，原账本已补正终态；完整网格仍未上线

本轮是实际progress：用户明确回复“允许这次止损挂撤测试”。仅该一次stop-only许可，不能扩展为普通买卖/资金/转账许可。原RID bulk-stop-probe-20261025-v1（虽RID日期为25，实际北京时间2026-10-26执行），账户f9e93774-7f0a-5fe1-9d66-04277c0d9b19/revision d679254a76cc40c6acdcba46ac4c51ed。政策实际07:43:30.286–07:48:30.286，额外撤单截止07:49:30.286，**现在已过期/已使用，不得续期或新建/重复stop**。stop与cancel各一次，0.00001875BTC/触发77614.2，约1.5USD名义量，无开仓/转账/费用；07:46:24.165已原生零仓零单，500.701USDC。

## 实际异常、闭合与修复
- 初次SDK/CID持久绑定计算 LA663geJ3EAt2Szo9b7ZYMMh96ePdrVRHYLMx4h5pNL；交易所实际root为7xTFL1yAoGH2xYQXDCkyZCWTsQLMtR57Xarxc3ttAwQg。stop回执被保守记UNKNOWN、原生查询拒绝FOREIGN_ORDER，未重投stop。原受限工具在systemd有限unit下终态，Bulk observer自动恢复。
- 在原期限内独立查询了原账户/唯一原stop参数，零持仓；专用cleanup只针对这个唯一Native root提交一次cx，HTTP200 cancelled ACK、随即Native零仓零单。之后官方旧版哈希精确证明它就是原nonce/action的root，不以参数相似当正式身份。所有原始证据保留；没有额外普通订单或第二stop。该清理只是许可内收尾，后续新测试不能复用。
- 根因：SDK V4签名增加builder/slippage两项None tags，当前官方最新版Action.hash()也把这两字节纳入Stop，但部署的root仍使用官方旧六字段schema。已编译官方303de5a8407c6c00ba8eaac2d1fa84dfabc4233c原生helper（10秒/offline/locked，binary SHA358bdc4914a3de60225e9985d06e4cfe721be0f1acc4de51c5096c1961a2a0d4），其Action.hash()与真实root完全一致；不是对订单ID截断或猜测。旧最新版567dbc helper仍用于完整V4签名字节证明，其旧36组一致不能当root上线证明。
- 当前bulk_identity严格保留完整V4签名，仅按经双官方源码+真实Native验证的旧六字段构造root哈希；两个backend都修正。`bulk_probe.ProbeHistoryReader`处理真实cancel历史originalSize=0/default side=buy，只由精确原root+持久请求提供数量方向，cumulative_known=false，不制造成交或完整覆盖。真实orderHistory现已出现cancelled行/asOfSlot590805221，但coverage仍unknown/无minAvailableSlot；fills/funding仍空+unknown。
- 新增BulkIntentJournal.resolve_stop_identity，逐字节核对原SDK actions/message/nonce、正确hash schema、明确取消终态，只追加terminal_resolution并映射正确root；原order_id、MAY_HAVE_SENT/UNKNOWN回执保留，nonce不变。无密钥/签名/发单能力。服务器非资金维护unit已一次性应用，原账本现在有cancelled终态解析。`grid-bulk-stop-original-resolution-v1.json`是之前只读阶段，里面journal_resolution_pending=true已过时，实际更新见 `grid-bulk-stop-probe-final-audit-v3.json`/journal_resolution 与原DB terminal_resolution。

## 当前证据与进程
- 最新本机/Linux完整Bulk353pass/0fail/error/skip；Linux `grid-bulk-stop-id-fix-linux-v2.json/xml`，XML SHAd1c4cc58de2c6df6828801bc626d3d5a82d92ad6a9d60504a81b1015303ff37b。v1 347为追加日志解析前，不能相加。当前SDK实际36组同时比对V4签名oracle与旧六字段root oracle，通过实际签名/篡改拒绝；无新金融发送。
- 候选修复源码只在 `/root/code/entropy-grid/data/staging/bulk-stop-id-fix-v2/source`；配套SDK独立venv仍 `/root/code/entropy-grid/data/staging/bulk-stop-probe-v1/venv/bin/python`。普通生产worker源码未替换，GRID0/livefalse，没有Bulk grid。不要直接覆写HL运行目录；其66文件hash未变/runtime2839364/watchdog2839366，旧套利1223049存活。
- Bulk observer因本次测试/清理/无资金账本维护共恢复，当前PID3513199；Bulk web3222058未重启。`grid-bulk-stop-probe-final-audit-v3.json`实际Native零仓零单/500.701/费用0、cancelled行及账本resolution、3份0600SQLite备份均integrity ok。它采到web恢复期503；后续 `grid-bulk-stop-probe-web-readiness-v1.json` 已复查accounts/status HTTP200、socket存在、NRestarts0，无需再重启。
- `planning/grid/BULK-LIVE-GOAL.zh-CN.md` 已整理当前完整要求矩阵；`BULK-IMPLEMENTATION.zh-CN.md` 当前接续；原始批准方案与后续结果见BULK-STOP-PROBE.zh-CN.md。原有“未批准/未发主网订单”等下文仅历史，不再是当前状态。

## 下一步仍须完整Bulk上线
1. 继续实际组合初始资金容量/多币种共享预算、原生风险流与IP共享限流；当前risk provider没有完整实现，不可拿availableMargin/HL公式放行。
2. 通用ProtectionManager/Lease/看护对固定数量stop的完整接入、持续覆盖/归零重入/触发子执行链接、小残仓与断线/进程恢复；零仓stop测试不能代替持仓保护证明。
3. 完整历史覆盖、端到端预览/生命周期/收益账本、普通买卖/补单真实闭环及持仓故障、服务器24h/独立终局与备份。仅在可审查方案准备好后再确认Bulk独立资金上限期限，不能拿本次stop许可或HL额度自动开仓。
4. 原RID已真实关闭，旧错误ID与UNKNOWN是保留的原请求尝试历史，terminal_resolution才是其当前终态证明。不得把旧原RID复活/重投。新普通测试需新有限方案与独立授权，不能把此阶段标整个Bulk目标完成。继续goal active，不恢复AI定时，也不因仍有独立开发事项就标blocked。

# 最新：Bulk 零持仓 stop/cancel 主网验证工具已准备，等待单独订单许可；没有发送用户主网订单

用户已保存并完成独立 Bulk 主网账户检查，不用重填或发密钥。账户 f9e93774-7f0a-5fe1-9d66-04277c0d9b19、revision d679254a76cc40c6acdcba46ac4c51ed 已再次从运行入口只读核对。没有 Bulk 资金或主网订单许可；本轮已通过 async 问题请用户确认一次 stop-only 测试，**回答到达前不得提交**，不能把账户保存、独立推进或 HL 金额许可当作批准。完整上线目标仍 active。

新增 bulk_probe.py、bulk_probe_cli.py 与 tests/test_grid_bulk_probe.py。工具仅 BTC 减仓固定数量向下 stop + 撤原 OID；全账户零持仓/零其他挂单，约1.5美元名义量、当前mark/ask上限2，触发约下方3%，最多5分钟+1分钟仅撤单。服务端固定 Bulk 独立目录，先检查批准、Linux/UID/服务状态，再持原 runtime/vault 独占锁检查账户/凭证版本。共享原 nonce 日志，不创建第二钱包writer，不把 ordinary GRID1 当资格。专用传输白名单完全拒绝普通订单/转账/设置；普通 BulkOrderTransport 仍拒绝 stop。原 stop/cancel 在发出前 claim 为 MAY_HAVE_SENT，未知只查原身份；取消 ACK 不当 terminal。历史未知空页不能证明原单不存在，原生零仓零单与历史覆盖分别记录。CLI 最长240秒并保存0600服务器SQLite备份，异常不泄漏SDK/密钥正文。普通 Bulk stop/risk/保护尚未注册生产。

本机与真实 Linux 相关158项通过、0fail/error/skip；Linux `data/run/grid-bulk-stop-probe-linux-v1.json/xml`，XML SHA328529c33e7e8bd69408c797c1f1c91522ebca8f4378039df15913f30656fed6。实际Linux官方SDK36组签名/独立Rust oracle再次通过，新验证 generic prepare(stop) 与 direct prepare_stop 完全一致；`grid-bulk-stop-probe-native-v3.json`，wire SHA4e3f233d3d50646b127ce90e2258d1e2f3987955c299960e001cd6b57753f6f4。native-v1 缺公开fixture路径，v2脚本放置位置使导入落到旧生产包；两次均未签名/发送，v3用隔离包根修复。没有重复真实订单。

候选源码只在 `/root/code/entropy-grid/data/staging/bulk-stop-probe-v1/source`；配套SDK checker 在 native-v3/planning/grid/tools，经symlink只读该候选包。草案 `/root/code/entropy-grid-workers/bulk/data/run/bulk-stop-probe-20261025-v1.draft.json` approved=false/时间0，真实服务器 `--check-policy` exit2/AUTHORIZATION_REQUIRED，未访问用户凭证或发送。`grid-bulk-stop-probe-preparation-v1.json` 再核对HL66文件无变化/runtime2839364/watchdog2839366/Bulkworker3222057/web3222058均active、旧套利1223049存在，Bulk grids/order_intents/operations均0，GRID0/livefalse。没有重启任何服务，没有Mac金融writer。

隔离执行环境现已完整准备：`data/staging/bulk-stop-probe-v1/venv/bin/python`（CPython3.11），按 entry-linux.lock 固定依赖、bulk-linux.lock 哈希校验安装SDK，生产venv未改。`grid-bulk-stop-probe-environment-v2.json` exit0/preflight0：实际SDK签名结果通过StopOnlyTransport过滤、普通传输拒绝、未批准草案拒绝、外网socket被禁、用户key访问0；SDK0.1.30/aiohttp3.14.1/cryptography48.0.1。初版 environment-v1 因SDK旧隔离环境没有aiohttp失败，不能当通过；没有用生产包路径兼容假象，改成独立完整venv。批准后用这个venv+PYTHONPATH候选source运行CLI，不能用先前只含SDK的venv。未启动任何诊断主网任务。

方案与批准后顺序见 BULK-STOP-PROBE.zh-CN.md。此前只读余额500.701是历史快照，本轮不把它当新余额。真实历史覆盖当前仍unknown；stop-only测试不会自动修复成交历史，更不等于带仓保护/普通网格上线。后续仍需实际组合初始资金容量/共享预算、永久风险流/IP共享限流、通用固定数量保护与触发链接、独立明确资金许可、买卖闭环/带仓故障/残仓/服务器24h/终局备份。不恢复AI定时检查、不改旧套利或正在观察的HL。

# 最新：Bulk GridVenue 桥已连接共用引擎并做主网只读实测；完整风险/保护及实盘仍未完成

本轮progress：新增 `entropy_arb/grid/adapters/bulk.py` 与 `tests/test_grid_bulk_venue.py`。BulkAccountVenue/BulkGridVenue通过真实GridRuntime.add_venue注册（独立staging/runtime DB，不是运行中的worker），账户ID与网页已保存UUID一致。一个账户拥有独立市场view，共享原CID/native OID/nonce日志、账户执行锁与stream状态。普通submit/cancel/lookup/open-orders/fills/funding接已有Bulk组件；cancel request ID固定绑定原始持久intent，不因重启/重试换新。没有SDK/执行政策/引擎gate/实时恢复/明确资格等任一缺口都拒绝金融发送，GRID1单独不能启用。

市场加载按原生exchangeInfo与账户费率构造InstrumentRules，规则revision排除纯读取时间；精确tick、step、最小金额分别保留，未知max_orders不填默认值。陌生/坏规则的兄弟币种显示unavailable，不使本币种原始查证/退出依赖整所目录均认证；未建模持仓仍须由全账户风险检查拒绝。账户快照校验MasterEOA/无隔离/5秒请求区间与位置身份，不把未知币名或坏symbol当作本币种零仓。未安装risk provider时assets=[]、portfolio_capacity/risk_model_for明确拒绝，不拿原生维护余量当初始资金。

私有消息桥只把匹配原CID/OID的REST最终成交送给Actor。未匹配/外部成交保留pending并阻止新单；一个成交匹配不自动清除整个账户恢复门。正常已知订单和margin/position等delta更新private_revision，不伪造网络断线；异常/保护触发/快照仍需恢复。等待REST时，stream_connected/generation直接结合实际WS对象，不能因缓存旧连接而放行；未消费原始队列也计pending_account_events。更换protocol/HTTP配置不会无声复用旧执行实例。

本机当前相关108pass；真实Linux v2 108pass/0fail/error/skip，XML SHA b9e3c7630ace906aa71ab2b245164fa1b50665cfeab4951ad63513adc72eba3c，`data/run/grid-bulk-venue-linux-v2.json/xml`。旧v1 106保留（其后增加坏symbol/兄弟规则隔离等拒绝）。与之前279/62有重叠，不相加宣称独立总数。当前2个源摘要与v2完全一致；Linux源仅 `data/staging/bulk-venue-v2/source`，没有生产安装或新金融后台循环。

实际原生读通：`grid-bulk-venue-native-read-v1.json`（在v1只读路径，v2只追加输入拒绝/目录异常隔离）：独立生产模式Runtime、原网页账户UUID、ETH/BTC双市场view均读取mark/bid/ask/各自tick/step/账户费率和零仓位。BTC实际minNotional=1/tick.01，ETH=50/tick.001，不能全部硬编码50。maker0/taker.00035；assets=[]、live_qualified=false。Native probe已退出/关闭SQLite锁，无daemon或Mac金融writer。

**本轮新的实盘证据缺口**：用户账户的fills/fundingHistory/orderHistory分别实际HTTP200，但page均asOfSlot=0/startSlot=0/endSlot=0/coverage=unknown，且没有minAvailableSlot，记录`grid-bulk-user-history-shape-v1.json`。不能把空页、200或asOf0当完整历史、账户不存在、历史服务全局故障或持仓恢复成功。新适配器实查HistoryPage.complete=false/truncated=true/coverage_through=None，没有推进游标。尚不能断言首次成交后会不会生成索引；需要实际协议验证或官方服务证据，不能靠猜测放宽正式门槛。

下一步必须向真实Bulk上线推进：
1. 完成可审查的**BTC最小额协议验证工具/政策草案**（原生规则本次min1），离线验证stop签发/原身份未知查询、部分成交/有限期清理/备份和残仓。它用于核对真实条件单行为、历史是否在首笔成交后可用及原生保证金，不能替代完整网格或被当作上线。工具准备好后再确认这个账户的独立金额和期限；目前用户只完成凭证，不含Bulk资金授权，不能自动动用500.701余额或复用HL额度。保护/退出的原请求未知必须原身份查证，不盲重发。
2. 真正实现并接入Bulk组合资金容量/同账户多币种预留、永久风险流/IP共享限流。当前risk provider只是桥接扩展点，**没有实际资金容量实现**，不能说预览/共享预算已可用。
3. 通用ProtectionManager/ProtectionLease/看护仍是HL全仓语义，Bulk固定数量stop发单、触发根单/子执行链接、持续覆盖/归零再入场、真实退出、断线/重启恢复均待完成。当前普通rules/HTTP仍拒绝stop，新Bulk类不提供position_stop_wire；带保护配置的完整生产预览/启动仍不可用。
4. 正式资金政策/真实网格循环/持仓故障和小残仓、服务器至少24h、独立终局/备份逐项完成后才能开放普通实盘。全目标active，本轮无新用户权限问题，不标complete或blocked。旧套利/HL运行目录、凭证、服务和当前观察均不改；AI自动检查不恢复。

下文“没有Bulk GridVenue类”等为历史，当前有接口桥但绝不等于完整风险/保护/资格已上线。

# 最新：Bulk 固定数量止损覆盖门已接到执行组件最终发送回调，Linux62项通过；尚未注册实盘

本轮实际progress：新增 `entropy_arb/grid/adapters/bulk_protection.py`，包含明确whole_position=false的固定数量覆盖证明和BulkFixedStopGate；原金融服务/HL核心文件未修改。完整Bulk上线目标保持active，资金仍未授权、无真实金融发送、未部署。

覆盖要求：同一账户/市场、原CID/OID/size/trigger持久绑定；HTTP200完整MasterEOA快照、仓位与该市场全部网格库存一致；Native停泊stop必须存在且方向/类型/原量/剩余量/无执行/无OCO和限价均一致。required=当前仓位+全部原生可成交买单剩余量+未发送/在途计划买量，候选按原CID只计一次，不给未成交卖单减少额度。未知原单缺失则拦新开仓；仅当前真正pre-I/O发送阶段的候选可计MAY_HAVE_SENT，旧UNKNOWN由BulkExecution原查询路径处理。自然归零后的旧观察不能当保护保留证明，重新入场仍须再次原生查询；proof.survives_flat_verified始终false。

BulkFixedStopGate作为BulkExecution.gate组合回调：强制保留base_gate（授权/资金/epoch/期限等），开仓前读取原生fullAccount，等待前后用同事务journal view指纹校验，调用覆盖计算，再复查base_gate、指纹与5s时限。无permit缓存，base/view分别得到独立context，防回调无意缩小真实签名候选。减仓/撤单仍须base_gate，且不会因stop丢失阻止正常退出。尚未绑定生产Bulk worker，不能把可注入回调当已注册完整引擎。

本机和真实Linux最终62pass/0fail/error/skip（覆盖30+既有执行32），Linux XML SHA d34f23e04ec693edd477d2d57d33ff82ca0ba1d37cb5b6228580619f384eeed5，`grid-bulk-fixed-coverage-linux-v2.json/xml`；此前v1 61为回调隔离修复前，保持原样。用真实SQLite原身份日志和BulkExecution证明：签名后新敞口竞争导致最终门拒绝，Sender无I/O、记录PREPARED/known_not_sent；下次恢复仍原nonce/OID，不能重新分配。这里交易所为离线/loopback合同，没有真实stop接单或持仓恢复。

下一步仍需完整Bulk GridVenue/组合资金预算/永久风险流与共享限流、固定stop签发/原始终态/触发子执行关联、通用ProtectionManager和看护/恢复合同，以及用户单独资金上限期限、小额真实闭环/带仓/残仓/24h/终局。现有通用ProtectionManager/ProtectionLease仍HL全仓语义，**未改成Bulk可用**；普通BulkRules/HTTP仍拒绝stop。不能伪装whole_position或用此开发证明放宽资格。上轮原生SDK36与Linux279证据保持不变，当前无新用户资金请求、未重启任一运行金融服务。

# 最新：Bulk 原生止损编号/签名 Linux36实测通过，原生风控读取继续；真实资金仍未授权

本轮是实际progress，不是no-progress/blocked：编译官方Rust oracle并与Linux真实SDK对照，新增可移植编号桥、原生风险/费用解析和原生响应回归。get_goal 本轮实际 active（此前usageLimited为历史），目标仍完整的Bulk实盘上线，不得缩小为只读或离线测试。无AI定时/新金融writer/旧服务重启。

## 新完成与原始证据
- 隔离Mac Rust1.91.1官方源码/helper终于编译成功，`data/staging/bulk-native-identity-build-v2/native-build-v4.json` exit0（107s/offline/locked），binary SHA884c46ef27b500eedf267ec95f3aae80824e5b839be04532bdfcfb3560f1cdb4，native revision567dbc51a38c73d757ccb91547f197215b40ddc5，Cargo.lock已生成。v1/v2超时保留；v3固定源IPv4/TLS relay下载进展后aws-lc-sys半包exit101；随后用官方static.crates.io包按Cargo.lock SHA校验补齐113个，v4离线编译成功。所有下载/构建/临时relay进程均已退出；全局工具链/生产未改。Docker官方Rust镜像拉取两次EOF终态，无镜像/容器安装，不再盲目重试。
- 新 `BulkSdkStopIdentity` 从官方SDK产生的V4消息检查完整单stop ABI后，按官方Action.hash()的seq0+canonical+owner+nonce哈希，**不是整份签名消息哈希、手写序列化或客户编号截断**。官方Rust独立oracle持续保留用于验证，运行时不必额外Rust二进制/服务。默认native_identity仍None，不注册执行许可。修复SDK1e-8浮点str科学计数法进入plain DecimalText的桥接错误；所有金额先原生精确解析再canonical text，不接受精度丢失。
- Linux官方SDK原先12case对照已成功（`grid-bulk-stop-native-sdk-v2.json`）；首次v1仅隔离SDK venv缺cryptography，按入口既有固定48.0.1/cffi2.0.0/pycparser3.0补到SDK隔离venv后通过，未改生产依赖。随后**真实Linux可移植桥36case**通过：ETH/BTC/SOL×main/test×nonce0、2**53+7、2**64-2×两种size/trigger（含1e-8），逐字节匹配官方Rust canonical/OID并实际签名/篡改拒绝。`grid-bulk-native-risk-linux-v1.json` native_sdk_exit0。原request/金融发送0，独立临时无资金key仅在Linux `data/staging/bulk-stop-native-v1/ephemeral.key`0600，禁止打印/下载；不是用户key。公开向量/oracle在本机 `grid-bulk-stop-native-public-vectors-v2.json`、`grid-bulk-stop-native-oracle-v2.json`；源码工具check_bulk_stop_sdk.py可--portable。该工具没有财务HTTP出口。
- 新 `bulk_risk.py` 严格解析全部9个regime、原生[notional][leverage]网格、mmrO/mmrE/p时间衰减、完整相关矩阵PSD和组合维护/清算预留公式；不推导未认证的初始保证金容量。elapsed传入原生regimeDt计数（保留原协议tick单位，不用墙钟秒替换）。未覆盖网格坐标拒绝，不伪造/外推额度。主网leverage曲线实际是notional_0/notional_half，与OpenAPI margin_*不同，保留basis、拒绝混用。相关币名有kPEPE，不能全部转大写。当前普通市场规则仍只接受其既有大写scope，不悄悄拓展金融范围。
- 实际主网ETH/BTC riskSurfaces读取成功，`grid-bulk-risk-native-v1.json`。HTTP impact实查是**504网关超时，不是404/空曲线**；同官方WS `impact`订阅真实收到ETH/BTC曲线，实际payload为symbol/timestamp(ns)/full{buy,sell}/book{buy,sell}，与旧HTTP文档形态不同。`grid-bulk-impact-http-ws-v1.json`。新增 `BulkImpactObservation` 按实测WS合同解析、按持仓方向选择关闭边、250个log大小点取上界、拒绝过期/未来/未覆盖size；完整曲线不能默认0。只读探针10秒结束，不是持仓恢复/永久风险流。
- BulkReadTransport 新增只读feeTier白名单（symbol仅此类型允许），账户费用保留精确bps/有效maker/保守maker预留、返佣不当可用资金。首次真实fee quote被拒绝，实查发现scopeInstrument=null；现在必须另查同一MasterEOA完整账户，明确global tier/金额/指数/窗口匹配且无市场override，才认global，**不把null直接当global**。本轮新源真实读通用户maker0/taker.00035、14日窗口。`grid-bulk-native-risk-account-shape-v1.json`还实查到fullAccount.reserve各市场[reserve,rfactor]、marginBufferRate .05、账户leverageSettings；这些已采集但尚未接到完整组合预算/保护状态机。
- 当前Linux v2 **279pass/0fail/error/skip**，XML SHA be82567583358e48300f2f2b5fc1a60736ae4878d6a9ac95b250b51f678b837e，`grid-bulk-native-risk-linux-v2.json`；其中v1的272为旧费率处理版本，不相加。v2明确按三个SDK源码摘要未变复用v1真实36case签名证据（不是重跑），proof SHA eb98b786af2bf63add388f9f88be39a5e73693150fa325af898099bf64521b86。各轮测试初始fixture/精度错误原XML保留，新源码已修正。源码只在Linux独立 `data/staging/bulk-native-risk-v1/v2/source`，未部署任何此轮金融组件。

## 下一步仍必须做（不偷换上线目标）
1. 真实资金完整适配器/共享预算：利用刚读到的native reserve/buffer/fees及WS曲线补齐初始/逆向/挂单潜在暴露、杠杆曲线和同账户多币种预算。当前只是原生维护数学及解析，**还没有完整Bulk GridVenue、risk capacity或永久市场风险流**。不能用availableMargin、HL公式或零impact直接放行。
2. 固定数量stop覆盖与通用保护合同：现有ProtectionManager仍按HL全仓stop/whole_position处理，Bulk固定size不能冒充。须覆盖潜在开仓、归零再入场、触发后根单/子执行关系、未知结果原ID查证和残仓；普通BulkRules/HTTP仍拒绝stop，编号签名通过不等于保护已上线。
3. 出口跨服务限流、真实历史覆盖/资金费/断线及进程重启恢复、网站预览/完整生命周期、独立限定资金政策与小额验收、服务器24h及终局/备份。
4. 用户已保存独立主网账户，读取500.701USDC仅快照；**未授权Bulk资金额度/期限**。保持GRID0/无order_intents，不复用HL授权或套利凭证，不发问密钥，不在功能未准备好前要求资金批准。

下文“helper没有binary/Cargo.lock”“没有真实风险/WS/费率读取”等是历史，当前以上述实证为准。新源不得直接覆盖活动HL的66文件或旧套利根；它们保持原版本，Bulk只读入口仍之前已部署版本。

# 最新：Bulk 用户账户已保存，真实只读检查与消息订阅通过；实盘仍未开放

2026-10-25 用户回复“已保存并完成检查”。最初 GET 历史 verification 仍 unverified（`grid-bulk-account-connection-review-v1.json`），不能据此声称签名认证通过；随后对同一账户/凭证 revision 单次只读 verify HTTP200，已保存 pending：MasterEOA、主网、margin total/available500.701 USDC、used/buffered0、无仓位/挂单、12市场。只读公开接口不证明签名权限。证据 `data/run/grid-bulk-account-connection-verify-v2.json`。用户无需重新填写或发密钥；Bulk 额度/止损/期限尚未单独授权，不得使用全部余额或复用HL授权。目标未完成；get_goal 当前实际 usageLimited，不改成complete/blocked，不恢复AI定时。

新增未部署 `bulk_stream.py`、`bulk_market.py`：账户初始快照屏障、订阅重连/真实PING/PONG、精确WS成交与REST历史匹配、重复/乱序与队列溢出拒绝；WS isBuy是taker方向，REST isBuy是本账户方向，maker只在WS反转。partiallyFilled是终态，working可部分成交；已修bulk_read及旧fixture。流成交保持provisional，未经原生REST终态不能入账。行情严格源站/精度/过期校验；没有完整GridVenue、风险预算或恢复就绪放行，recovery_required仍true。

最新 Linux v1 291pass/0fail/error/skip，XML `grid-bulk-market-stream-linux-v1.xml` SHA e588bc510f1ee1fd92f4238a9f17966eb3773a44a3dabfa47ef65fe3a3d9c139；它包含修正前盘口毫秒假设，不冒称修复后全量。实际用户主网只读feed-v1已连账户流/ack/snapshot，但book不可用。真实GET证实主网盘口timestamp为纳秒（1792931881948315123），与OpenAPI毫秒标签冲突；按实测修正为保留原生ns，无猜测缩放，新捕获fixture。修复后本机/Linux68项通过/0skip，Linux v2 XML SHA490aa8d36ed12ff94dea4587ee4b6430d6f02d96188fbc41b9edc8a8b8d23059。主网只读feed-v2再次真实通过：mark2328.2305/bid2328.057/ask2328.058，均无unknown reason，账户流已connected/ack/snapshot。只是空账户连接证明，不是成交、权限、持仓保护或恢复证明。`grid-bulk-real-readonly-feed-v1/v2.json`、`grid-bulk-public-book-native-v2.json`，未产生金融发送或注册执行器。最早调试误写mainnet-api无数字1导致DNS失败；正确固定origin为mainnet-api1，不换源绕过校验。

原生止损编号桥仍未编译成功：隔离Mac Rust1.91.1已从官方校验归档安装，receipt `data/staging/bulk-native-identity-build-v2/install-cached-v3.json` exit0；全局/生产工具链未改。Cargo build-v1在600s超时exit-9、build-v2在300s超时exit-9，均只有registry/git更新，无binary/Cargo.lock，不可声称原生stop ID验证通过。v2会话35951已terminal0（内部build失败已写record）；此前62743亦terminal0。无活跃下载/构建遗留。原installer13367/5076已明确停止并保留exit-15；54915实际安装成功，不复投未终态进程。

Linux普通下单+撤单SDK v4八项签名通过，`grid-bulk-native-wire-linux-v4.json`，bulk_wire SHA7c361d7a2379dda5a4af584b515730522499b0300a1263637aaaa026f60e6dd7；不包含止损。Linux `data/staging/bulk-stop-native-v1/public-vectors.json` 12个stop prepare向量已生成并复制本机 `grid-bulk-stop-native-public-vectors-v1.json`，SDK均缺根ID；临时无资金key仅留同stage/ephemeral.key0600，禁止读取/下载或使用用户key。新工具 `planning/grid/tools/check_bulk_stop_sdk.py`尚未运行，等官方Rust helper真正成功/摘要/锁文件后先本机公共向量原生oracle，再Linux真实SDK签名对照。不要把VectorOracle映射当已经生成的官方证据。

仍需：官方止损ID/固定数量覆盖、Bulk原生组合保证金（riskSurfaces文档字段与OpenAPI存在语义差异，不用HL公式或availableMargin替代）、IP共享限流、完整GridVenue/风险保护/历史恢复/网站预览、独立有期限资金确认、真实闭环/带仓故障/残仓/24h和终局。源仅本机及Linux独立staging；新生产Bulk入口仍旧只读版/GRID0，用户凭证已保存但无资金授权，HL与旧套利保持原版本。

下文“尚无用户回复、账户0、无工具链”均为历史采样，以本段和原服务器实际为准。

# 当前任务：Bulk 实盘上线目标继续，独立账户入口已开放，实盘未开放

最新：已实际部署 [Bulk独立账户入口](https://taoli.coinfetcher.xyz/grid/bulk/#/grid/accounts)，使用原网格网站账号密码；内置浏览器Basic凭据报错，正常认证HTTP已200，提示用户用Chrome。已异步询问是否保存并检查；尚无回复。采样1792923492268账户/策略/订单意图均0，尚未使用用户真实账户/资金。目标 active；本轮为实际进展，不是等待或 blocked。完整要求/缺口 `planning/grid/BULK-LIVE-GOAL.zh-CN.md`。

新根 `/root/code/entropy-grid-workers/bulk`，独立 venv/SQLite/vault；服务 `entropy-grid-worker@bulk.service` PID3222057、`entropy-grid-bulk-web.service` PID3222058，GRID0。只读账户保存/检查不注册执行器；完整GridVenue/风险/保护/私有流尚未完成。Linux207执行相关通过；Linux44入口/隔离通过（有重叠）；本机最新110通过。公网认证page/scripts/status/venues/accounts200、匿名401、跨源写403、旧HL状态200。报告 `grid-bulk-entry-prepare-v1-output.txt / grid-bulk-entry-activate-v1.json / grid-bulk-entry-public-v1.json / grid-bulk-entry-final-v1.json`；新部署源来自已测Linux `bulk-execution-v1/source`，不包含随后条件单开发稿。

只增加 `/etc/nginx/entropy-grid-bulk-location.inc` 和原HTTPS server一行include，平滑reload；原配置备份在Bulk根data/run/nginx-https-before-bulk-v1.conf。首次 systemd-analyze验证模板默认实例i失败，未启动；按实际bulk实例0通过后续接同一预启动记录，失败记录保留。最终旧66源码完全匹配、HL2839364/watch2839366仍active/running、套利1223049存在，未重启它们/未改旧套利根。新服务只是观察入口，不代表允许Bulk实盘。

本机新增 `bulk_identity.py`、`BulkKeychain.prepare_stop`、官方pinned Rust `planning/grid/native/bulk-identity/` 是**未认证开发稿**：官方SDK0.1.30不算条件单根ID，拟使用官方Action.hash和序列化解决。隔离Mac Rust1.91.1下载300s超时进程已退出（35252 terminal1），无可用编译器、未编译helper/未获真实条件单ID证明；全局工具链和生产未改，部分缓存留在data/staging/bulk-native-identity-build-v1。其16项测试只证明信任拒绝边界，不代表原生止损可用。普通rules/HTTP仍拒绝保护单。旧SDK v3八项对应此前bulk_wire SHA7dab...；当前新增开发方法后不能冒称该报告验证了止损。

本轮全部本机SSH准备/测试/激活/公网/终检会话均已退出；未留下AI定时、Macwriter或Rust下载进程。用户保存账户后可先只读检查，额度/止损/期限仍需单独确认；在此之前继续无资金开发。下文“账户入口尚未部署”是此前阶段，被本段取代。

# 上一阶段：执行组件已实现，尚未开放实盘

2026-10-25 用户已确认独立主网账户准备好，当前询问需要做什么。账户尚未填入新服务，独立安全账户入口尚未部署；现在无需用户发送密钥、充值或手动开单。入口准备后再请用户在页面填写，实际资金验证前另行确认该账户额度、止损和期限，不能复用 HL 授权。

新增 `entropy_arb/grid/adapters/bulk_execution.py`，接上受最终风控回调控制的单笔 HTTP 提交及撤单，缺省拒绝发送；原始编号/nonce、MAY_HAVE_SENT 和未知结果查询仍持久绑定，撤单回执不代替原订单终态。`bulk_wire.py` 补充官方撤单签名、取消日志及同账户共享 nonce。**尚未注册 Bulk GridVenue 或接入真实网格资金循环，也未部署网站或生产服务。** 原生保护、Bulk 资金风险模型、私有流、带仓恢复及实际 24 小时验收仍未完成。

当前本机相关回归 **207 通过、0 失败**，原 XML `data/run/grid-bulk-execution-focused-v1.xml`；最新执行组件尚无 Linux 完整回归证明。Linux 独立 SDK v3 **8 项**普通单及撤单签名/防篡改验证通过，原 request_id `bulk-wire-linux-v3`，报告 `data/run/grid-bulk-native-wire-linux-v3.json`。两项原会话 19913/45740 均已退出 0，金融发送 0。保护单原生编号尚需解决：官方 0.1.30 SDK 不返回条件单预计算 order_id，不能编造或用普通单算法冒充。本轮目标保持 active，整个 Bulk 实盘上线尚未完成。以下旧记录反映前一阶段，不覆盖上述最新进度。

# 上一阶段：Bulk 独立推进，主网账户已准备，仍未授权使用资金或开放实盘

2026-10-25 用户明确“进行独立推进”，随后答复 Bulk 已有独立主网账户。**未给新资金额度/期限、未填写到新服务；本轮没有查询用户真实账户或发送任何交易。** 当前新增 `bulk_wire.py / bulk_read.py`：精确市场/限价GTC/ALO/IOC、官方SDK固定0.1.30、原客户编号→native OID及账户共用nonce的原子日志、原消息防篡改、发送前MAY_HAVE_SENT防重复、完整当前快照/原订单查证、slot分页及明确覆盖/留存证明、精确maker/taker费用返佣与资金费反号。保护/特殊账户/不明确金额拒绝，不套HL风险。账户提供者接到Bulk公开读取，主钱包凭证标签及不支持代理/子账户/逐仓原因明确，保存/检查不注册任何交易执行器。

本机最终175pass/0fail（Bulk新61+旧相关114）；Linux独立staging v3当前175pass/0fail/0error/0skip，旧169/170原报告保留。Linux SDK独立venv真实库2域×3政策6项签名/单位/消息篡改检查通过，交易发送0；原探针v1测试误用枚举/字段位置，按官方源码修正新v2并保留失败。主网实际公开23项/12TRADING规则通过，临时无资金地址真实ACCOUNT_NOT_FOUND仅是负面接口证据，不能当仓位/恢复证明。报告 `data/run/grid-bulk-native-wire-linux-v2.json / grid-bulk-linux-focused-v3.json / grid-bulk-public-read-v1.json`，实施和下一步 `planning/grid/BULK-IMPLEMENTATION.zh-CN.md`。源码检查点 `data/run/grid-bulk-source-checkpoint.json` 是独立增量，不能当部署安装器。

线上只读隔离核对1792920244297：原66网格源文件完全匹配，runtime2839364/watch2839366/旧套利1223049均active/running且未重启；没有本轮Native仓位/盈亏审计。新工作仅 `/root/code/entropy-grid/data/staging/bulk-*`（SDKvenv和源码/测试），未安装生产源码/虚拟环境、未更新旧套利目录或线上网站、未开启新writer或Mac实盘、未恢复AI定时。**仍无完整Bulk GridVenue/真实HTTP执行出口、原生止损/风险模型/私有流和带仓恢复，实盘关闭。** 下一步先补这些能力及独立安全账户入口，再请用户页面填凭证并明确该账户单独资金期限；不复用HL授权、不盲目重派UNKNOWN。Lighter独立账户未备的事实保留，不因此阻塞Bulk。整个多所目标未完成。

## 以下为 Lighter 只读层交付记录

Bulk 收尾补记：Linux175最终XML已复制并按v3摘要核对；Native SDK v2对应bulk_wire源码SHA与当前文件一致。再次只读1792920554931原66生产源、runtime2839364/watch2839366/旧套利1223049仍未变且运行，证据 `grid-bulk-production-isolation-final.json`。本轮全部Linux准备/测试和Mac取证会话均已退出，无新常驻writer或AI定时。不可把独立源码增量包直接覆盖生产/旧套利。

2026-10-25 用户已回答独立 Lighter 账户“尚未准备”。本轮继续无资金开发，新增 `adapters/lighter_read.py`，包含独立私有 GET 白名单、精确订单/成交/原始资金费、原持久客户编号、双快照夹住历史查证、账户/部署/市场/类别/窗口绑定的可保存分页、重复/冲突/循环检查。原订单离开挂单簿但历史尚缺时不能重用旧OPEN或认定不存在。费用/funding sign/finality/retention真实未验证，资金费不入账，历史complete始终false且不推进覆盖。**非GridVenue、未注册、无真实只读token或交易发送**。`observation.py`仅将每个实例白名单独立存储，公开探测权限未扩展。新测试54case加旧相关60共本机114pass/0fail；XML data/run/grid-lighter-read-focused-v1.xml，没有新Linux全量实测。详细交付及待验收项 `planning/grid/LIGHTER-READ-EVIDENCE.zh-CN.md`。本轮无SSH/部署/服务重启/资金请求/定时变更，不把上轮生产采样当当前状态。下文v2是上一批快照，当前读取层另记检查点；旧快照保持不可变。下一步真实权限与精确费用/历史覆盖、nonce/execution/risk/protection等仍须完成，不能因账户未备而复用旧套利或HL资金授权。

## 以下为多交易所第一批交付记录

2026-10-25 用户明确要求实施已确认的“现有交易所尽量全覆盖、先基础网格”方案。本批已完成独立网格交易所目录10项、加密多类型接入配置、公开目录/只读检查、单网站多Unix RPC路由及离线/未知请求/归属冲突保护、动态凭证与列表筛选界面、交易所自有保证金容量接口、原生编号上界及同事务绑定、Lighter精确编码/GTT期限与Linux原生签名库验证。**其他所尚无完整GridVenue、订单/成交/资金费回补、风险模型、保护和恢复认证，不可将目录或SDK检查当实盘可用。** 原保护/资格中仍有HL专用部分待拆。新账户不注册到交易引擎，不能复用HL资金政策。

实现记录 `planning/grid/MULTIVENUE-IMPLEMENTATION.zh-CN.md`。当前不可变源码包 **data/grid-code-checkpoints/grid-code-20261025-multivenue-foundation-v2.tar.gz SHA99faf18c6060a70cad39c1f369523f8091b5cb14e53b17f137a8b1eab75f8822**，398源文件，qualifiedfalse/deployedfalse；v1原包769a7af92588ad30f31d5b4b6c599da2b61314e470347e7cc4e83467d05726ba保留。v2仅修新接入的公开读不等于私钥授权状态及页面、增加相应测试。

证据：本机网格全量1914通过、后续专项60通过（有重叠）；Node既有24通过/修改页语法通过；浏览器HL/Lighter/Backpack实际字段及文档切换，截图planning/grid/screenshots/multivenue-accounts-review.png。Linux v1 **1921pass/0fail/0skip/251.32s/原exit0**，最初SSH取结果420s超时，已按原request_id查原result+原XML确认完成，无重派；原timeout audit保留。Linux v2当前专项 **60pass/0fail/0skip**，不冒称v2一次全量重跑。原XML及报告data/run/grid-multivenue-linux-offline-check.json、grid-multivenue-linux-focused-v2-check.json。Linux回归仅独立staging、外网和生产Unix socket禁连、低优先级，无真实交易；临时Mac UI空账户RPC无执行循环，页面/服务均已关闭（第二次主动SIGINT为预期terminal130）。

Lighter SDK固定a7a9ba88514d38a863a33642acd41f8adf442e5c/1.1.8，Linux x86_64 Python3.11.6独立venv安装及原生库/本地keygen通过，42依赖冻结；LighterWire当前SHA444ed2b460b22d40c51d5ffb7909865f09ca9b63816ffb44d59b51455425c788实际原生签名序列化字段核对通过，**发送交易0**。仅放在data/staging/lighter-sdk-a7...及multivenue-foundation-v1-769a7af9/v2-99faf18c，不改生产venv。公开市场8项实际读通；Backpack本机连接失败、Lighter-RH本机超时，保持未验证，不能推断其交易所本身不可用。读失败及修复后的原报告均保留，汇总grid-multivenue-public-audit-latest.json。

线上隔离只读核验1792900471738：原网格66源码逐项匹配旧funding-send版本，runtime2839364/watch2839366/旧套利1223049仍运行未重启；证据grid-multivenue-production-isolation-check.json。**未部署网站、未启用新worker、未git提交/推送、未重启Mac实盘、未恢复AI定时**。旧工作区大量套利改动保持原样。原v19 g-490d1620d25b421a的观察、原24h/25h期限保持；下文08:50是那次策略状态采样，不可当作当前行情/盈亏。

下一步继续Lighter完整真实适配器、私钥权限、终态/历史/资金费、风控/保护/带仓恢复及独立资金期限验收，再逐家Katana/Backpack/Bulk/Ondo/Arcus及特殊部署。已异步询问用户是否准备Lighter独立测试网/主网账户，尚无回复；不能假定有新资金授权或自动复用旧套利凭证。密钥只填之后独立账户页面，不发聊天。当前线上HL观察期间不覆盖其代码。所有本批本机工作会话已完成；Linux原测试进程已退出。整个多所产品目标未完成。

收尾追加只读核验1792901941928：原线上66源文件仍完全一致，runtime2839364/watch2839366/旧套利1223049均active/running且PID未变；原策略SQL为RUNNING/ACTIVE/e9、stop_reason=null。该检查没有重新进行Native仓位/挂单/盈亏审计，不引用08:50的估值作当前值。证据data/run/grid-multivenue-production-isolation-final.json。全部本机测试、证据复制及只读会话均已结束。

## 以下为原 v19 实盘状态及历史记录

# 最新 v19 08:50 状态：RUNNING，3次受保护恢复，真实仓位/挂单一致

2026-10-25 08:50:37北京时间用户仅询问状态，本次只读。原g-490d1620d25b421a / ordinary-observe-linux-20261025-v19为RUNNING/ACTIVE/e7，原start成功，已观察约7h27m。3次恢复121.082/181.352/125.983秒均attempt_ok/reconciliation_ok，均≤原300s；无当前恢复/stop_reason。独立Native当前.0156ETH、4普通网格单+原全仓stop OPEN/2247；SQL前后稳定、仓位及所有挂单CID/OID匹配。按Native对应mark2360.9账本估值净+.642072USDC（含浮盈、手续费和资金费），无完整网格配对，勿称已实现收益。detail初次缓存滞后，当前估值使用独立Native对应mark。risk_watch仍RISK_API_BUDGET_WAIT。原controller/finalizer/runtime/watchdog运行、原deadline等待，旧套利PID1223049未变。

按已接受安全恢复口径，旧observation_invalidated/soak问题字段保留，不单凭其false判失败；尚未24h/未授予正式资格。原起点10月25日01:23:26.068、最早24h为10月26日01:23:26.068（建议01:30后查），原25h截止10月26日02:17:23.844均不变。不重启/不重复create/start、不延长、AI定时仍暂停。新报告planning/grid/reports/2026-10-25-grid-v19-interim-status.md；新证据data/run/grid-v19-current-status.json / grid-v19-latest-progress.json。所有本机只读会话均terminal0，无Macwriter。

## 以下为启动时历史记录

# 最新 v19 已 RUNNING，真实正仓/4格+原生止损独立核验通过

**当前 gid g-490d1620d25b421a / RID ordinary-observe-linux-20261025-v19 / RUNNING-ACTIVE-e1**；原start **b43fac3e-d865-4546-b33b-6af1eec8a6f5 succeeded**，原cutover三阶段全0/finishedtrue，不能再次dispatch/create/start。Native正向proof真实 **.0156ETH/cross3/5working（4grid+原全仓stop OPEN触发2247）/gross48.2352≤150/sampling1**。01:26:20再次独立Native确认SQL前后完全相同、仓位精确匹配、所有当前CID/OID相符；soakissues{}、无恢复、无stop，riskwatch仍RISK_API_BUDGET_WAIT提示，不据此宣称以后绝无问题。

配置 **2266.1–2555.7/4格/ETHlong/3cross/GTC/40含20reserve/stop2247/150gross/原30s保护**；宽289.6约原100的3倍，主要向上扩展，保留原3%止损。实际观察起点 **2026-10-25 01:23:26.068（1792862606068）**，最早满24h **10月26日01:23:26.068**，建议用户 **10月26日01:30后**检查原状态及终局；原25h独立资金截止 **10月26日02:17:23.844**，合同SHA **9e7cb2f314be300f2c1946eb8793c844bde7f9e67a112c05726af5463e0bf13e**，timerwaiting，不重置/不延长。按用户新口径允许受保护安全恢复，严格ordinary_24h字段仍保留原定义，恢复后false不能单独作产品否决；仍需真实24h+每次≤300秒安全证据+Native/账本/原请求终态/备份。未达24h，未授予正式资格，不标整个产品完成。

当前runtime **2839364/watch2839366** fresh同instance健康，旧套利 **1223049未动**。实际public匿名401/认证page+metrics+新detail200 production/GRID1/RUNNING.0156，原登录不变；没有额外持续UI负载，不冒称负载通过。新网址 **https://taoli.coinfetcher.xyz/grid/#/grid/g-490d1620d25b421a**，已请求app打开queued。本机启动watch **77375 terminal0**，public61500/final92891/current15678均terminal0；旧证据复制27638已terminal0，新8份证据复制31279已terminal0，无本机运行任务。AI定时/goal继续暂停，服务器原控制器/观察/终局/期限自行执行，无Mac网格writer。

报告 **planning/grid/reports/2026-10-25-grid-v19-wide-live-start.md**。原STATE/soak/Native-running/cutover/install/deadline/review及final/current/public证据已准备下载，本次当前currentstatus在data/run/grid-v19-current-status.json，后续检查先fresh核原v19，不读较早v17/v18当现状。原v18资金启动失败已完整CLOSED/原cleanup成功、Native+backup+contain完成，净-.075094，具体历史指纹变化未唯一归因资金费。新funding窄修复已通过fixture竞态case，不虚称本轮已真实碰到资金费发送竞态或持仓故障。

## 以下为 v19 原提交及修复过程记录

新核心已实际安装 **grid-funding-send-install-proof.json installedtrue/finishedtrue/legacy_unchangedtrue**，包SHA2d8ae7b4b695fc535c6ad5bc03874ac17a4b7aea69205811400e79f38688b4b5、327files、仅2core+1test+manifest安装/保留rollback。实际新源Linux **1652pass/1deselected/0skip/302.29s**，唯一排除的原十万性能case和20console依赖SHA逐一保持一致且该原case已按原标准独立通过，组合1653/0skip，新reviewed XML **SHA7cfd9ee875cde48250d13a0c99725a21fca26cb36078088d105e5feafed804b4**，info明示reused_unchanged_performance_case/依赖SHA及两原XML，不冒称1653一次全在新进程重跑。原quote首次性能失败及独立复核证据保留。

**v19 RID ordinary-observe-linux-20261025-v19 / entropy-grid-ordinary-linux-v19-cutover.service** 已唯一dispatch1792862170821。本机只读watch **session77375** 每15s/600s有界运行，继续查原返回；绝对不要再次dispatch/create/start。新review SHA **58b2450c3c1a87e67d2386bb192beb76c889c199ee777fbd22ad273945a88d91**，控制器/deadline/Linux155+workflow50/HTTP6/guard7/installed-executor/systemd全过，66源与currentfundingversion匹配。新controller **edea7dc899ada0bfa8d4a98a8e90da366abc0c31a88a85df3bde0f6635987026**；deadline83ae9ee06d2167f66cbfc08574e511f626f4759dfa9832335a25b1d2344faf38。仍4格/宽12.5%（lower*.978/upper+mark*.125）/40含20reserve/3cross/原3%stop/150/30s/300s恢复/25h。下一步等实际gid/原start终态/Native正仓4普通+stop核验成功，跑public/finalcheck/currentstatus并保存原deadline起点准确CST。`grid-v19-public-check.py`当前含旧g311地址，须在新gid确定后改；currentstatus已动态读v19原STATE。尚不能汇报已RUNNING。AI定时暂停，不启动Macwriter，旧套利1223049未动。

## 以下为此前 v18 收尾与 v19 修复过程

**先不要复用v18或v17身份。** v18实际 gid g-31111fb71bab44fa 在STARTING已建仓、仅1普通单后，01:00:34发生 `RISK_OBSERVATION_CHANGED`→risk_paused/e2，原start41ecc013...failed/ORDER_NOT_CONFIRMED。原唯一cleanup **95aeb650-d88c-4b88-b727-8c7e2ee8e387 succeeded/remaining0**，CLOSED/e3，原finalizer Native+backup全0。原watch96646已terminal1，不应再poll；本轮未进入正式观察。已 `grid-ordinary-linux-v18-contain.py` freshNativeflat+backup/清grant/GRID0/停仅v18timer成功；旧套利PID1223049未动，当前runtime2834695/watch2834697。v18的快照具体变化未留部件诊断，不唯一断言资金费根因。

已用真实executor/risk+fixtures复现“资金费在journal或签名后入账导致未发送订单误整轮暂停”。新修复严格仅在 **funding表变化、全部其他语义指纹（预算/订单/仓位/账户/历史/保护/stream）一致** 才返回RISK_FUNDING_CHANGED_WAIT，且只在明证NOT_SENT的预发送门或final_gate_not_sent恢复原同CID到PLANNED，再做全新风控观察；不跳过风控，不对UNKNOWN/native rejection复用，旧预算/历史后退等仍按原拒绝。新增4case涵盖两时序以及新观察预算不足仍拒绝。未知指纹变化仅记固定部件名（无值/账号/密钥）。本地94风险/执行/保护cases通过，v19私有本地100/Linus155亦通过。

新不可变核心包 **data/grid-code-checkpoints/grid-code-20261025-funding-send-v1.tar.gz SHA2d8ae7b4b695fc535c6ad5bc03874ac17a4b7aea69205811400e79f38688b4b5**、327文件，相比已安装quote仅 `live_risk.py / executor.py / test_grid_live_risk.py`。远端stage funding-send-v1-stage；`entropy-grid-funding-send-linux-complete.service` 正在跑**1652个新源case**（排除原十万记录性能case），上次进度47%。原性能case及20个console依赖SHA与前已通过版本逐个完全相同，安装器将新1652通过+原性能独立1通过按唯一个案组成1653/0skip，明确记录reused_unchanged_performance_case和依赖SHA，不重跑无关耗时基准、不弱化标准。原quote首轮性能失败及复核通过XML保持原貌。

`grid-install-funding-send-v1.py`已上传未执行，要求新1652实过、oldperformance1及依赖SHA一致、v18contain完成/flat/GRID0/原66source匹配，备份并停独立writer后只安装2core+1test+manifest/可rollback，生成 **grid-funding-send-install-proof.json / grid-funding-send-version-proof.json / grid-funding-send-linux-reviewed.xml**。完成后执行 `data/run/grid-v19-prepare-workflow.py`（未执行，必须等installedversion）；Linux155已准备 `grid-recovery-supervision-v19-install-proof.json`（controllerSHAedea7dc899ada0bfa8d4a98a8e90da366abc0c31a88a85df3bde0f6635987026）。新RID **ordinary-observe-linux-20261025-v19**，仍4格、12.5%宽、40含20reserve/3cross/原3%stop/150gross/30s/300恢复/25h。新review生成后替换 `grid-v19-dispatch.py` 内旧070fef...reviewSHA为真实新值，再唯一dispatch。previous核验已改v18正式Native+backup及contain，不能复用失败轮。所有workflow/native/progress/watch/public/current-status已从v18备好；public-check仍含g311旧gid，必须新真实gid后改，动态current-status从原STATE取gid。AI定时暂停，无Mac执行，旧套利不动。

## 以下为较早 v18 原启动记录，现已被上段取代

**v18 RID ordinary-observe-linux-20261025-v18 / entropy-grid-ordinary-linux-v18-cutover.service** 已在1792860920096唯一执行 `grid-v18-dispatch.py`；本机只读watch session96646正在每15秒核原身份（600秒有界，资金执行在Linux），不要再次dispatch/create/start。新review SHA **070fefcf6b372740dab8b727a6dd240da0d7b733a07419c15e97708ea8019c00**，155监督+50workflow/HTTP6/guard7/systemd/installedexecutor全过，66公共SHA匹配当前quote-freshness版本。prep首次只因preflight旧151计数没同步新增4case而拒绝，仍无v18资金；只修未激活的preflight及fixture至严格155，经本地50+Linux50重新检查，原同一准备流程 `grid-v18-prepare-workflow-resume.py` 验每个已写文件精确一致后生成review；不是弱化/跳过检查。controllerSHA **4ebed2211ff3053c1e6f3b4877ab29ee0aacbd5c8ca83a5b264ad759321f17df**；deadlineSHA b1eafc1af16fef7fd789b2b5f4d2aa803fcc278fa8715dd34065b63a8f9f6eb0。最新watch还未见gid，需继续检查而非结束“已提交”。`grid-v18-public-check.py`还含旧g75地址，必须在真实gid出现后改；`grid-v18-current-status.py`已改从原STATE动态取gid，只在Native-running-proof存在后用。完成Native4grid+stop正仓、publicHTTP、原期限/健康/旧套利PID核验，再写准确运行时间和链接。AI定时保持暂停。证据下载session7434正在执行14个原证据文件复制，查看原返回。

## 本次较早准备记录

最新：核心quote-freshness已安装成功（GRID0安装proof/rollback/旧套利1223049未变）。Linux首轮1648pass+原未改动性能case独立复核1pass共1649成功、0skip，原失败XML保留及新reviewed组合SHA f99cd3efb375195ed5736b7a371742ed8cfa2c4589eeda42ffa29d249d6eb4ca。v17cutover唯一已执行但在create前真实preview拒绝：5格宽12.5%无法满足原预算留白+最低下单金额，plan=null旧controller索引导致TypeError；financial_start_submittedfalse/gidnull，无deadline。原记录保留，已 `grid-ordinary-linux-v17-contain.py` fresh11DEXflat+backup/清grant/GRID0/failed_before_creation=true 结束，不能复用v17身份或冒称资金失败。真实只读对比 `grid-wide-range-readonly-previews.json`：相同12.5%宽、4格VALID/planliveeligibletrue，5格7%宽也预算不满足。用户已被告知改4格，资金/止损不变。

**新RID ordinary-observe-linux-20261025-v18**，4格，lower=mark*.978/upper=lower+mark*.125、原40含20reserve/3cross/GTC/stopmark*.97/150/30s/25h。新controller显式拒绝空/无效/未授权preview，不产生TypeError、更不会继续create。controller/deadline本地100及实际Linux155过，proof `grid-recovery-supervision-v18-install-proof.json` / controllerSHA4ebed2211ff3053c1e6f3b4877ab29ee0aacbd5c8ca83a5b264ad759321f17df。核心源不变不重跑全量。`data/run/grid-v18-prepare-workflow.py` 正在执行（本机session8124需查看），生成新review后把 `grid-v18-dispatch.py` 内9899c70...旧v17review SHA更新为新实际review SHA；dispatch的previous校验已改为v17failed_before_creation+financialfalse，不冒造v17终局资金审计。随后唯一dispatch，查原资金身份，必须实际Native正仓/4普通+stop通过才结束。`grid-v18-watch-start.py` / `grid-v18-progress.py` / `grid-v18-final-start-check.py` / `grid-v18-public-check.py` / `grid-v18-current-status.py`已备好；public-check里旧gid需在实际gid确定后替换，别误查g75。AI定时暂停、无Mac网格writer，旧套利不动。

## 以下为本次较早准备记录，v17部分已由以上段取代

2026-10-25 用户要求“继续启动实盘，可以把价格范围设置大一些”。本次已通过正常RPC stop_flat新唯一终态核验请求 `ordinary-observe-linux-20261024-v16-finalize-flat-20261025` / operation `900009e5-2ae4-4ea8-80ac-76f4a0bbeed7 succeeded/remaining0` 完成旧v16 CLOSED/e7；前置Native/SQL零仓零单，没有重复原partial请求。原finalizer已Native0+backup0/finishedtrue。`grid-ordinary-linux-v16-contain.py` 已成功生成containfinishedtrue/清grant/GRID0/停仅v16timer，旧套利1223049未动。原STATE finishedfalse保留原貌，contain改为实际controller进程0+正式finalization完成校验，不伪造历史。

局部修复：`HyperliquidGridVenue.execution_market_snapshot` 在WS返回过期book（仍原-1000..5000ms）时只读获取一次REST新观察；scope/rate错误不绕过；两个通道都过期仍原保护拒绝，触及止损仍退出。针对43测试已过，2个新增case。新immutable包 `data/grid-code-checkpoints/grid-code-20261025-quote-freshness-v1.tar.gz` SHA803703c8700655dee654ca37210fa354b287125dd6bc7a56fbfca5a703d2dd9f，327files，仅adapter+2tests变化。实际Linux `entropy-grid-quote-freshness-linux-full.service` 正在quote-freshness-v1-stage全量tests+planning/grid/review回归，期望1649/0skip，须查实际XML/退出，不直接安装。

已准备 `grid-install-quote-freshness-v1.py` 并上传但未执行（要求Linux1649/flat/contained/GRID0/备份后仅安装1core+2test+manifest，rollback）。新v17 controller/deadline已通过Linux151且准备到data/run；`grid-recovery-supervision-v17-install-proof.json`。新controller价格lower=mark*.978 / upper=lower+mark*.125，总宽12.5%、mark在首格内部（约-2.2%到+10.3%），原40capital含20reserve/3cross/5grids/GTC/3%下止损/150gross/30s保护/25h期限不变；本地controller/deadline/workflow96过。`grid-v17-prepare-workflow.py`、preflight/install/cutover/native-check等已从v16准备，绑定新quote-freshness版本/1649，不改旧source证据。下一步Linuxfull完成后运行原新installer；成功后运行 `data/run/grid-v17-prepare-workflow.py`（新review绑定hash），将dispatch中旧review SHA换成实际新review SHA，然后唯一执行 `data/run/grid-v17-dispatch.py`，RID **ordinary-observe-linux-20261025-v17**，只查原资金身份直到Native正向proof与实际5grid+stop。`grid-v17-progress.py` / `grid-v17-public-check.py` / `grid-v17-final-start-check.py`已备好。不得重复创建/下单，AI定时保持暂停。最后只读core回归进度56%，未查到最终结果。

**回归更新：** 原Linux全量1649中1648过，唯一失败是未改动的 `tests.test_performance_api::test_100k_event_performance` compute10.8459s超过原10s（query1.422/convert4.238/insert175.86）；原full.xml/log/failedunit保留，严禁假报全过。实际 `entropy-grid-quote-freshness-linux-performance-recheck.service` 正在相同不可变source单独复核这个原case，阈值不改。新 `grid-install-quote-freshness-v1-r2.py` 已上传但未执行；只有复核原case通过，才把原1648pass+单独1pass按唯一case身份组合到新 `grid-quote-freshness-linux-reviewed.xml`，原full.xml保持失败原貌，info含first_full_attempt_failed1/isolatedrecheck及两原XML SHA。v17 preflight及测试路径已改为reviewed.xml，本地50再次过；旧r1 installer从未提交。若复核失败，不循环刷过、不弱化原标准。只读真实行情新stage验证两通道age731ms/238ms，`data/run/grid-quote-freshness-native-read.json`，不是持仓恢复证明。最后新trial未提交，controller/deadline prepared已151，quote新core尚未安装，GRID0/旧套利1223049。

## 以下为此前状态检查历史

# v16 状态（2026-10-25 00:27）：实际平仓，保护行情失效退出及终态待修复

用户“看一下现在的情况”本次仅只读；未部署/重启/新开轮次/重投资金请求。原g-75c77a49beb44fd3 当前NEEDS_ATTENTION/STOPPED/e6，观察从10月24日18:40到23:10约4h30，未满足新口径24h。23:10:21保护行情PROTECTION_QUOTE_STALE→PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE，不是因零断线指标失败而退出。原保护close ae908193-0f1a-4b00-818c-d62cbb6eff95 cancel_orders/partial/reconciliation blocked/5个FOREIGN_OPEN_ORDER；原cleanup cd4884f4-5c0d-43c0-9011-f5ced6869ea0 已实际平掉.022ETH@2317.8、remaining0，但平仓后一次POSITION_MISMATCH使操作partial/策略未CLOSED。错误码不能直接当用户外部订单或既定最终仓差根因；具体行情刷新和退出对账原因待定位。

**本次独立Native11DEX全部零仓零单，SQL前后稳定零库存；6笔真实成交逐笔匹配、5笔资金费按ms及反号匹配，净-.797861USDC（fees.036964/fundingcost.001017），无网格完整配对。** 原止损584504244369在平仓时reduceOnlyCanceled。原finalizer等待CLOSED，正式终局/备份未完成；controller inactive，原25hdeadline仍waiting，不冒称终局通过。旧套利PID1223049未动，AI定时保持暂停。报告 `planning/grid/reports/2026-10-25-grid-v16-stopped-status.md`，新证据 `data/run/grid-v16-stop-investigation.json` / `grid-v16-native-stopped-audit.json` 及更新progress/current-status。下一步必要修复行情刷新/退出对账/终态收尾，不重复下平仓单。本次全部工具会话terminal0。

## 最新用户验收决定仍有效：允许受保护的断线恢复

2026-10-24 用户明确接受断线，要求恢复后尽可能正确续跑。新验收说明见 `planning/grid/RECOVERY-ACCEPTANCE.zh-CN.md`：同轮普通实盘至少24h，允许有记录、受保护、对账通过且每次≤原300s的恢复；报告次数/各次及累计耗时，不冒称恢复时间为正常交易。保护、身份、仓位/账本、重复下单、超时及终局独立Native/备份要求保留。现有严格零断线字段保持原义及历史值，其false不再单独作为新的产品上线否决条件；新的恢复验收结果须单独按完整证据评估，当前未生成/未授予正式资格。原v16继续原观察/原25h资金期限，不为此重启/部署/开新轮，不恢复AI定时。下文旧“连续24h失败”等仅指旧严格指标，不再代表新的产品决定。

## 最近一次实际 v16 状态核查（21:53，只读）

北京时间 2026-10-24 21:53 用户只要求查看状态，本次仅只读。原 `g-75c77a49beb44fd3 / ordinary-observe-linux-20261024-v16` 当前 RUNNING/ACTIVE/e3，原 start succeeded。21:26:27 CLOSE1000，监督器记录约181.454秒恢复，5张网格单已重建；独立Native当前.0088ETH、5张普通单+原止损OPEN，SQL前后稳定且仓位/挂单CID-OID吻合。原止损584504244369/触发2299未变。当前无完成网格配对，21:53附近净盈亏约-.030837USDC（含浮动、手续费、资金费）。risk_watch仍RISK_API_BUDGET_WAIT，诊断采样used268/900、无cooldown/锁占用、last_rate_limitnull，不据此宣称全部正常。首次UI缓存旧，背景刷新后行情更新，勿引用旧估值。

**supervision.observation_invalidated=true；严格最长9980.046秒，约2h46m20s，不满足连续24h。下方旧文字的“待明日达到24h”已被本段取代。** 不拼接窗口、不延长原期限、不自动新开一轮。原25h截止仍2026-10-25 19:33:02.799，原controller/observer/finalizer及timer继续，旧套利PID1223049未动，AI定时保持暂停。报告 `planning/grid/reports/2026-10-24-grid-v16-interim-status.md`；本次本地只读证据 `data/run/grid-v16-current-status.json`、`grid-v16-current-diagnostics.json`、`grid-v16-current-display-check.json`、`grid-v16-latest-progress.json`。本次本地会话均terminal0，无后台本机writer。

## 以下为启动时历史记录（最新状态以上段为准）

用户“进行问题修复，目标还是正式上线”本次已落实7生产文件修复/真实Linux1647全过/实际安装，并完成新v16资金启动及独立Native正仓核验。**g-75c77a49beb44fd3 / ordinary-observe-linux-20261024-v16 / RUNNING-ACTIVE-e1**。原start **5dd562e4-343c-46d2-9992-08e372dc7eb1 succeeded**；原cutoverfinishedtrue、freshformal/installer/Native-running全0。不能再次dispatch/create/start。原v14和v15失败/cleanup/contain全部保留，不复用观察或资金身份。

**实际严格起点北京时间2026-10-24 18:40:05.516（1792838405516）**；最早24h **2026-10-25 18:40:05.516**，建议用户 **10月25日19:00后**再查真实连续窗口+终局Native/账本/备份。原25h截止 **2026-10-25 19:33:02.799**，timer entropy-grid-ordinary-deadline-linux-v16.timer activewaiting，合同SHA **f7fd245fd6479460cfe3d628092dc1524c56ae8759c4f81d8e8bb52d330a78db**，不延长/复用。服务器原controller/observer/finalizer/期限自行运行；AI heartbeat/goal维持paused，未新增AI定时，无Macwriter。

独立Native正向证明 **grid-ordinary-live-linux-v16-native-running-proof.json**：真实 **.0088ETH/cross3/6working orders（5grid+原全仓NativeSTOP OPEN）/真实非reduce entry/gross52.151880≤150**，原CID/OID及参数、scope/epoch、SQL前后相同/Native精确仓位/guardian一致均通过；本次sampling_attempts=1，未触发在线重采样。40capital含20reserve/long/GTC5格、原30sNative/600启动+600稳定/连续86400不变，新配置仅居中mark-50到mark+50，避免mark自身恰在买单阈值；不是强行补单或减弱核验。

最后只读 **data/run/grid-v16-final-start-review.json checked1792838514377（18:41:54）**：RUNNING/e1/start succeeded、无未决，soak最长105.757s/issues{}、ordinary24false。runtime **2686694/watch2686696** 同instance健康新鲜；旧套利 **1223049** 全程未动。实际public **grid-v16-public-access-check.json** 匿名401/认证page200/metrics200production-GRID1/detail200RUNNING-net.0088。入口 https://taoli.coinfetcher.xyz/grid/#/grid/g-75c77a49beb44fd3 已请求app打开queued。原登录不变，不要重发密码。未额外跑连续UI负载，不冒称负载全200。

源仍327包 **SHA59d3753bacb37b3ebaaf431eb47c80c1678873f174858039343f0366ec722559** / actualLinux **1647pass0skip** 合并XML **SHAbcd43e52492891c52318b27344d58a523e6a3357928c2482ad943874a1ec8084** / **grid-batch-recovery-version-proof.json 66source/13delta/qualifiedfalse**。不重复全量或安装。新v16Linux监督151（新增居中case）XML **SHA9fa43789c9457c7374d214d2cc995576ef216e4e6369f8fbb72123121b88b6bd** +workflow50/HTTP6/guard7/systemd/installedexecutor全0；review **SHA7c39ee16768aca88c2cc9225fd391380213271dbf147e4db8ca81563454cffa3** 绑定12private和当前source，controllerSHA **eeeac9f6374f61a1df03eb6cfbd8ec00d601fbcba4e668cd6b5c986cb4a6373f**；shared监督eb7...字节未变。上游周期性CLOSE1000仍未唯一确认，实际新持仓恢复及24h仍待，不声称正式认证全部成功。

原v15因最后一格2365.4始终未触发，无SENT/cidnull/armedfalse；Native原600窗NO_STABLE_ORDINARY_RUNNING_WINDOW，原cleanup **4e1d956b-553d-4812-b70f-8463f138d5dc succeeded/remaining0**、CLOSED/e2、Native+backup全0、containfinishedGRID0/grants[]/停仅v15timer，净-.01961USDC/grid_pair0。没有弱化待发/未知门槛，v16改新原配置居中且Linux151+50重新验证；其原controllerSafetyEvidenceFailed是cleanup后记录，不能补造独立故障。v15所有原STATE/终局/contain证据已复制，本机watch49631 terminal0。

最新完整报告 **planning/grid/reports/2026-10-24-grid-batch-recovery-release.md**。新v16STATE/Native/soak/cutover/install/原deadline/review/151XML/50XML均已scp，本机exec **94896已terminal0**，本机只读watch **52565已terminal0**；最后public79639和finalcheck均terminal0。后续用户检查先fresh查原v16状态/严格soak/原cleanup/finalizer/终局Native+backup/原期限，未知结果先查原RID不POST。只有真实连续86400+完整终局Native全flat/账本+备份才推进阶段资格，不把离线/空仓当实际持仓证明，不标整个多交易所产品完成。

以下v16“尚待启动”及v15/v14启动文字均历史，已被本段取代。

# 最新：v15因价格条件窗口未满足已完整收尾；v16新参考价居中已唯一提交，继续只查原请求

本轮用户修复/正式上线工作仍进行，**不应终止于STARTING或未核验持仓**。v15 g-369b1ba6c19d4b2c 曾RUNNING/e1/真实.0088ETH/stopOPEN，但最后一格2365.4的GTC未触发（PLANNED/client_idnull/无SENT/armedfalse）；参考价原mark-40恰落网格线，启动轻微跌价即等条件。原Nativechecker到原600s运行窗后NO_STABLE_ORDINARY_RUNNING_WINDOW，**没有放宽未知/待发门槛**。cutover原cleanup **4e1d956b-553d-4812-b70f-8463f138d5dc succeeded/remaining0**，CLOSED/e2；原controller随后SUPERVISION_SAFETY_EVIDENCE_FAILED是该收尾影响，不补造独立runtime失败根因。finalizerNative0+backup0/finishedtrue；v15净 **-.01961USDC**（gross-.00088/fees.01873/funding0/grid_pair0）。contain **grid-ordinary-linux-v15-failure-containment.json finishedtrue/fresh11DEXflat+backup/清grants+GRID0/停仅v15timer**，runtime2685556/watch2685558，旧套利1223049未动。

**新v16唯一RID ordinary-observe-linux-20261024-v16 / entropy-grid-ordinary-linux-v16-cutover.service** 已执行data/run/grid-v16-dispatch.py，once receipt grid-v16-cutover-dispatch-attempt.json存在；最新exec **85271**需poll原返回（不要再次dispatch）。原proof将grid-v16-cutover-dispatch-proof.json。当前实际gid **g-75c77a49beb44fd3**，原start **5dd562e4-343c-46d2-9992-08e372dc7eb1** running/initialize/STARTING-e1，已NativeSTOP OPEN及IOC待发。只查data/run/grid-v16-progress.py/原STATE/cutover/Native-running。本机只读watch **exec52565** 600秒有界运行，查看原输出，不再次启动金融控制。原v15本机watch49631已terminal0达到600秒界限，无本机writer。

v16只改新有限controller **lower=mark-50、upper=lower+100**，将参考价放在20USDC格子的内部，避免恰等于买入门槛。是新原配置，不改旧活跃策略，不强行补单，不削弱Nativeassertions；40capital含20reserve/ETHlong3cross/GTC5格/gross150/30sNative/600启动+600稳定/25h/连续86400全不变。追加离线断言初始mark距最近网格线≥9.9，市场大变仍可失败，未保证收益或24h成功。

v16 actualLinux监督 **151pass0skip XML SHA9fa43789c9457c7374d214d2cc995576ef216e4e6369f8fbb72123121b88b6bd**（新增参考价中心案例），workflow50/HTTP6/guards7/systemd/installedexecutor0。review **SHA7c39ee16768aca88c2cc9225fd391380213271dbf147e4db8ca81563454cffa3**，12privateSHA，controllerSHA **eeeac9f6374f61a1df03eb6cfbd8ec00d601fbcba4e668cd6b5c986cb4a6373f**；shared监督eb7...未变。核心未再改，仍327包59d375.../Linux1647全过/batch-recovery-version-proof66source13delta/qualifiedfalse，不重复全量或安装。上游CLOSE1000仍未唯一确定，不能宣称根治。

当前修复报告 **planning/grid/reports/2026-10-24-grid-batch-recovery-release.md** 尚记录v15启动等待，需在v16实际结果后更新，保留v15失败历史。所有v16工具已本机data/run、公开controller/deadline在planning/grid/tools，新tests相应v16；只本机预备/远端inactivehelper150/50已复核后才dispatch。新增read-only finalchecker grid-v16-final-start-check.py动态从原state读取startid，不依赖不存在的UI负载artifact；publiccheck脚本已替换实际gid g-75c77a49beb44fd3，最后实际Native启动成功后再运行。

下一步坚持原v16gid/RUNNING/start succeeded/独立Native正仓+stopOPEN、实际source66及review12SHA/原25h合同/guard/网站完成，再写真实起点和nextday验收时间、scp原proof。若失败只查原cleanup，不重发；v15所有原Native/终局/contain证明需复制本机。AI heartbeat/goal仍paused，不新增AI定时/新资金规模、不动旧套利/不启Macwriter。

# 当前修复已上线，v15唯一cutover已提交：只查原身份，尚待Native启动成功

用户“进行问题修复，目标正式上线”。7生产文件修复已安装，尚未完成新实盘启动，禁止重放dispatch。**唯一RID ordinary-observe-linux-20261024-v15 / entropy-grid-ordinary-linux-v15-cutover.service** 已提交，回执data/run/grid-v15-cutover-dispatch-proof.json submitted_ms1792836607203；once receipt grid-v15-cutover-dispatch-attempt.json存在。新review SHA **60db2506600cdf3b8871eb28c125af8119da18755cb7394ec6f7d0f3c92001ae**，12privateSHA绑定，原监督ordinary_supervision字节未变。只读watch本机exec **49631** 正运行（600秒界限），只查其输出/原STATE/原请求，不重新POST。当前实际gid **g-369b1ba6c19d4b2c**，原start **4eb7e575-4c5f-42ff-8c5f-12073b990256** running/initialize，STARTING/e1；已真实初仓.0088ETH，原生stop及部分网格accepted，最后watch剩1张PLANNED，无新原始Native-running证明/观察起点。认证publicpage/metrics/detail200、realdispatchtrue但detail尚STARTING；原登录未改。

已修复：HL普通已确认订单按grid/market/epoch≤20成批撤销，一次新鲜授权/签名，所有原CID先逐条journal CANCEL_REQUESTED、独立Nativequery终局，响应丢失/超时绝不盲重发；UNKNOWN等仍走原query-first单条路径；nativewhole_position保留。单个冻结市场不重复account级完整barrier，多冻结市场保留全账户先全部核对后再重建。原recovery整段350作用域及普通公开550/关键350/总900预算保持原设计，未将普通独立提交扩大到关键额度。加入额度等待计数及最后等待、CLOSE连接年龄/ping/pong/原因分类（只静态词表不存peer正文）、cancel/reconcile/rebuild/attempt耗时。离线真实生产risk/executor有持仓5单恢复原pacing实测虚拟120秒/高占用<300；不当成真实新Linux持仓证明。新9public安全用例；额外真实bound全仓Native stop保留检查本机和Linux各1通过。

核心包 **data/grid-code-checkpoints/grid-code-20261024-batch-recovery-v1.tar.gz SHA59d3753bacb37b3ebaaf431eb47c80c1678873f174858039343f0366ec722559**，327文件，远端source-batch-recovery-v1.tar.gz / stage batch-recovery-v1-stage。实际Linux完整 **1647pass0skip**，分3不重叠模块组（1238grid+394其他+15review）合并report **grid-batch-recovery-linux-full.xml SHAbcd43e52492891c52318b27344d58a523e6a3357928c2482ad943874a1ec8084**；Mac1645pass2skip。独立新v15监督150pass（XML78c466...）/workflow50+HTTP6+guards7+systemd+installed executorpriority全部0。

安装原proof **grid-batch-recovery-install-proof.json installedtrue/finishedtrue/legacy_unchangedtrue**，version **grid-batch-recovery-version-proof.json qualifiedfalse/66source/从v35起13delta**（比先前11delta新增lifecycle.py及rpc.py）。只更新7生产文件+2新测试+manifest，备份/停writer前后fresh11DEXflat，GRID0守护健康，安装后runtime2677342。旧套利PID1223049未改动、未重启，未sync旧目录。

v14先已contained：proof **grid-ordinary-linux-v14-failure-containment.json finishedtrue/Nativefresh11flat+backup/旧v14timer disabled/清grants/GRID0**，runtime曾2673179/watch2673181。原v14已CLOSED/e10/cleanup3a9ded...succeeded，净-.441369，24hfalse，全部原失败记录不复用。

最新v15progress checked1792836672009：3fresh历史Native审计+formalbundle+preflight全部0；ordinary installer正在GRID1独立runtime切换（新2678065/watch2678067），grants1，尚无gid/start。旧套利1223049。下一步等watch/原STATE实际gid/RUNNING及独立Native正仓/止损OPEN，再run data/run/grid-v15-final-start-check.py（已从原STATE取startid、无continuousUI负载需directpubliccheck），核实际网站并同步原证据、写准确起点与24h/25h时间。公共Basic凭据未改，data/run/grid-public-login.json只内部用不输出。

本次source/fulltest安装helper均在data/run保留；新v15公开tools/controller/deadline/tests在planning/grid/tools及tests，但生产包不包含新privateworkflow，另150/50严格绑定。原controller25h/30sNative/40capital含20reserve/ETHlong3cross/GTC/5格/gross150/600启动+600稳定核验/连续86400门槛均不变。外部约3h CLOSE1000原因仍未唯一确认，新日志有诊断但不宣称已根治。AI heartbeat/goal保持paused，不新增AI定时或Macwriter。只资金CLI/API，未知原身份先查询。

# 最新定位完成：本机额度调度复现恢复慢；原始CLOSE1000根因仍有缺口，未部署/未重开实盘

用户“进行定位”已完成只读线上取证+离线复现，详见 **planning/grid/reports/2026-10-24-grid-v14-disconnect-diagnosis.md**。生产8关键文件SHA与本机一致；runtime2292364 diagnostics预算900/window60/last_rate_limit=null，原进程未重启，没记录到Native429冷却。旧套利1223049未动。**未修改生产源码/未部署/未服务重启/未资金/未新轮/未AI定时**。

四次SQL事件分段分别 **60.815+182.676=243.490s、60.867+182.552=243.419s、61.360+182.325=243.685s、120.233+182.965=303.198s**。第四次引擎确实恢复e9成功，但原监督首次识别起已301.525s，先检查300上限后才接受RUNNING，正确timeout→原cleanup平仓CLOSED/e10。不是重现最小名义错误或永不恢复。

明确可复现调度机制：每张cancel新鲜named-agent检查140+Nativequery2=142，5张710；后续recover整段350预约令710+350>900，必须等60s。初始额度800的离线场景复现先等60再等60；800非历史实测，历史起始占用未保存。生产executor+risk原Native格式fixture每单实耗264，两单528<普通550，但固定350准入令第二笔等60s；仅离线反事实264准入消除等待，**264不是unified实测通用常数或部署修复**。真实Linux与Mac两个离线脚本均通过、网络0资金0。原SENT→ACK多~1s、重建分钟分组吻合，不能将全部恢复时长归于交易所响应慢。

账户流四次均peer CLOSE1000约2h49–53m；已2s JSONping，无本轮心跳超时日志。历史只读无signer探针10556s亦CLOSE1000，说明交易不是必要条件，但同程序/网络不能唯一定位服务端/网络。原close reason/末次心跳与阶段预算历史未存，不能补造；本次没新建需数小时探针。下一步修复应分阶段准入/减少重复重型读取、保留安全核验并补脱敏阶段耗时/等待/心跳诊断；不扩大IP额度/资金或放宽30s保护、300s监督、25h退出、86400门槛。新轮先contain旧grant/timer，不复用v14。

新证据data/run/grid-v14-diagnosis-raw.json、grid-v14-cancellation-diagnosis.json、grid-v14-runtime-diagnostic-read.json、grid-v14-recovery-phase-timeline.json、grid-v14-offline-budget-replay.json、grid-v14-offline-executor-profile.json、grid-v14-linux-offline-diagnosis.json。可重跑完全离线scripts在planning/grid/diagnostics/v14-recovery-budget-replay.py及v14-executor-budget-profile.py。以下只读资金审计/启动历史保留。

# 最新只读检查：v14已CLOSED，监督恢复超时退出，24h未通过

北京时间2026-10-24 16:46–16:48核验：g-8678b5d46b724281 CLOSED/STOPPED/e10，活动网格0。原start afcf6d7d-2b8f-4f50-8cbd-748369ee2a78 succeeded；原cleanup **3a9ded66-2cae-4510-89ee-8da016428c56 succeeded/remaining0**。旧回执running不是当前operation，禁止重POST。

00:49:13起至12:19:03平仓约11h30m含恢复；**最长连续10144.986秒（2h49m05），ordinary24false**。四次流中断，前三次监督确认恢复242.408/246.816/241.913s。第四次12:12:02 Native CLOSE1000gen4，事件5548至5552恢复约303.198s；虽最终RUNNING/e9，监督原五分钟已过，SUPERVISION_RECOVERY_TIMEOUT原退出。不能误报永不恢复或24h成功。CLOSE1000根因仍未唯一确认，未修改生产代码。

**本次fresh11DEX flat/noorders**，4笔Native成交匹配原账本，12条资金费逐条匹配。净 **-.441369USDC（-1.1034225%/40）**，gross-.3999/fee.040072/funding净支出.001397/unrealized0；grid_pair0，未完成网格获利闭环。原finalizer Native0+backup0/finishedtrue，备份当前integrityok/原策略CLOSED。认证page/detail200且CLOSED/0仓位，grid2292364/watch2292366active，旧套利1223049未动。

本次金融0/服务变更0/未新轮/未恢复AI自动化。原v14timer仍waiting（2026-10-25 01:42:10.955）/policy1原grant未改，不代表活跃交易。后续新轮先按流程contain旧grant/期限，不复用本轮身份。详见 **planning/grid/reports/2026-10-24-grid-v14-status-audit.md**；原始新只读证据data/run/grid-v14-user-audit.json、grid-v14-recovery-status-evidence.json、grid-v14-status-public-check.json、grid-v14-latest-progress.json。以下“唯一活动/RUNNING”均启动历史，被本段取代。

# 当前唯一活动轮v14：真实启动及独立Native正向核验成功，服务器继续24h观察，不重放

用户“开启新的实盘验证”本次已落实。**g-8678b5d46b724281 / ordinary-observe-linux-20261024-v14 / RUNNING-ACTIVE-e1**；原start **afcf6d7d-2b8f-4f50-8cbd-748369ee2a78 succeeded**。原cutover **grid-ordinary-linux-v14-cutover-proof.json finishedtrue/freshformal0+installer0+native-running0**。真实Native **grid-ordinary-live-linux-v14-native-running-proof.json**：**正仓.0129ETH/cross3/6working orders含非reduce entry+全仓NativeSTOP OPEN/gross51.77845<=150**，SQL前后相同、Native精确匹配、runtime同身份；sampling_attempts1，未声称在线重试已触发。不能把它当24h完成。

**实际严格观察起点北京时间2026-10-24 00:49:13.053 /1792774153053**，Native 2026-10-24 00:49:14.119；最早24h **2026-10-25 00:49:13.053**，建议用户 **10月25日01:00后**再查连续窗口及终局Native/账本/备份。独立25h截止 **2026-10-25 01:42:10.955**，timer entropy-grid-ordinary-deadline-linux-v14.timer activewaiting，合同SHA **5f2bff53d70c9df5d152ab5deabc87ef4a44516fb8a45d3c765a8dd57474af5e**；不延长/重装/复用。服务器controller/observer/finalizer/期限继续；AI heartbeat/goal保持paused，本次未新增AI跟进，无Macwriter。

最后只读 **data/run/grid-v14-final-start-review.json checked 2026-10-24 00:50:54/1792774254809**：RUNNING e1/start succeeded/无未决，soak最长101.049s/issues{}、ordinary24false。实际runtime **2292364/watch2292366** 同instancehealthy新鲜，旧套利 **1223049** 全程未动。网站真实 https://taoli.coinfetcher.xyz/grid/#/grid/g-8678b5d46b724281 已请求app打开（queued）。原密码未改不要重发；public **grid-v14-public-access-check.json** 匿名401/认证page200/metrics200production-GRID1/detail200RUNNING。配置仍40capital含20reserve/ETHlong3cross/5grids/GTC/150gross/30sNative/25h，原600s启动及稳定核验窗不变。

只GET负载 entropy-grid-v14-ui-read-check.service已自行结束 **finishedtrue/native_running_observedtrue/financial0/detail20071 max.2953s/market2002+5033/delays4**，不能说所有HTTP成功；行情可能延后，控制详情读取响应正常。原proof已scp本机。完整150Linux（SHA6d0223...）+新50workflow（新增16 Native采样安全用例）/6HTTP/7guard/systemd/installedexecutor全0，新review **SHA367b70130c738c64baa8a2991e06df9442f1720ed8ce3b825d4a60b356272f40** 绑定12private/当前sourceversion。source核心完全未改，仍325包SHA867fdf.../Linux1638/66sha11delta/qualifiedfalse，不重复全量/安装；当前私有NativecheckerSHA **e8490ef6ce101cf2bee7545dc41d3497e6de5fa5bedbc23f0870175eb68b0dae**。

本次v13曾在RUNNING边界Native比较仓位断言失败；正常新buy于1792772132602成交，5523入账32.984，检查33.296失败，observer33.446才开始；完整前后样本未保存不补造。原startdd906...succeeded，原cleanup **2aef9e81-57a6-4034-ae95-cf6ff22144ad succeeded**，CLOSED/e2/3fills/FIFO **-.024904USDC**。原finalizerNative0+backup0，原contain finishedtrue/fresh11DEXflat+backup/停v13timer+GRID0/grants[]。其历史与v12更早失败均保留，不复用资金ID/观察窗。新v14只读sampler修复已验证：移动样本不 qualify，只共享原600窗重采样；固定SQL与Native不符10s失败，foreign/stop missing/新增控制立即失败；最终严格Native+SQL相同/identity/guardian断言不变或加强，shared ordinary_supervision字节未动，Native30s与25h期限及86400门槛未延长。上游CLOSE1000未唯一确认，留待真实运行，不声称新版本实盘认证全部完成。

原v14 STATE/Native/soak/cutover/install/deadline/UI/review/150XML/50XML及原v13 STATE/终局Native/原finalizer/cutover/contain均已scp本机。最新报告 **planning/grid/reports/2026-10-24-grid-v14-live-start.md**。本机watch **68204已terminal0**，最后finalcheck26949、HTTP39693、scp94990均terminal0；无需poll旧session，所有本机执行已结束，只有服务器原验证在继续。

下一次用户要检查时，先fresh查**原v14 STATE、SQL、strict soak、原cleanup RID、原finalizer/终局Native+backup、原期限**；未知请求查原identity不POST。只有真实连续86400秒+严格报告+终局全flat/账本+备份均通过才推进阶段资格。用户说“一天了”不代表成功，不将普通测试/空仓故障当持仓证明；若失败按真实记录处理，不重用失败窗。保持旧套利/root/code/entropy独立，不启Macwriter或AI定时，不把多交易所产品整体标完成。更早“v14尚未启动”段落均历史。

# v14已唯一提交，当前只查原启动：新采样器Linux50+监督150已通过

用户“开启新的实盘验证”仍在执行，必须完成实际正向Native启动再汇报。**唯一RID ordinary-observe-linux-20261024-v14 / entropy-grid-ordinary-linux-v14-cutover.service** 已提交，回执 grid-v14-cutover-dispatch-proof.json submitted_ms1792773656385，once receipt grid-v14-cutover-dispatch-attempt.json存在，**禁止再次dispatch/create/start**。只查data/run/grid-v14-progress.py、原STATE grid-ordinary-live-linux-v14.json、cutover proof及Native-running proof。本机只读watch grid-v14-watch-start.py刚启动，exec见最近工具输出（需poll），没有Macwriter或新AI定时。

v14新review **grid-linux-recovery-supervision-review-v14.json SHA367b70130c738c64baa8a2991e06df9442f1720ed8ce3b825d4a60b356272f40**，绑定12私有SHA。实际Linux新 **150pass0skip** XML SHA **6d0223aaf3d515a9cdee06c9cdfc22da3507d302c3d61baf8b9c49fdcca2d53a**，另workflow **50pass**（新增16 Native竞态/严格安全/永久不符10s/共享600s不延期测试）+6HTTP/7guard/systemd/installedexecutor全0。核验器源码SHA **e8490ef6ce101cf2bee7545dc41d3497e6de5fa5bedbc23f0870175eb68b0dae**；生产核心完全未改，仍325包/1638Linux/66source11delta/qualifiedfalse。

资金前已先启动**只GET有限UI负载 entropy-grid-v14-ui-read-check.service**。原cutover将fresh3Native/formalbundle/preflight，安装本轮40grant/GRID1并仅切网格服务，再唯一controller/finalizer/25h期限/独立Native正向检查。新资金仍40含20reserve/ETHlong3cross/5grid/gross150/30s原生保护/原600s启动+600s稳定窗口/严格86400观察不变。未知请求只查原identity不POST，Native失败原cutover通过cleanup_once原RID收尾，不重POST。尚未查询gid/RUNNING/Nativepositive，不能说已开始。

v13已CLOSED/原cleanup2aef...succeeded/Native+backup/containfinishedtrue/GRID0/空grants，净-.024904、不是成功实盘或24h。v14只修**独立只读检查器**采样竞态，最终SQL前后完全一致且Native实际正仓精确匹配/所有订单归属参数/cross3/STOP OPEN/gross150/guardian严守；正常成交跨读只discard样本，在**共享原窗口**重读，不改止损/新金融/更新时间原Native期限。Native与不变SQL不符10s即失败，foreign/stop missing/scopeepochchange等立即失败。失败v13记录/原合同不复用，不把未确认样本、空仓或历史持仓证明冒充新实际通过；上游CLOSE1000原因仍未唯一确认。

待实际正仓Native证明/原start succeeded/切换完成后，读freshSQL/66与12SHA/原25h合同/health/旧套利PID1223049/实际网站，再复制真实启动证据、写准确CST起点和下一天验收时间，关闭本机有界watch。服务器自己运行；AI heartbeat/goal继续paused，不新增AI自动化。更早“v14未准备”的记录均为历史。

# 最新：v13 Native核验采样竞态已退出并contain；v14只读采样修复在准备，未金融启动

用户最新明确“开启新的实盘验证”，仍要完成新轮真实正向启动，不要停在准备或失败。新v13 **g-0c92b0053ec248bf / ordinary-observe-linux-20261024-v13** 初始开仓及5GTC实际成功，但Nativechecker在RUNNING边界比较非原子SQL/Native仓位断言失败（原grid-v13-cutover-native-running.log line64 qty）。Native正常新buy .0043在1792772132602成交、5523于32.984入账，原Native检查33.296失败，controller33.446才开始strictsoak；并非断线恢复/生产源1638失败。原cutovercleanup **2aef9e81-57a6-4034-ae95-cf6ff22144ad succeeded**，原start **dd90670d-850f-4af1-8743-cb08bfc6bf45 succeeded**，最终CLOSED/e2，3fills、FIFO net **-.024904USDC**。原finalizerNative0+backup0/finishedtrue；没有成功Native-running proof，strictsoak0秒，不能重开/复用v13资金身份或算24h。诊断data/run/grid-v13-startup-failure-diagnostic.json真实全记录。

唯一 **entropy-grid-ordinary-linux-v13-contain.service**已terminal0：proof **grid-ordinary-linux-v13-failure-containment.json finishedtrue**，再fresh11DEXflat+backup、停仅v13timer清grants/GRID0，仅gridruntime/watch重启，现runtime **2289137**、旧套利 **1223049**未动。源仍initial-exit-slots版本66/11delta/325包1638Linux，**未改任何生产source/未重装/未重测完整**。所有旧cleanup/financial不重发。

已准备新RID **ordinary-observe-linux-20261024-v14**。planning/grid/tools的v14controller/deadline、tests新v14controller/deadline、data/run所有v14脚本已从v13clone（同原限额和期限）。只新 **grid-ordinary-linux-v14-native-check.py** 修正核验器：最终成功仍须SQL前后全同、Native实际ETH正仓精确等于账本、同scope/epoch/guardian、所有Native订单归属/参数、cross3、原生STOP OPEN、working entry、gross<=150；对正常成交穿越采样只discard未确认样本并在**同一原600s窗口**只读重采样，不发资金操作、不改/延长止损或25h期限；Native与不变账本持续不符最多10s即失败；foreign order/额外position/stop missing/changed scope/epoch/用户pause等立即失败，不能用重采样绕过。暂无新Native-positive证明。

新data/run/test_grid_ordinary_linux_v14_readiness.py+version本机 **50pass**（原34+16采样/保护/范围/无deadline续期/真实永久差异失败），不依赖tests/engine_harness路径。v14prepareworkflow已更新50workflow/150supervision，并用新alias **grid-v14-executor-check.py**，避免重写已存在v13 alias；原shared ordinary_supervision字节不动。新review增加native_sampling_review，明确v13失败已关、只修只读采样，不能当唯一上游CLOSE原因解决。core full1638门槛、Native positive/11source精确绑定等不放宽。

当前 **PYTHONPATH=. python data/run/grid-recovery-supervision-v14-verify-install.py** 正在本机exec **59573**，唯一新Linux stage recovery-supervision-v14-stage，预计150/0skip；先poll原exec，别重跑。成功后再执行 **grid-v14-prepare-workflow.py**（未执行，预计50workflow/6HTTP/7guard/systemd/installedexecutor都0），审阅实际新review SHA后准备新的v14dispatcher（未创建/未执行，必须绑定新reviewSHA、lastfinalization=v13）。v14 UI只GET源已准备未启动；未创建新v14candidate/STATE/金融contract，不可说已开始。需要真正启动后按原Native proof验收，再给用户时间/链接。AI heartbeat/goal保持paused，不恢复；没有Macwriter。

v13只读watch98669已terminal1（发现CLOSED），所有旧v13准备/dispatcher等exec均terminal，v13contain提交80996terminal0实际unit已确认finished。当前只有59573私有Linux验证待poll，不重复旧脚本/已closedcleanup/原source安装。用户已授权新小额40USDC含20reserve、ETHlong3cross、5grid、gross150/30s保护/独立25h/严格86400秒，继续完成本次新轮启动，遇阻先查原身份。

# v13唯一cutover已提交：先查询原STATE/请求/Native证明，禁止重新dispatch

**已唯一成功提交** data/run/grid-v13-dispatch.py，receipt grid-v13-cutover-dispatch-attempt.json 和成功回执grid-v13-cutover-dispatch-proof.json。RID **ordinary-observe-linux-20261024-v13 / entropy-grid-ordinary-linux-v13-cutover.service**。实际fresh3Native/formalbundle/preflight全0，原installer已备份/只停独立网格/安装新40grant+GRID1，正等待实际runtime/watch/HTTP就绪才启动controller。尚未读到新gid/RUNNING/Native-positive，不能说验证已开始。

原服务器只GET UI负载 **entropy-grid-v13-ui-read-check.service** 已先启动；原controller/finalizer/native-runningchecker/25h期限仅原cutover负责，一旦STATE/contract创建不可复用或延长。本机只读watch **data/run/grid-v13-watch-start.py exec98669** 正在有界采样原进度，优先poll它，不要另开资金controller。其他前置150Linux/34workflow/6HTTP/7guard和dispatcher均已terminal0；无Macwriter、无新AI自动化。新RID/版本绑定/测试/准备来源见下。

完成标准：原start真实succeeded+RUNNING/独立Native正仓、working entry、全仓原生stop OPEN、前后SQL同identity、policygrants/66SHA12private及guardian/旧套利复核，再保存原STATE/soak/Native/cutover/deadline/启动报告，给用户明确CST时间。请求未知查原identity，不重POST。若Native失败，先查原oncecleanup和终局Native+backup；不要把task start或模拟/空仓当实际启动通过。严格24h仍待实际时间，不修改旧失败记录/门槛。

# 即将唯一提交v13：用户明确要求开启新实盘验证，先查原身份，不重复资金请求

本次用户明确“开启新的实盘验证”。已fresh Native确认原v12 CLOSED、11DEXflat/0orders/无active。新rid **ordinary-observe-linux-20261024-v13**；源仍新initial-exit-slots325包/66SHA11delta，未改生产源/未重测1638/未重装源码。原v12全部失败窗与合同保留。

新v13私有Linux **150pass0skip** XML SHA **b2524d124e546746dcdb07cfb0e7baf3588c75b3c2222028e788b2199832e5e8**，proof grid-recovery-supervision-v13-install-proof.json preparedonlytrue/服务未重启/grants0/financial0；另34workflow/6HTTP/7diagnosis/systemd/installed executor check全过。新preflight严格当前11source变化/1638真实Linux/fullsource66及150新监督，未放宽资格。真实原v12数据offline replay也已在实际Linux运行，old HL_MIN_NOTIONAL/new VALID/qty.0088，同Macproof SHA；不是现场异常或新持仓证明。

新review **grid-linux-recovery-supervision-review-v13.json SHA4f00f39fe55dfcc302364f19add0fb6798543d843c52163b97fade919fc1bc13**，绑定12私有SHA+current versionproof+150/34测试、原保护/小残仓/持仓流恢复实际历史证据；ordinary24false/根因仍未唯一确认。新private源码仅新RID/版本exact gates/新证据引用，控制/25h期限/原30s/600s Native启动、资金40含20reserve/ETHlong3cross/5grids/gross150/GETretry POSTonce/严格86400全部不变；原supervision代码不改。AI heartbeat/goal保持paused，禁止Macwriter与旧套利操作。

**data/run/grid-v13-dispatch.py已准备，尚需唯一执行**。启动前还将启动只GET有限UI负载grid-v13-ui-read-check.py，不发交易。提交会先写once receipt grid-v13-cutover-dispatch-attempt.json；只要该文件已存在，不再运行dispatcher/重发create/start/cutover，查 **entropy-grid-ordinary-linux-v13-cutover.service / grid-ordinary-linux-v13-cutover-proof.json / STATE grid-ordinary-live-linux-v13.json / data/run/grid-v13-progress.py**。原cutover先fresh3Native+formalbundle+preflight，唯一安装新40grant并只切网格服务，真实HTTP准备好后唯一controller/finalizer，随后独立Native-running正仓/working entry/全仓STOP/稳SQL。尚无新gid/金融启动或Native-positive证明，不能说已开始或24h通过。

如果Native启动失败，原cutover会通过原cleanup_once以原RID清理，不得重复POST；要查原operation和终局Native+backup。新deadline合同属于v13且一旦创建不可延长。当前本机所有private测试/prepare exec已terminal，旧完整Mac/LINUX也terminal，不需poll旧session。待实际RUNNING和Native正向证明后写启动报告与准确时间，服务器自己运行，用户一天后再查，不创建AI定时。

# 当前最新：初始退出档位与重建诊断修复已实际上线；GRID0/空授权/无活动资金轮

用户“进行修复”已完成。**原v12 g-9b3b7681eded4dfa仍CLOSED/STOPPED/e6，普通24h未通过；未启动新实盘轮**。现在GRID_REAL_DISPATCH=0、grants[]、无活动策略/未结束operation/未决intent。原v12 timer inactive/dead、合同保留。仅独立网格服务已更新；旧套利PID1223049未动，无Macwriter/无AI heartbeat或goal恢复。

**实际新源码已安装成功，不重复测试/安装/原资金请求。** 原Linux完整 **entropy-grid-initial-exit-slots-linux-complete.service terminal0 /1638passed0skip/520.28s**，XML SHA **948ff8525f9056cbc5266d6afdff00ef40df8b479af3346613c0ca8433a1a670**；Mac1636pass2skip/115.56s。325源包 **data/grid-code-checkpoints/grid-code-20261023-initial-exit-slots-v1.tar.gz SHA867fdf0588040ddab783f3cb29a14949c48adc1f7220f776f42135dcff74cf25**，仅4source inventory_layout/runtime/recovery/stream_recovery +1新test/1改test变化。版本新 **grid-initial-exit-slots-version-proof.json qualifiedfalse/66source/原v35起11delta**（原8+inventory_layout/recovery/stream_recovery）。后续新正式审阅必须针对本版，别用旧initial-reconciliation版本或旧v12私有审阅自动授权。

实际修复：真实v12终局数据离线重建，旧0.0065/.0023同timestamp导致FIFO先consume.0023+.0021，逐fill offset把剩余初始.0044拆为slot4.0023/slot5.0021，各<10USDC，真实HL纯订单校验HL_MIN_NOTIONAL。新对同原IOC全部真实filled/remaining汇总推进原保存slot序列，原slot5.0044完整退出，另gridlot .0044不变，owned.0088；金融FIFO/fee/price/pairs未变。映射含closed-cost-fill、offset连续、cumulative所有证据均严格验证，真小残额仍Native最小金额拒绝。**原现场异常未落盘，离线证明不冒充现场唯一根因**；上游CLOSE1000仍未唯一确认。新增recovery_rebuild_failed安全代码+epoch证据；scheduler即使epoch已变也记录appliedfalse/retryfalse，不覆盖用户新pause/stop。旧源码明确复现新测试2fail2pass；新相关48pass/完整上述1638pass。

**一次部署链实际有受控续行，不掩盖失败。** entropy-grid-initial-exit-slots-repair.service 原链finishedtrue/errorAssertionError：contain-v12成功，而首次源安装因stage SOURCE-MANIFEST0644触发WATCHDOG_FILE_PERMISSIONS，未创建installproof/rollback目录，原66源SHA仍全同。只将stagemanifest改0600（内容SHA不变），唯一新 **entropy-grid-initial-exit-slots-install-resume.service terminal0**成功。canonical **grid-initial-exit-slots-install-proof.json finished/installedtrue/resume_attempt2/无rollback**，绑定首失败logSHA，首失败chain留存。不要再运行任何installer/containment或改原链记录。原v12 containment **grid-ordinary-linux-v12-failure-containment.json finishedtrue/GRID0/grants[]/fresh11DEXflat+backup/仅原v12timerdisabled**，之后installer又freshflat+backup、停网格writer后再Native11flat、仅替6文件+manifest、网格健康；所有financialactions0。

最后 **grid-initial-exit-slots-final-review.json checked 2026-10-23 23:50:20**：newruntime **2273027**，watch同instancehealthy/.3秒内新鲜、66/325SHA全同、旧套利1223049、active0/pending0/unresolved0/grants0/GRID0/timerinactive。新fresh **grid-v12-post-repair-native-audit.json 2026-10-23 23:49:39**：11DEXflat/0orders，原5fills重核，CLOSED/FIFO净+.109088不变；不能把它当持仓/新24h通过。网站 **grid-initial-exit-slots-public-access-proof.json** 匿名401/原认证page200/metrics200production-realdispatchfalse/detail200CLOSED；密码未改未重发。

报告 **planning/grid/reports/2026-10-23-grid-initial-exit-repair.md**。实际XML/install/version/contain/原失败chain已scp本机，所有本次exec均terminal（最后并行29045/59087/47128都已terminal0），本机无watch等候。当前工作已收尾，用户尚未在本次另行要求重启新的资金验收；后续如明确推进新轮，先fresh查原SQL/Native/本版source，再完成新源码正式审阅/持仓恢复证明及独立新RID/期限，不沿用v12失败身份/窗口、不弱化门槛、不恢复AI自动定时。更早记录为历史。

# 正在修复：初始退出档位修复包已Mac完整通过；原Linux完整测试/一次部署链在跑，先查原unit

用户最新明确要求“进行修复”。已按实际v12成交离线重建并复现旧代码 HL_MIN_NOTIONAL：初始IOC .0065/.0023两成交同timestamp，FIFO按UUID先消耗后到的.0023，再.0021，存活初始.0044被错误分成2419.6价.0023+2439.6价.0021，单笔各<10。新owned_lots按同一原IOC全部已拥有成交的filled总量/remaining总量推进原始slot序列，完整剩余slot5 .0044；金融FIFO lot/价格/手续费/pair不改。完整历史offset连续/cumulative/所有closed-fill映射都校验，真小残额仍原HL_MIN_NOTIONAL，未合并不同价格或不同IOC。

本机真实离线proof **data/run/grid-v12-offline-inventory-replay-proof.json**：原v12输入重建（不是现场异常快照）old HL_MIN_NOTIONAL/new VALID/owned .0088不变。源码仅4项 **inventory_layout.py/runtime.py/recovery.py/stream_recovery.py**；新增initial_exit_fragmentation9case、recovery_audit4case（真实runtime/scheduler的epoch变化/新pause新stop、错误脱敏）。旧代码新回归明确2failed2passed；修复后针对性48全过；Mac干净325源包完整 **1636pass2skip/115.56s**。qualified/24h依旧false，CLOSE1000根因未唯一确认。

新包 **data/grid-code-checkpoints/grid-code-20261023-initial-exit-slots-v1.tar.gz SHA867fdf0588040ddab783f3cb29a14949c48adc1f7220f776f42135dcff74cf25**，325文件/仅6项文件相对上包变化（4源+新增test+audit test）。原Linux唯一 **entropy-grid-initial-exit-slots-linux-complete.service** PID2267561（预期1638/0skip）；新stage data/run/initial-exit-slots-v1-stage。不要重复测试或重新解包同目录。

唯一server一次部署链 **entropy-grid-initial-exit-slots-repair.service** 已提交成功，原proof **grid-initial-exit-slots-repair-chain.json**。它只等原Linuxfull真实1638全过后，按顺序执行 **grid-ordinary-linux-v12-contain.py**（原v12终局+fresh11DEXflat+backup，停仅原v12timer、清grants/GRID0、重启仅网格），再 **grid-install-initial-exit-slots-v1.py**（freshflat+backup、严格旧66+原324manifest/新325SHA、仅改6文件和manifest、失败仅source rollback、网格健康、旧套利同identity）。proof分别 **grid-ordinary-linux-v12-failure-containment.json / grid-initial-exit-slots-install-proof.json / grid-initial-exit-slots-version-proof.json**。未读到finished前不要声称已部署。网络不明查原unit/proof，不重新dispatch；本机once记录 **grid-initial-exit-slots-repair-dispatch.json**。原Linuxfull是server后台进程，不是AI自动化，AI heartbeat/goal保持paused。source-only/financial0，不创建新轮次、不重放任何原资金请求。本机pipeline执行23380已terminal0。

若测试/链失败，先查原proof/日志和现场，不重发原脚本。本轮Macfull67917已terminal0，无本机gridwriter。下一步等原Linux完整/contain/installer实际结束，核验新66SHA/源码真实加载、GRID0/空grants/无活动策略、guardian、freshNative及网站认证HTTP200、旧套利1223049不变，保存证据并写修复报告，不擅自新实盘或恢复AI定时。之前v12实盘失败/终局证据见下。

# 当前最新：v12已提前CLOSED，严格24h未通过；本次只读审计无重启/新资金轮

用户最新仅要求“运行1天了，进行检查”。只读实际检查 2026-10-23 22:58:40：**g-9b3b7681eded4dfa / ordinary-observe-linux-20261023-v12 已CLOSED/STOPPED/e6，无活动策略**。原start74ba9...succeeded；原cleanup **b4aa062a-9af4-4808-b84a-c887badc6a4d succeeded/remaining0**，不要重POST。原controller/finalizer均finishedtrue，native-audit0+backup0。原STATE中的start/cleanup running只是提交回执，不是当前状态。当前结论**普通实盘24小时未通过**，非观察中。后续下面旧“RUNNING”记录均历史。

实际观察北京时间**10月23日02:00:02.381→07:54:32.919**（总5h54m30s含恢复），严格最长 **10532.138秒/2h55m32**，issues GRID_EPOCH_CHANGED4/GRID_NOT_RUNNING61，ordinary_24h_observedfalse。实际日志两次CLOSE/1000：**04:52:59 generation1**→首次恢复242.407s/04:57:01/e3成功，**07:52:39 generation2**→e4完整对账5514/5515ok后e5 NEEDS_ATTENTION/PAUSED、未成功重建；原supervisor SUPERVISION_GRID_STOPPED→原cleanup once并正常退出。已确认失败阶段，但**具体激活异常未持久化、上游CLOSE原因未唯一确认**，不要臆称预算/资金/杠杆根因。源码_recover重建catch只存通用gap，_finish遇epoch已改变会直接return，有可观测性缺口；本次仅检查未改代码/部署，不擅自重开验证。

fresh Native 2026-10-23 22:58:38：**11DEX全flat/0orders、无账本残仓、4FILLED11CANCELED/无UNKNOWN**。5笔fill量价费/identity与原终局proof逐一再核、6笔funding按时间ETH与符号全对。账本FIFO **+0.109088USDC**（fee.022036/funding-.001526/40ROI.27272%）；gridpair净.071791446...，总净不全为gridpair。Native closedPnl总.132704 vs FIFOgross.13265差.000054成本舍入口径，不能说两者精确相等。原备份grid-20261022T235436Z-5e778e25/seq5518，重新只读integrityok且策略CLOSED。

源66/private12 SHA无变；实际runtime1802603/watchhealthy同instance、旧套利1223049active。原v12 deadline timer仍active/waiting、grant仍1、GRID1无活动资金策略，本次**未GRID0 containment/未停timer/未动grant/未重启任一服务**；若后续需收尾必须查原身份与Native再按授权处理，不把此记录误读为已containment。AI linux heartbeat/goal仍paused，未创建自动跟进，无本机writer。所有本次exec均terminal（只读采样/diagnosis/health），不要poll旧session。

报告 **planning/grid/reports/2026-10-23-grid-v12-live-audit.md**。最新本机只读证据 **data/run/grid-v12-user-audit.json、grid-v12-readonly-diagnosis.json、grid-v12-activation-previews.json、grid-v12-source-and-health-audit.json、grid-v12-user-audit-summary.json**，fresh原v12最终STATE/soak/终局摘要/cleanup/backup均在audit内，旧单独本机STATE/soak可能仍是启动快照，不应覆盖fresh结果。继续工作只在用户授权的后续修复范围内修复重建及异常落盘，再新独立身份/期限/真实证据验证；不复用v12或更早失败窗口。

# 当前唯一活动资金轮：v12启动及独立Native验收均成功，服务器继续24h观察；不重放/不再部署

**g-9b3b7681eded4dfa /ordinary-observe-linux-20261023-v12 /RUNNING-ACTIVE-e1**；原start **74ba9c1b-9397-419b-9b9a-8945c508999e succeeded**。原cutover grid-ordinary-linux-v12-cutover-proof.json **finishedtrue/freshformal0+installer0+Native-running0**。真实独立 **grid-ordinary-live-linux-v12-native-running-proof.json** 已本机保存：**正仓0.0088ETH、cross3、6working orders含开仓单与原生全仓stop OPEN、gross52.33536<=150**，前后SQL与实际runtime同身份。不是空仓/模拟/24h通过。新源与新private真实66/12SHA逐一复核，Linux完整1625/0skip/实际源install/version与审阅均完成，禁止重复这些已经成功的测试/部署/资金请求。

**实际RUNNING/严格观察起点 2026-10-23北京时间02:00:02.381 /1792692002381**，Native证明02:00:03.796；最早24h为 **10月24日02:00:02.381**。建议用户 **10月24日02:10后**再来验收实际连续观察及终局；不按网站在线时长算成功。原immutable25h timer entropy-grid-ordinary-deadline-linux-v12.timer active/waiting，due **2026-10-24北京时间02:53:02.062**，合同 SHA **89cfaf191fe5b39b0b5166978ac291d59f99881218386fad85a0121b32794579**，不延长/重装/复用。服务器原controller/finalizer/observer及期限继续，**AI linux heartbeat/goal按用户省token偏好保持paused**，没有创建新AI自动跟进；本机所有exec/watch均terminal（新watch28216terminal0、最后scp33164/46776和HTTP43324均terminal0），无本机grid writer，不需继续poll旧session。

最后只读 **data/run/grid-v12-final-start-review.json checked_ms1792692142382**：RUNNING e1/start succeeded/unknown0，严格soak最长136.182979s/issues{}，ordinary_24h_observedfalse，runtime **1802603/watch1802605** fresh同instance，旧套利 **1223049** 安装/资金切换前后没变。真实网站 https://taoli.coinfetcher.xyz/grid/#/grid/g-9b3b7681eded4dfa 已请求应用打开；public页面200/认证metrics200 production/GRID1、匿名401，原登录未改且不要再输出密码。当前配置40capital含20reserve/ETHlong5grids/3cross/150gross/原30s保护/25h独立退出。不是整个多交易所产品完工或正式实盘资格通过。

只读网页负载 **entropy-grid-v12-ui-read-check.service** 已自行结束（prooffinishedtrue/native_running_observedtrue/financial0）：初始化全程详情70次200、maxdetail.70004s；行情2次200+3次503/延后4次。**不能说全部HTTP请求成功**；行情请求在交易准备优先期间会等待/暂时不可用，详情/控制读取保持响应；未关网页掩盖饥饿，也没放宽Native/资金/时间门槛。其原proof已scp本机。新的Native初始建仓完整对账已成功后才生成5网格单，之后全部挂单确认并Native独立核验通过；本轮尚未另行证明必须遇过在线索引延迟再恢复（离线已真实复现和覆盖），不夸称唯一根因全部解决。上游CLOSE/1000原因仍未唯一确认，留待真实断线恢复/24h结果审查。

**最终报告** planning/grid/reports/2026-10-23-grid-repair-and-live-verification.md，记录修复、测试、隔离、实际启动、网页行情503及限制。旧报告2026-10-23-stop-recovery-deployment.md已改成v10失败历史，不能把旧gid或其初始化当当前轮。关键实际证据已本机：grid-initial-reconciliation-{install-proof,version-proof,linux-complete.xml}、grid-recovery-supervision-v12-linux.xml、grid-v12-workflow-linux.xml、grid-linux-recovery-supervision-review-v12.json、原installer/合同/STATE/cutover/Native-running/soak/UI/public-access/final-start-review。当前Source324包SHA7cf83.../Linux1625XML SHAdca7...，private135XML90b3...，reviewa4da...，详后续记录。v9已CLOSED/原终局Native+backup，v10/v11也分别CLOSED/Native+backup+containment，不重发其cleanup或把失败窗拼进v12。

下一次用户要检查时先查**原v12 STATE、SQL、soak、原cleanup请求、原finalizer/终局Native+backup和原期限**，请求结果未知查原RID；只有真实连续86400秒+严格报告+最终Native全flat/账本+备份均过才推进阶段资格。别因用户说“一天了”直接重开策略或把elapsed wall算通过；也不把空账户故障测试当持仓故障证明。

# 最新：初始对账修复已Linux1625全过并实际部署；v12唯一cutover已提交，先查原身份，不重发

**新public已真实部署完成，不再重复全测/安装。** entropy-grid-initial-reconciliation-linux-complete.service terminal0 /**1625passed0skip/510.41s**，XML SHA **dca7cea280fac75695e95cee61165edbb01d793c123dffebe8929a948539fbcc**。唯一源install terminal0、grid-initial-reconciliation-install-proof.json installed/finishedtrue/legacyunchanged，version-proof qualifiedfalse/current66SHA/**8delta**（原7+runtime.py）/324包SHA **7cf83c59d4c35fc82e7bee8f4c1bae86273fe6a5e9a8a34a097d46bd897d4cee**。GRID0/emptygrants，runtime **1798805/watch1798806**；旧套利 **1223049** 未变。XML/install/version已scp本机。Mac1623pass2skip117.10s（sourcehelper10s在两个历史永久缺失case各真实等待，未缩短生产门槛）。

**v12实际private Linux135passed0skip** XML SHA **90b3566790887599666d879db58b98c0e6ef808397a44bd4aecbbb8c3c84ad58**；34workflow/6HTTP/7guard/systemdverify/新alias真实AccountExecutor优先调度检查全0。新 **grid-linux-recovery-supervision-review-v12.json SHA a4daccce426b1040f2f0a35f1a1f28b36ec178e87a8016283791650a47d2d0c0** 绑定12privateSHA和当前sourceversion。只有一个新RID **ordinary-observe-linux-20261023-v12**，**data/run/grid-v12-dispatch.py已提交一次（本机exec4929可能需poll）**；once receipt grid-v12-cutover-dispatch-attempt.json已落盘，禁止重新运行dispatch/create/start/cutover。查询原 entropy-grid-ordinary-linux-v12-cutover.service 和只读 data/run/grid-v12-progress.py；若网络结果未知只查原identity。当前尚未读到gid/RUNNING/Native-positive，不能说验证已开始或已过24h。

资金dispatch之前同exec先启动了只读UI负载 **entropy-grid-v12-ui-read-check.service**（data/run/grid-v12-ui-read-check.py，只GET详情/market，约6秒、900s循环/unit960s限时，绝无资金POST）。等v12STATE创建后保持网页行情负载，Native-running proof或controller结束后自行收尾。proof grid-v12-ui-read-check.json。不要重启副本/不要关网页掩盖调度问题。新controller/finalizer/25h timer将在原cutover fresh3Native+formalbundle+preflight通过后once启动；40capital/20reserve/ETHlong3cross/5grid/150gross/30service/Native600startup/25h/24h严格均保持。

v11已真CLOSED/原cleanup1b160...succeeded/Native0+backup0/finalizerfinishedtrue，再freshNative+backup+containment finishedtrue恢复GRID0并停仅v11timer；本机原STATE/Native/finalizer/cutover/containment/UIproof均已scp（57881terminal0）。净 **+.029154 USDC**，不是普通网格配对盈利，也不是启动通过。v11UI只读最终 **detail20028/market2002+5031/maxdetail2.0838s/financial0/finishedtrue/nativeRunningfalse**，行情可被延后/超时而详情仍响应，不能据此伪造Native启动通过。v10也CLOSED/Native/backup/containment，净-.246006；v9已修正真实终态CLOSED/终局，原亏-.972565。所有失败身份/24h窗保留，不复用。

本轮最新只读Native启动watch **data/run/grid-v12-watch-start.py** 刚启动，exec见最近工具结果（预计600s）；后续poll它或读原progress。所有旧watch98269/53110均terminal不要poll，旧source/full/install/资金units均terminal。AI linux heartbeat/goal为用户省token保持paused，无新AI定时跟进；禁止本机gridwriter或操作旧/root/code/entropy目录/服务。接下来必须等真实RUNNING+独立正仓/开仓挂单/NativeSTOP OPEN/稳定SQL，再完成一次启动验收并写最终报告，不提前写24h通过。上游CLOSE根因仍未唯一确认。

# 最新：v11首次成交索引延迟触发失败，已CLOSED/Native+backup+GRID0；新有界初始对账包Linux全量与安装链在跑，v12未启动

**当前无活动资金campaign，GRID0/empty grants。** 不重放v11。gid g-5de1790be67d4652 /start05185acf-334d-47cd-9426-e9b535f81a2a failed initialize，实际error **INITIAL_POSITION_UNRESOLVED: initial position history is not reconciled**；seq5495初始reportgaps OWNED_ORDER_UNRESOLVED+POSITION_MISMATCH，随后seq5496/5497原IOC两笔Nativefill真实入账，再seq5498 cleanup对账ok。即ACK后Native订单/历史索引尚未可见，但仓位先已变化/stream稍后到达，不是priority Native预算再次饥饿。诊断原完整data/run/grid-v11-startup-failure-diagnostic.json已本机/远端。原cleanup **1b160b81-0058-4a11-b4e6-89387cc428ae /ordinary-observe-linux-20261023-v11-cleanup succeeded**，CLOSED/STOPPED/e2/2FILLED+1CANCELED，无GTC网格单/无新UNKNOWN，原finalizer Native0+backup0/finishedtrue。cutover一次cleanup返回ORIGINAL_CLEANUP_OWNER_ACTIVE而非第二POST；原owner最终成功。v11只读本机watch53110已terminal1，不再poll它；原服务器UI只读test有19detail200/market2001/maxdetail.846s（尚需看最终文件finished）；绝不当Native启动成功。scp57881正在复制本机原STATE/Native/finalizer/cutover/containment/UIproof，需poll是否terminal。

唯一 **entropy-grid-ordinary-linux-v11-contain.service terminal0** 另fresh11DEXflat+backup，停v11timer保留合同、清grants/GRID0、仅gridruntime/watch重启；proof grid-ordinary-linux-v11-failure-containment.json finishedtrue，实际runtime **1793272/watch1793274**，旧套利 **1223049** 全程未动。所有v11金融units均terminal，不复用身份。

新增public仅 **entropy_arb/grid/runtime.py**：`_reconcile_initial_position` 对一个已ACK的初始IOC做**最多10秒**的只读对账；原CID/epoch/grid/qty/side/ACTIVE/STARTING-or-NEEDS/stream_generation+connected严格fence。仅同初始CID的OWNED_ORDER_UNRESOLVED/FILL_CUMULATIVE_MISMATCH及POSITION_MISMATCH可短暂重查；其他历史/资金费/foreign/stream错误立即失败。期间只pump(dispatch=False)+原完整recover，不下新IOC、不制造fill、不改数量，必须fullbarrierok才回STARTING并继续GTC。超时若recover中断留下RECOVERING，guarded转NEEDS/PAUSED，原Native保护/30s/启动600/25h均不延长；epoch/用户操作优先。新增8真实生产引擎+Native-format离线case覆盖both/history/stream迟到、永久缺失、foreign、pause/generation、挂起timeout；21related全过，原代码正向both真实失败。Mac新隔离完整 **1623pass2skip/117.10s**。

**新干净包324文件** data/grid-code-checkpoints/grid-code-20261023-initial-reconciliation-v1.tar.gz **SHA7cf83c59d4c35fc82e7bee8f4c1bae86273fe6a5e9a8a34a097d46bd897d4cee**，从3a3c priority包仅替换runtime.py+新test_grid_initial_fill_propagation.py。stage data/run/initial-reconciliation-v1-stage。本轮其余已部署stop终态/预签8s/保护容量/ReadBudgetforeground修复全部保留。

**唯一原 Linux全量 entropy-grid-initial-reconciliation-linux-complete.service PID1794573** 正在跑，真实预期 **1625pass0skip**，禁止重复；**唯一原GRID0安装链 entropy-grid-initial-reconciliation-install.service PID1794582** 已--no-block提交一次，等原全量0后freshNativeflat+backup，旧当前66sha=grid-execution-priority-version-proof、当前变化仅runtime、新324manifest/1625XML，source-onlyrollback，只grid3服务。将产grid-initial-reconciliation-install-proof.json +version-proof.json（qualifiedfalse，原v35的66源码中**8项delta**=原7+runtime）。只读查询 **data/run/grid-v12-source-progress.py**；最近本机71491已terminal0，Linux进度~62%；不要重跑v12-linux-tests/installer。Mac58170已terminal0，dispatch3037/39365也terminal0。

已本机准备全新 **v12** RID ordinary-observe-linux-20261023-v12。data/run/*v12*、planning/grid/tools/*v12.py；preflight严格新initial-reconciliation1625 actual Linux/install/version +8delta/66表，fake tests8+58=66；本机110controller/deadline/supervision/version/readiness、6HTTP/7guard已通过。**v12 private未传Linux/未135Linux/未34workflow/未review/未candidate/未STATE/未dispatch**。待新source实际部署成功，执行 data/run/grid-recovery-supervision-v12-verify-install.py，然后grid-v12-prepare-workflow.py（包含新alias只读真实executor check grid-initial-reconciliation-executor-check.py，避免覆盖v11同名证据）。审阅真实新12SHA review和sourceproof以后才唯一新financialcutover；v12-dispatch尚未创建/执行。UI只读并发测试源码data/run/grid-v12-ui-read-check.py已准备，未武装，未来同v11在资金前once启动并保留网页负载，Native-running后结束。资金范围40capital/20reserve/ETHlong3cross/5grid/150/30/25h及Native startup600严格不变，源新同步等待仅10秒不补开仓。

仍须actual RUNNING+独立held/entry/原生stopOPEN/稳定SQL才报告启动成功，更不能提前说24h通过。旧v9真CLOSED/终局完成，v10真CLOSED/终局/containment，v11也同；所有失败窗均独立保留。上游CLOSE根因仍未唯一确认。用户为了省token的AI linux heartbeat/goal继续paused，不新建AI自动跟进，不重启本机gridwriter，不同步旧套利目录。报告2026-10-23-stop-recovery-deployment.md只写v10历史，后续要写最终source及v12实际结果。

# 最新：priority补丁实际Linux1617+安装成功，v11唯一cutover正在提交（先查原身份），网页读取测试已武装

**新public源已实际部署成功，不再重测/重装。** 原 entropy-grid-execution-priority-linux-complete.service terminal0/1617passed0skip/526.47s，XML SHA **1efd220d2db58b306babb33793c208061b2fb1a1de0ced246fa6b745696fe2e2**。唯一原安装链 entropy-grid-execution-priority-install.service terminal0，grid-execution-priority-install-proof.json installed/finishedtrue、version-proof.json qualifiedfalse/66source/7delta（原6+hl_rate）/323包SHA3a3c...一致；runtime1785645/watch1785646/旧套利1223049未变，GRID0/空grants。实际XML/install/version已scp本机。新增真实AccountExecutor离线priority检查在本机和实际安装Linux也通过：普通大预留等待时UI22读取等待，Native160 stop先行，仍public550/total900/reserve350，无金融操作。脚本data/run/grid-execution-priority-executor-check.py已纳入v11workflowprep。

**新v11 private实际Linux135/0skip** XML SHA **e962b350a24c91a25ee91067ba87a209b165f0c0fb231bb881f19f9a33458543**；另34workflow+6HTTP+7guard/systemdverify/真实executor check全0。新review **grid-linux-recovery-supervision-review-v11.json SHA e2dabe41ed71fc50d6a54af39694f02c689ea73c8757cb8493a1af64381c14d5** 绑定当前12privateSHA和新sourceversion。新RID **ordinary-observe-linux-20261023-v11**。唯一data/run/grid-v11-dispatch.py正在本机exec97002里首次提交；once receipt grid-v11-cutover-dispatch-attempt.json已先创建，**禁止再运行dispatch或重放v11 create/start/cutover，先poll97002和只读grid-v11-progress.py**。若网络未知只查entropy-grid-ordinary-linux-v11-cutover.service /原proof，不重复POST。尚未查询gid/金融启动/Native正向证明，不可说已通过启动或24h开始。

同一exec在资金dispatch之前已先提交**只读网站负载核验 entropy-grid-v11-ui-read-check.service**（源码data/run/grid-v11-ui-read-check.py；只GET、detail与market两个worker、约每6秒、900s循环/unit960s上限，绝无POST/发单），等原v11STATE创建后持续读真实网站，Native-running proof或controller结束后退出，证明无需关网页才能初始化。proof grid-v11-ui-read-check.json；若它还运行，独立只读，不重启副本；金融owner原controller/finalizer/25h仍唯一。新40capital/20reserve/ETHlong3cross/5grid/150/30/25h、原600s启动Native检查都不改。不重启本机runtime/不改旧套利/不恢复用户省token的AI heartbeat或paused goal。

后续查原fresh Native3项/formalbundle/preflight/install/web/RUNNING/独立正仓+entry+原生stopOPEN+稳定SQL再给用户启动结论。v10已CLOSED/Native+backup+containmentfinishedtrue且空仓；净 **-.246006USDC**，不能算24h或普通网格盈利循环。本机已复制原STATE/finalizer/Nativeproof/cutover/containment；旧v9收尾真CLOSED/终局完成且原亏-.972565。报告 planning/grid/reports/2026-10-23-stop-recovery-deployment.md已更正v10失败，最后要写一份最终priority部署+真实v11启动结果，不引用过期“初始化中”当当前状态。

# 最新：v10启动遇UI额度饥饿，已原请求CLOSED/Native+backup+GRID0收尾；新执行优先调度包Linux与安装链原unit在跑，先查它们

**没有活动资金campaign；不要重放v10，v11还未启动/未复制private至Linux。** v10 g-912a518184e846b5 /original cleanup **0145f678-755d-4332-b603-bee8e51c30ac /ordinary-observe-linux-20261023-v10-cleanup succeeded**，原start9ce...failed initialize。Nativechecker startup600s超时，controllerSTART_NOT_RUNNING，根本没有RUNNING/soak。原finalizer finishedtrue/Native-audit0+backup0；2FILLED/2CANCELED/4EXPIRED，无未决。唯一新 **entropy-grid-ordinary-linux-v10-contain.service terminal0** 完成又一次fresh11DEXflat+backup，停v10timer保留合同，policygrants[]/GRID0，只gridruntime-watch重启。实际runtime **1780279/watch1780281**，旧套利 **1223049** 未变。proof grid-ordinary-linux-v10-failure-containment.json finishedtrue。scp78946在复制原STATE/终局Native+finalizer/cutover/containment，若未terminal继续poll。本机只读v10watch98269已terminal0(有界结束不是成功启动)，不要再poll。

**新的实际阻塞**：v10原stop16:43:30 accepted，initialIOC16:44:32 FILLED，第一普通sell16:45:33 ACCEPTED，此后其余4PLANNED一直未发直到startup截止。实查本地RPCdiagnostics只有原start堆栈 `_handle->_m_start->start_grid->activate(line838)->submit_intent(line268)->__aenter__` 等提交读取容量；rolling used376/limit900/publicceiling550，无rate_limit/cooldown/锁占用，Native2/约2s，行情22/约6.5s和instrument40/约30s持续消耗。普通350预留须used<=200，页面小请求使这个条件永不出现。源码_m_detail/_public_observation market TTL5/详情refreshMarket证实UI轮询路径。没发生新UNKNOWN/本轮无stop重建错误；不把它算修复后的24h成功。

已新增 **执行读取优先调度**：ReadBudget.execution_read_sequence在原public550天花板登记foreground，暂缓新的UI/background scope直到提交准备完成；允许已进行读取scope收尾以免持transport锁死锁；保护/紧急平仓critical900/预留350原样不变，取消/异常退还等待和未用额度，read_capacity public快速路径也识别等待。HyperliquidTransport转发此接口，executor普通HL准备使用，Native160/emergency350路径不变。只public另改 **hl_rate.py/adapters/hyperliquid.py/executor.py**，不改签名/epoch/30s/25h/24h门槛；原止损终态/容量修复保留。新8回归+原related合39通过；旧ReadBudget真实正向测试1failed（UI先行造成大请求饥饿），log grid-execution-priority-baseline.log。新干净包从d9b7...归档提取仅替换这4文件，**323文件/SHA3a3cbdd77bea8994672655b21feedd3fda0200d6d75ac1d9161ce47939bcbd10**，data/grid-code-checkpoints/grid-code-20261023-execution-priority-v1.tar.gz，stage data/run/execution-priority-v1-stage。Mac完整 **1615pass2skip/99.15s**。所有旧core修复1609已上，但本新priority补丁尚未部署。

**唯一原Linux全量 entropy-grid-execution-priority-linux-complete.service** 已用--no-block提交，真实应 **1617pass0skip**（原1609+8），正在跑（~8分钟，禁止重复全测）。**唯一原安装链 entropy-grid-execution-priority-install.service** 已提交一次，先等原全测0，再fresh11DEXflat+backup+GRID0/SQLclosed/新323manifest/旧66当前source审查，只grid3服务部署，有source-onlyrollback，产grid-execution-priority-install-proof.json /version-proof.json（qualifiedfalse）。相对原v35核心delta变为**7**（原6+hl_rate.py），源表仍66。查询只读 **data/run/grid-v11-source-progress.py**，本机latest查询session26342需poll；不要重跑grid-v11-linux-tests.py或安装。原安装脚本 data/run/grid-install-execution-priority-v1.py，sourceinstaller dispatch90747已terminal0。

已本机准备全新 **v11**：RID ordinary-observe-linux-20261023-v11，data/run/*v11*及planning/grid/tools/*v11.py；新preflight/version tests严格绑定 **execution-priority 1617实际LinuxXML/install/version、7项delta、66表**。本机controller+deadline/supervision/version/readiness **110passed**（session88564terminal0）；没有v11Linux135/private34/6/7/新review/candidate/STATE/financialstart。准备器 **data/run/grid-recovery-supervision-v11-verify-install.py** 和 **grid-v11-prepare-workflow.py** 尚未执行；必须待新public源实际部署完成+version+旧套利/GRID0，再跑它们，审阅生成新12SHA review和真实报告后才唯一新cutover。资金范围原40/20reserve/ETHlong3cross/5grid/150/30/25h不变；不延长startup600、不关网页掩盖UI问题，不把空仓充作Native正向证明。新切换以后要真实held+entry+NativeSTOP OPEN稳定SQL，再计24h。上游CLOSE仍root未知。AI heartbeat/goal保持paused用户省token；仅服务器finite验收，不新AI自动跟进。

# 最新补记：新v10已真实成交，仍初始化；独立25h合同已安装，不重发

实际新gid g-912a518184e846b5，原start9ce8437f-572f-4762-86d1-f2b8e61afa7d仍running/initialize，最近ACCEPTED2/FILLED1/PLANNED4/STARTING e1；尚无观察起点或Native-running证明。本机只读watch session98269继续（最多600s）。服务器原cutoverPID1774073、controller1774545、finalizer1774543；runtime1774172/watch1774174，旧套利1223049。原installer/fresh3Native+formalbundle+preflight都0/finishedtrue；网站认证200、匿名401、metricsproduction/GRID1。25h合同data/run/grid-ordinary-linux-deadline-v10-contract.json SHA **44365aa988efe2aacafc5c74ef29ae98983d88662f7b214c797445711469c8ca**，due **2026-10-24北京时间01:42:30.435**，timeractive/waiting。新135LinuxXML/review/34workflowXML/freshreview/installer/合同已复制本机（scp98048 terminal0）。不要重复部署/测试/资金请求；仅继续原启动和Native核验。报告草稿planning/grid/reports/2026-10-23-stop-recovery-deployment.md需在实际RUNNING/独立Native结果之后重写当前状态。

# 最新资金状态：唯一新v10已创建g-912a518184e846b5，原start运行中，不重发

**新gid g-912a518184e846b5 /RID ordinary-observe-linux-20261023-v10**；原start **9ce8437f-572f-4762-86d1-f2b8e61afa7d** running/initialize，STARTING/ACTIVE/e1。已实际dispatch一次，grid-v10-cutover-dispatch-proof.json submitted_ms1792687278010。尚未RUNNING/无observation_started_ms/无Native-running proof，不能称24h开始。唯一只读有限watch **本机session98269 /data/run/grid-v10-watch-start.py** 正在最多600s跟原状态，后续write_stdin；原服务器cutover/controller/finalizer/25h timer由原链自行负责，勿启动副本。当前网站public认证site200/metrics200/production/real_dispatchtrue，匿名401（本机Python最初缺模块/默认CA未配置，改用项目venv+certifi后实际成功，没有禁TLS或改网站认证）；publiccheck证据grid-v10-public-access-check.json。应用已请求打开真实新gid详情。所有core修复/原v9CLOSED/native+backup/新Linux135+34/6/7/source1609actualproof均已完成，详下条。旧套利1223049一直未动，AI heartbeat/goal继续paused。

# 最新：新源已完成 Linux1609及实际GRID0部署，原v9真实CLOSED+原finalizer Native/backup；唯一新v10资金cutover已提交，先查原身份

**禁止再次运行 data/run/grid-v10-dispatch.py 或任何v10 create/start/cutover。** 最新用户已授权修复上线并新实盘。唯一新RID ordinary-observe-linux-20261023-v10，unit entropy-grid-ordinary-linux-v10-cutover.service。本机once dispatch receipt grid-v10-cutover-dispatch-attempt.json先落盘；dispatch exec39912可能尚需poll/实际结果见工具。后续只读 data/run/grid-v10-progress.py /grid-v10-watch-start.py 查看原STATE/install/cutover/25h合同/Native-runningproof；未知只查原身份，不重发。新源Linux1609的完整XML SHA **11421d698748bd4a8b68aec4a04813e980b21462b94069fa7056705772c35938**，322干净包SHA d9b7cfa...。

**源实际部署已成功**：grid-stop-recovery-install-proof.json installed/finishedtrue，grid-stop-recovery-version-proof.json qualifiedfalse/current66SHA/6原delta/1609actualLinux绑定，新core改原2文件。只grid三服务源升级，runtime1772520/watch1772521；旧套利1223049前后未变。本机已复制XML/install/version/大Native证据和原v9finalization。原安装首次在只读证据write_private64k限制处失败（steps[]无任何资金/服务/账本变化）；实际已核实原unitfailed1后用唯一 **entropy-grid-stop-recovery-install-resume.service** 恢复，新immutable大证据+直接JSON读，proof保留resume_attempt2/原失败SHA/first_attempt_failure，unitterminal0。不要重跑任何源install/resume/fullsuite。

**原v9不再未决**：Native原stopFilled .0123与原执行/手续费逐条吻合，新适配器终态/经济累计精确匹配后 journal记FILLED（无新fill）。原unknown的完整1181historicalOrders首尾夹住提交期且无CID、完整3fills无该CID、两次全11DEXflat、original CID前后unknownOid，再结合原安装源码包SHA/executor10s限时/defaultsignedexpiry10s可证明包络最晚上界 **1792607651096** 已过；只在强certificate校验后，exclusiveGRID0唯一storewriter按原ABSENT_FINAL路径 UNKNOWN->PLANNED->expire旧epoch，**没有重发/新clientid/修改任何成交库存资金费/forceCLOSED**。证据data/run/grid-v10-expired-absence-native-r2.json（大，禁止全量print）/纯验证器 planning/grid/tools/expired_stop_absence.py 有24反例回归。服务恢复后正常scheduler自行CLOSED（新protection-close第2次正常完成，原失败partial历史保留）；原v9finalizer **finishedtrue/native-audit0+backup0**，未称24h通过。

新v10 private实际Linux **135passed0skip** /XML SHA9676b156c8132cf230b052507e529695627845a39c05e4f885b37cc286a9292f；**34workflow+6HTTP+7guards+systemdverify** 全0，新workflowprep finishedtrue。新review grid-linux-recovery-supervision-review-v10.json SHA **f20427830cd28f44a579837364692749158c2bcfb2773e31ac91e960fb643ac0**，绑定12private SHA及新sourceversion。资金cutover将自行fresh3真实Native+unchangedformal bundle/preflight，安装仅本新40capital/20reserve/3cross/5grid/150gross/30service/25h有限scope，runtime/web就绪后原controller/finalizer，actual正仓+entry+nativeSTOP OPEN核验，最后才可说RUNNING观察开始。新24h不拼接失败v9；上游CLOSE原因仍未唯一确认；不恢复省token暂停的AI heartbeat/goal，不创建新AI监控。

# 本轮进行中：用户已授权修复、部署并重新实盘；新源 Linux1609全过，唯一 GRID0 安装链正在收尾原 v9

用户最新“进行修复，完成后推送上线，继续进行实盘验证”。新 public 仅改 adapters/hyperliquid.py 与 protection.py：真实触发成交后的同CID/OID market child终态验证/经济累计只供历史比对（不生成fill），发送前准备8s独立超时明确未发送，保护容量避免恢复时重复加已有仓位。新增20核心回归 +24只读operator证据验证；旧代码3个正向用例真实失败。干净包从82f46712...原包解压仅替换这5文件，322文件，新包 data/grid-code-checkpoints/grid-code-20261023-stop-recovery-v1.tar.gz SHA d9b7cfa66d61a2a56bb29aaae257808daec69718fed5c58dc6074ecdd383599b。隔离stage data/run/stop-recovery-v1-stage。Mac1607pass2skip，Linux原 entropy-grid-stop-recovery-linux-complete.service **terminal0/1609pass0skip/494.52s**（含原planning review15项，故比原1550增59）。真实XML grid-stop-recovery-linux-complete.xml。

**唯一新安装链 entropy-grid-stop-recovery-install.service 已提交一次，先查原状态，禁止重放。** 本机只读工具 data/run/grid-v10-source-progress.py。最新PID1770225/activating，proof grid-stop-recovery-install-proof.json finishedfalse/installedfalse/steps[]，正在新fresh Native证据；尚未停服务/改账本/安装。原Linux完整已过，链会自行收集原unknown的过期包络上界+首尾夹住提交时段的1181条未截断historicalOrders+完整3fills+两次11DEX空仓无单，验证planning/grid/tools/expired_stop_absence.py后备份，停仅grid三个服务并确认唯一写者，再以新适配器真实FILLED .0123与原成交/累计精确匹配将原旧PARTIAL记FILLED。UNKNOWN仅在强证据certified ABSENT_FINAL后走原journal UNKNOWN->PLANNED->expire旧epoch，绝不重发、不给resubmit权；不改任何执行/库存/资金费/预算/不forceCLOSED。安装322干净包后GRID0恢复，让正常scheduler收尾CLOSED，再产新actual version proof。首次只读collector因ctx键名修正后SSH55s超时，但远端继续完成原candidate（未资金操作）；新安装链会自己重新fresh证据，禁止据本机超时重放安装。安装失败仅查proof/私有log并修复原路径，不盲目重跑。旧套利PID1223049仍active未动；GRID0/empty grants，本机无writer。

已准备全新v10有限工作流本机data/run/*v10*，RID ordinary-observe-linux-20261023-v10，全部v9->v10且新preflight绑定stop-recovery新源1609 actual XML/install/version（6项相对v35的delta仍原6，因为本次改原2文件）。本机76 controller/supervision/deadline及34workflow/version/readiness通过，**尚未复制新private到Linux/未135Linux审阅/未授权grant/未create/start**。准备器 data/run/grid-recovery-supervision-v10-verify-install.py、grid-v10-prepare-workflow.py 尚未执行；必须等原v9实际CLOSED/原finalizer Native+backup/新sourceversion真实审阅后，再Linux135+34/6HTTP/7guard、review、新fresh3Native/formalbundle/preflight、唯一cutover新40capital/20reserve/150gross/30service/25h。不复用旧失败v9，不弱化24h、不拼接窗口，不恢复省token暂停的AI heartbeat/goal。此时本机最新exec见工具；原full本机session18770已terminal0；Linuxfulldispatch因systemd Typeoneshot等待10秒超时，但原unit实际已跑完1609，绝不重复。新版source安装unit已--no-block提交，查询它即可。

# 最新覆盖：用户10月22日要求一天验收，v9失败；当前Native已平仓无单，GRID0/空grant，保留未决状态待修复

**不能称v9通过24h，不能重启新资金轮次或重放原清理。** g-fecf202d3d044c32 /ordinary-observe-linux-20261021-v9 最长严格观察 **10206.669000148773s（2h50m6.669s）**，soak2041samples/issues GRID_EPOCH_CHANGED2+GRID_NOT_RUNNING16。实际起21日23:42:38.935，22日02:32:48.310再遇HL_ACCOUNT_STREAM_INTERRUPTED frame=CLOSE close_code=1000 generation1，seq457恢复e2；seq458/459于02:33:49/50对账ok，恢复重建后新保护intent UNKNOWN，没有成功恢复记录。原controller SUPERVISION_GRID_STOPPED，02:37:14.242退出，原cleanup **66249bc8-e2b7-42e6-98e5-8d8f9360a9dd /ordinary-observe-linux-20261021-v9-cleanup partial/cancel_orders**（reconciliation blocked flattening），不能重发。

**实际平仓发生在22日10:28:07.944，原生止损成交0.0123ETH**，并非02:34自动退出成功。Native原OID581629066428 /CID...0246为filled，book旧保护intent64a5a64b-640d-4bef-ba2b-989f12dcd649仍PARTIAL（qty_bound.123/cumulative.0123），适配器_validate_position_stop把成交后isTriggerfalse/triggerPx0的真实Native对象按在挂trigger验证，持续HL_POSITION_STOP_MALFORMED。新重建保护intent **ec144a0b-1f19-40b6-9090-3e681319e1ec /CID0x978dfbcc2070f4b7000000000000024d** 仍UNKNOWN，无OID，直接Native orderStatus unknownOid；此状态不能据此假标REJECTED/CANCELED。原protective-close **a114a72a-76ea-4f00-9281-2bbddfe1ab9b /protection-close:g-fecf202d3d044c32:5:1 partial/cancel_orders**，保护record phase reconcile/expected_epoch6。当前desiredSTOPPED、NEEDS_ATTENTION，后台对账中会临时RECOVERING；账本net0。

**本次已两次直接Native查询11DEX全空仓无单**，最新post-containment proof observed_ms **1792682668280**。3笔Native成交qty/px/fee逐笔匹配；11笔Native ETH资金费-.004208与账本一致。grossrealized-.94095、fees.027407、funding-.004208 => **net-.972565USDC**，grid_pair_qty0。没有普通完整网格盈利循环，不能当认证通过。

已fresh11DEXflat+私有在线backup后执行 **flat-but-unresolved containment**：空grants/GRID_REAL_DISPATCH0、停仅v9deadline timer、合同SHA **c25052fc527dda0121f2b95762846df38d77252dca99f5f18c55c7e185744ba4** 保留不变；只gridruntime/watch/web重启，未知记录/账本/原请求一字不改，不forceCLOSED。实际proof **grid-v9-flat-unresolved-containment-20261022.json finishedtrue**（首次后置检查未确认，之后只重查未重放任何动作，标initial_postcheck_unconfirmed+postcheck_rechecked_without_replaying_actions）。实际runtime **1744074/watch1744076** fresh健康、RPCmetrics false；旧套利 **1223049** 前后不变active，未动旧目录/服务。原finalizer仍finishedfalse等CLOSED，不是成功终局审计；本次有独立Native与备份证明但不能把它冒充原finalizer完成。AI heartbeat/goal继续paused。

本轮仅检查和撤销失败轮新增权限，无新金融请求/无核心源码修改/无重跑测试。所有本机exec均terminal。最新证据已复制本机：**grid-v9-day-native-inspection-20261022.json、grid-v9-day-event-inspection-20261022.json（完整事件很多，禁止全量print，读摘要或first/last）、grid-v9-day-ledger-review-20261022.json、grid-v9-day-funding-review-20261022.json、grid-v9-flat-unresolved-containment-20261022.json、grid-v9-post-containment-native-flat-20261022.json**，以及原最新STATE/soak/finalization。报告 **planning/grid/reports/2026-10-22-v9-acceptance.md**。后续用户若要求修复，先处理Native已成交position-stop的严格终态识别、UNKNOWN原提交证据和保护退出闭环；绝不弱化Native/正式门槛、不把unknownOid直接清空，不复用失败身份，不开新钱轮直到实际修复+回归+新审阅。上游CLOSE原因仍未唯一确定，多交易所产品/24h资格都未完成。

# 最新覆盖：v9实际启动和运行中Native正向核验均成功，原有限观察继续；用户明晚回来验收

**当前唯一活动网格 g-fecf202d3d044c32 /ordinary-observe-linux-20261021-v9 /RUNNING-ACTIVE-e1**，原start **5ed3a068-84bb-412c-af5d-8d5e9707dc96 succeeded**。原cutover **grid-ordinary-linux-v9-cutover-proof.json finishedtrue、freshreview0/installer0/Native-running0**。真实 **grid-ordinary-live-linux-v9-native-running-proof.json** 已产出并复制本机：正仓 **0.0123ETH**，6workingorders含2entry，Native stop **open**，cross3，gross **51.4345<=150**，前后SQL与实际runtime身份相同。不是空账户证明，也不是24h已过。

**实际观察开始 2026-10-21T23:42:38.935000+08:00 /1792597358935**，最新soak issues{}、longest85.838698495s、supervision未invalidated。建议用户 **2026-10-22北京时间23:50后**回来验收（满24h最早23:42:38后，届时仍按实际双时钟/严格observer/原终局Native+backup判定，不按日历通过）。原immutable25h timer **entropy-grid-ordinary-deadline-linux-v9.timer active/waiting，due 2026-10-23T00:35:37.375000+08:00**，合同data/run/grid-ordinary-linux-deadline-v9-contract.json。服务器原controller/finalizer继续，不需AI跟进；用户省token的linux heartbeat/goal保持paused，未创建或恢复自动化。**不重发任何v9 create/start，不再开新轮、不重跑已过测试或源部署。**

最新实际runtime **1240640** /watch **1240642** /web **1241009** fresh健康同instance；旧套利 **1223049** 本轮安装前后未变，未操作旧目录/服务。网站 https://taoli.coinfetcher.xyz/grid/ 和认证metrics均200/production/real_dispatchtrue；应用已请求打开真实新gid详情，登录信息已按用户明确提问在聊天答复，别再输出密码。

**所有本机exec已terminal**：有限启动监控67812 terminal0、终审70734 terminal0；不再poll旧session。最新完整实际源proof/STATE/Native/soak/合同/正式review/旧v8终局与containment共多份均已复制data/run；最新摘要 **grid-v9-startup-final-review.json**，只读工具 **grid-v9-progress.py** 可下次查原身份。源66SHA与1550原Linux完整版本逐一一致、12新private SHA与review相同，新135Linux/34workflow/6HTTP/7gate全0。人类报告 **planning/grid/reports/2026-10-21-v9-started.md**。

原v8已CLOSED/stop_flat7444...succeeded/Native11DEXflat+backup/containmentfinished，原v7创建前失败无资金动作且已contain。失败轮次不拼进v9，整体目标与正式24h资格仍未完成。下一次用户要求检查时只读原STATE、SQL、observer、原Native-running/最终Nativeproof、finalizer/backup/原期限，再按实际结果处理；未知请求先查原身份，不重复下单。

# 最新进展：唯一v9已创建g-fecf202d3d044c32，原start运行中，有成交；切勿重发

**当前 g-fecf202d3d044c32 /ordinary-observe-linux-20261021-v9，原start 5ed3a068-84bb-412c-af5d-8d5e9707dc96 running/initialize、STARTING/ACTIVE/e1**，最近ACCEPTED2/FILLED1/PLANNED4，尚无observation_started_ms或Native-running proof，不能说24h观察已开始。新价格窗口mark-40至mark+60已通过真实preview并实际创建，资金及保护边界不变。先等原初始化，不重复create/start/cutover。

本机唯一只读有限启动监控 **exec session67812 /data/run/grid-v9-watch-start.py** 正在运行（15秒只查询、最多600s），继续write_stdin取变化；不产生资金操作、不替代服务器controller/timer。其最新快照在grid-v9-latest-progress.json，亦可运行grid-v9-progress.py仅只读。旧v8session58011已结束1，其他本机阶段exec已结束；v8旧grant/timer已contain停用，旧套利1223049未操作。原v9unit已实际dispatch成功一次，不能重复；下一步核实真实timercontract、actualNative正向proof和RUNNING观察起点，再给最终启动结论。AI automation/goal继续paused。

# 最新覆盖：v8已CLOSED并Native/backup+containment完毕；唯一v9新cutover正在推进，先查原身份

**不复用v8。** g-e2ae81682d554da3最终CLOSED/STOPPED/e2，原start和cleanup7444b8da-b4c8-463c-ad6d-fc22dc0887e9均succeeded；4笔FILLED/6CANCELED，无active/pending。原finalizer finishedtrue，Native-audit0/backup0。v8 ordinary观察未过，Native-running在无开仓挂单断言失败，严格门槛保留。源contain初次用STDIN启动时子进程缺PYTHONPATH导致ModuleNotFoundError、proofsteps[]（无服务变更）；已用新只读Native-audit-r2身份恢复原收尾，fresh11DEXflat+backup后停仅v8timer/清grants/GRID0/只gridruntime-watch重启，**grid-ordinary-linux-v8-failure-containment.json finishedtrue /first_attempt_import_failure+resumed_with_explicit_pythonpath真实记录**。runtime1239412/watch1239414/旧套利1223049前后不变；勿再跑contain。

**唯一新RID ordinary-observe-linux-20261021-v9 /entropy-grid-ordinary-linux-v9-cutover.service已提交一次**，dispatchproof grid-v9-cutover-dispatch-proof.json，当前本机dispatch exec25444可能尚需wait输出。不要重dispatch，后续只查data/run/grid-v9-progress.py；若无该本机脚本先确认原dispatch完成后按v8->v9复制只读monitor，不调用任何金融重试。

新v9 Native SQLeligible增加必须有ACCEPTED/OPEN/PARTIAL非reduce-only entry，避免只剩平仓单时进入必然失败的Native审查；原Native正仓/开仓挂单/原生止损OPEN/<=150/稳定SQL断言一字不删。原有限startup600+stable600仍在。为减少初始化时全部entry成交，**验证价格区间改为mark-40至mark+60、5格，原40本金/20reserve/3cross/150gross/3%stop/30sservice/25h不变**，用户已被告知更宽验证区间，预览仍需原VALID/live_eligible。没有额外增加资金或修改源交易引擎。

新v9 Linux **135pass/0skip**，XML SHA **6f29b9586e5db9f9d16feedf0a03fb6e48df4c49b63883f7d0c59548c6ffefdf**，preparedproof grid-recovery-supervision-v9-install-proof.json finishedtrue，controllerSHA **9170d94c5a55f253f34cdd62b5d98d16fa045ded0a30ddecfb678b9dbb23e0a6** /deadlineSHA **2b115ffc8455b8e1ea72e9c74073a403fb8e1b581da155f3aa9a33fbc93ce62b** /helper仍eb7ae741...。另实际Linux **34workflow/version/readiness/cleanup +6HTTP+7diagnosis+systemdverify全0**，grid-v9-workflow-preparation-proof.json finishedtrue；新review **grid-linux-recovery-supervision-review-v9.json SHA985ca8bf4c491d717c97d2365adc07ddc022d474227c7f0bed3814c3f8c760c5**，66public源仍逐一匹配原1550完整版本。v9-verify-install/v9-prepare-workflow已成功一次勿重复。

下一步先核对原fresh三Native/正式bundle/preflight、原installer健康+webHTTP、原controllerstart/25h timer，再等实际Native-running证明。未取得RUNNING与独立正向证明前不要告诉用户“已完成启动验收”。用户本轮已授权网站开放+实盘重启；不要恢复省token而暂停的AI heartbeat/goal。网站登录信息已在聊天按用户要求给出，不再打印。旧套利1223049一直未动，整个产品与24h资格未完成。

# 最新故障：v8实际RUNNING后独立Native核验缺开仓挂单，原cutover已once收尾；先查原清理，别复用v8

**当前v8不能称启动验证成功/24h通过。** 原start d55e073f-9d2c-4eb0-927a-c3e6d0467774 已succeeded，g-e2ae81682d554da3 实际RUNNING/ACTIVE/e1，观察起 **1792596151341 /北京时间2026-10-21 23:22:31.341**，曾soak18samples/issues{}、longest85.912s、账本正仓 **0.0210ETH**，但不是Native独立proof。后来PLANNED从1到0、ACCEPTED6/FILLED3时原Nativechecker开始，断言失败 **grid-ordinary-linux-v8-native-check.py line71 `assert any(not o['reduceOnly'] for o in orders)`**；不是上游WS断线，也不是假定保护失效。

原cutover已finishedtrue/failed_stagenative-running，于1792596272130记录Nativeexit1；通过共享once_cleanup已提交 **7444b8da-b4c8-463c-ad6d-fc22dc0887e9 /ordinary-observe-linux-20261021-v8-cleanup** running/cancel_orders。最近gridSTOPPING/STOPPED/e2，CANCELED3/ACCEPTED3/FILLED3；必须查询这个原请求，不另发stop/start。原controller和独立finalizer会自行完成CLOSED+Native终局+backup。本机只读watch session58011已terminal1，不再poll它。当前新read-only progress exec可能仍在运行，见工具最近session。

已向用户说明：当前挂单只有平仓方向，未满足本轮独立核验要求；按原规则收尾，之后修正为有限等待完整窗口，**不删除/弱化Native必须同时有正仓、开仓挂单、止损OPEN的断言**。下一步先查v8cleanup+finalization，Nativeflat及backup全0后可用v6contain模板新v8独立身份收尾清grants/GRID0并停仅v8timer；勿在仍有持仓时重启runtime/覆盖源码。需离线复现Nativechecker SQLeligible不要求有entry这一遗漏：v8先在只有reduce-only单的正常状态过了eligible、立即触发Native断言。修复应增加SQLentry存在条件并有界等待，Native断言仍保留；若用户已授权这次重启验证内继续推进，使用全新v9身份/新freshreview/新证明，不复用v8金融请求。上游断线原因仍未确认。AI heartbeat与goal保持paused，旧套利1223049本次未动。

# 最新进展：v8已真实创建并开始初始化，有真实成交；先等原启动与Native-running，勿重发

当前 **g-e2ae81682d554da3 /ordinary-observe-linux-20261021-v8**，原start **d55e073f-9d2c-4eb0-927a-c3e6d0467774** running/initialize、STARTING/ACTIVE/e1。最近intent ACCEPTED2/FILLED1/PLANNED4，有实际初始成交和挂单进展；**还未RUNNING、无observation_started_ms/Native-running proof，不能说24h已开始或独立核验通过。** 原独立25h timer已active/waiting，immutablecontract due **2026-10-23T00:15:28.396+08:00**。新web HTTP就绪检查已通过、原installer finishedtrue，legacy1223049前后未变，runtime1232991/watch1232993健康。原cutover1232831/ordinarycontroller1233366/finalizer1233364 active；后续Native-running检查由原cutover继续，不另跑副本。

本机唯一 **data/run/grid-v8-watch-start.py** 只读有限监控正在exec session58011（最长600s，15s查询，只打印变化），需要用write_stdin继续接收；它不会发资金请求，结束不影响服务器原controller/timer。最近原proof/STATE/candidate/review/期限和两LinuxXML已复制本机（10份）；最新state可查grid-v8-latest-progress.json或执行grid-v8-progress.py只读。应用已请求打开真实新grid详情 https://taoli.coinfetcher.xyz/grid/#/grid/g-e2ae81682d554da3 。用户已收到原网站用户名密码，后续不要再打印登录信息。继续验收原初始化、真实Native持仓/挂单/止损/敞口/看护；失败只查原once清理，不重启旧v6/v7或重发v8。

# 最新覆盖：v7在创建前因网站旧RPC连接503退出，已空仓收尾；新v8原cutover已启动，勿重复（2026-10-21）

**当前唯一需要查询的是 v8，不重放v7/v8请求，不恢复AI跟进。** 用户已明确要求开放网站、重启实盘验证，并追问登录信息，已答复原登录。公网Basic仍保留，旧套利未改。

v7原controller在第一次只读网站请求时GateError RUNTIME_UNAVAILABLE，controller_finished_ms已写，grid_id缺失/financial_start_submitted=false；实际DB查v7RID无request_dedup，所有gridsclosed，无资金动作。直接原因是runtime切换后网站持久RPC连接首次请求失败；Web RPC实现明确失败后关闭旧连接、下次才重连，此类时序已补GET重连测试。已停止仅原v7只读cutover等待，fresh Native11DEXflat+backup后清grants/GRID0、重启仅独立gridruntime/watchdog/web，原 **grid-v7-precreation-failure-containment.json finishedtrue**，旧套利1223049前后未变。v7无新金融grid或timer，原证据保留。

新v8 controller只对GET读请求最多3次有界重连（每次最长120s），金融POST仍只一次，未知只查询原RID；installer在runtime健康后重启仅独立web，并实测同路径网站metrics200/GRID1后才启动controller。Native checker若controller创建前已结束则立即失败；仍保留独立正仓/挂单/止损/无PLANNED或UNKNOWN/稳定SQL条件，启动和RUNNING稳定各600s上限。只读核验失败仍共享oncecleanup。

**唯一新RID ordinary-observe-linux-20261021-v8，原unit entropy-grid-ordinary-linux-v8-cutover.service 已提交成功一次**（submitted_ms1792595650567），dispatchproof **data/run/grid-v8-cutover-dispatch-proof.json**。现应查 `data/run/grid-v8-progress.py` /远端cutover/install/STATE/timer/Nativeproof，不再启动副本。此时尚未查询到grid或RUNNING，不能称24h已开始。

新controller+deadline+原helper实际Linux **133pass/0skip**（含6新增GET重试/POST不重试），XML SHA **ab39c8358b85e011dfe2b2d49641fd0a87486b4d037dd624f4a6456e17fdae25**，preparedproof **grid-recovery-supervision-v8-install-proof.json finishedtrue**。另30workflow/version/readiness/cleanup +6HTTP +7diagnosis +systemdverify实际Linux全0，**grid-v8-workflow-preparation-proof.json finishedtrue**，review **grid-linux-recovery-supervision-review-v8.json SHA69220c2cb5bc42ac6d05e275ae023958f586c1913c04801e592c6bf67b7c5267**，12private source SHA已绑定，66publiccore与1550Linux旧完整版本仍逐一匹配。新代码可审阅源planning/grid/tools/*v8.py，回归tests/*v8.py，未覆盖旧v7/v6。准备脚本v8-verify-install/v8-prepare-workflow均已成功执行一次，勿再重跑。

v8资金cutover会自行fresh三Native审核、正式bundle与preflight、backup、独立runtime/web就绪，再controller/finalizer、start前25h timer、实际Native-running；范围40capital/150gross/30sservice/3cross/5grids不变。未放松正式24h，无拼接失败窗。AI linux heartbeat与goal继续paused。当前所有本机exec均terminal；原v8远端unit继续自行推进。需要等实际start/Native证明后再给用户“已启动”的结论。

# 最新覆盖：用户已要求开放网站并重启实盘验证；唯一 v7 cutover 已启动，先查原进程（2026-10-21）

用户明确授权“把网站开放给我，然后重启实盘验证”。已核实公网 https://taoli.coinfetcher.xyz/grid/ Basic正确登录200、匿名401；metrics曾短暂503后200，RPChealthy/heartbeat与watchfresh同instance。用户追问用户名密码，已按其明确要求在聊天提供本机原登录信息（不要在日志再打印）。应用网站/登录文件已请求打开。**不要恢复AI linux heartbeat或paused goal**，用户此前为了省token选择自己一天后来检查；本次只启动服务器有限验证。

**唯一新RID ordinary-observe-linux-20261021-v7，切勿再dispatch或重发资金请求。** `entropy-grid-ordinary-linux-v7-cutover.service` 已真实启动，dispatchproof `data/run/grid-v7-cutover-dispatch-proof.json`。最新原 review-runner finishedtrue：fresh三Native11DEX终局flat核对、正式bundle、preflight均0。原 installer `grid-ordinary-linux-v7-install-proof.json` 还 finishedfalse，steps到reviewed_ordinary_policy_and_grid1_runtime_installed；runtime已变 **1229899** /watch **1229901**，正在就绪检查；旧套利 **1223049** active未动。grants1但尚无v7STATE/gid/controller/timer，不能说已开始交易或24h观察。原cutoverPID1229767 active，fresh-formal-review0。就绪后它会自行启动finalizer和controller、在start之前武装独立25h期限，再做独立Native-running核验。只查询原 `data/run/grid-v7-progress.py` 或远端proof，不重复任何installer/cutover/start。

v7完整工作流已准备并实测：preflight保留原6runtime delta、66源SHA、1550Linux全过证据审核，新增绑定本次已完成127Linux监督修复XML及3实际脚本SHA。新Native checker不放松正仓/无PLANNED或UNKNOWN/Native止损与挂单条件，启动阶段最多600s，首次RUNNING后稳定核验另最多600s，避免上次初始化耗尽核验窗口；二次SQL需eligible且config/owner/context/qty/manifest（含state/cumulativeqty/terminal）完全一致。若该正向核验失败，原cutover通过共享once_cleanup主动结束本轮，不再默默观察一天。已实测额外30 workflow/version/readiness/cleanup cases +6 HTTP +7diagnosis guard，实际Linux全0、systemdverify0。新review `grid-linux-recovery-supervision-review-v7.json` 已绑定12私有源码，reviewSHA ae7008da399a9af387f03efbb5637317f7801e841c8015aac371052d136c0fc9；**66**核心源逐文件核实未变。工作流准备proof `grid-v7-workflow-preparation-proof.json` finishedtrue，上传脚本 `grid-v7-prepare-workflow.py` 已执行成功一次勿重放。

本轮原有限范围仍40USDCcapital/150gross/3cross/5grids/30sservice/25h独立期限，断线可按新监督恢复但严格24h观察永久失败，绝不拼接失败窗口。所有历史v6证据及普通失败身份保留，未同步旧套利目录或源码。新workflow私有文件在本机/远端data/run，原3controller/helper可审阅源仍planning/grid/tools。后续需先查原start/真实Native-running证明/原timer，再确认观察起点；失败则查原once清理/Native终局/backup，不重开新轮。当前活动本机exec均已terminal，不需要继续poll；远端原cutover独立继续。

# 最新：用户要求先修复；断线验收控制修复已完成离线回归并准备在线上，未开启新实盘（2026-10-21）

**当前不得把 v7 当作已运行的 campaign，不重放 v6，不恢复 AI 跟进或暂停的 goal。** 用户本轮仅说“你先修复”，本次无资金请求、无新授权、无 timer 启用、无服务重启。线上 active_grids0/pending_operations0/unresolved_intents0、grants=[]。runtime719771/watchdog719773/旧套利1223049 安装前后同 PID active；旧套利其他时段的独立变更不归因于本操作。

已确认直接退出原因在 **原 v6 私有验收 controller**：CLOSE1000 后 runtime 冻结进 RECOVERING，controller 一遇 epoch 变化即 finally stop_flat，提前切断已有后台恢复。上游关闭的具体原因仍未唯一确认，不声称修复了上游连接。**没有修改正式 soak_observer 谓词或任何已部署核心源码**。新可审阅源 **planning/grid/tools/ordinary_supervision.py、grid-ordinary-live-linux-v7.py、grid-ordinary-linux-deadline-v7.py**，只准备替代有限控制器：真实 stream-gap 事件才允许恢复，需同轮 recovery_checked 无漂移/缺口 + stream_recovery_attempt 成功 + RUNNING 新 activation epoch 才记录恢复成功；每次检测后最多300s、不被重试续期；原 Native保护30s/资金/权限/观察截止/独立25h退出均不变。健康/保护/对账/未知epoch/超时异常仍原once清理，用户pause/stop_keep/context变更优先交还。断线使24h证据永久无效，原observer继续累计问题，不换观察器、不清空或拼接。另修正observer initializing竞态（10s限）、陈旧报告15s限及先报告后快照的异步竞态。

**本机与实际 Linux 127pass/0skip**（新3测试 + stream recovery/protection handoff/epoch/strict observer/native watch），包括模拟正仓真实引擎+DB事件的完整恢复；这不是新实盘正仓证明。Linux XML SHA **8173028dbdfb10a07549f18e6126d25fe8a02287a4278aa693f9b9b00c580ce5**，本机 XML SHA **1b0fdf28d6ca61a1e72393ffeeb8110f6c7eafd66afb6fce980b4ba82cbed87b**。未重复旧1550完整测试；旧公开包82f46712...和当前安装66core逐文件SHA实查全匹配，旧v6 controller/deadline也与本机原始SHA一致。

已将3个 **未运行** 新脚本放到远端 data/run，隔离测试目录 **data/run/recovery-supervision-v7-stage**（旧干净319文件归档+新私有控制器/3测试）；实际proof **grid-recovery-supervision-v7-install-proof.json prepared_only/finishedtrue**、Linux XML/log、终审 **grid-recovery-supervision-v7-final-check.json** 已复制本机。3文件SHA分别：controller **bb1a3602f9967cdc75d14339b5468c685ba3f3ac9300cb18e53c3c538b748b08** /deadline **2efa3d63c1f5b37a438caf25354de869b090f0f3f6c2553c4a92f14d540e846a** /helper **eb7ae74192c8f3866c0af212f25a42d0bb7485ed22c706484de167003978894f**。上传脚本 **data/run/grid-recovery-supervision-v7-verify-install.py 已成功执行一次，勿重放**。目前无运行测试/session，所有本轮exec已结束。

**新RID ordinary-observe-linux-20261021-v7 尚未使用**，无 grid-ordinary-live-linux-v7.json、无 grid-service-linux-v7-reviewed-candidate.json、无新 armed deadline。线上只调用了只读 local_guard，已实证因缺新审阅候选拒绝启动。下一步如用户要求新验证，需重新审阅/新finite资金cutover/fresh Native运行中正仓核验与终局审计；新v7没有完整installer/preflight/native checker，不能调用旧v6 cutover或仅拷旧candidate开启。原v6独立Native-running未完成问题仍然存在，未被本轮离线测试替代，原失败窗口继续失败。报告 **planning/grid/reports/2026-10-21-recovery-supervision-fix.md**。整体目标/24h实盘资格均未完成，AI automation linux 和 goal 继续 paused。

# 补充复查：用户2026-10-21再次要求检查，未发现新交易轮次；实盘仍关闭

实际读取最近grids/operations/intents：最近仍为原v6 g-961b88dff80248a4 CLOSED，最后成交北京时间10月20日02:01:50.604；active_grids0/pending_operations0/unresolved_intents0，grants0/GRID_REAL_DISPATCH=0，v6timerinactive，runtime719771/watchdog719773持续在线。旧套利当前736376 active，本轮仅只读，未操作旧服务。最近24h没有新增grid或实盘交易；不能把服务在线当网格连续运行24h。原最长9827.767秒/连接关闭1000/运行中独立核验未完成等结论不变。
本次另直接只读查询交易所当前11DEX范围：positions0/orders0；证据data/run/grid-latest-manual-review-20261021.json和grid-latest-native-review-20261021.json。未重开资金单、未恢复AI跟进、未改政策/期限/服务。下一步仍需用户后续要求修复断线恢复并开始全新有限验证，不自动复用失败身份。

# 最新验收终局：用户一天后回来检查，v6未过24h；已fresh Native平仓核对并恢复GRID0，AI自动跟进仍暂停（北京时间2026-10-21）

用户仅要求检查，没有授权自动重启跟进或新campaign。本次 **只验收/安全关闭失败轮授权，无新资金订单**。原gidg-961b88dff80248a4已CLOSED/STOPPED/e3，start成功，原cleanup **b737a240-eb12-4441-b4a0-2b610bb429c0 /ordinary-observe-linux-20261019-v6-cleanup succeeded**，无须再POST。
**未通过24h**：真实观察起北京时间10月19日23:16:58.071，最长连续 **9827.76699996秒（2h43m47.767s）**。10月20日02:00:49.104 seq439 stream_recovery_required/e2；已部署脱敏日志直接捕获 **HL_ACCOUNT_STREAM_INTERRUPTED frame=CLOSE close_code=1000 generation=1**。原controller按epochchange fail-fast结束，02:01:51.216清理结束。上游关闭原因/固定周期未唯一确认，不能声称修复；未拼接失败窗口。原Native-running仍缺证明（NO_STABLE_ORDINARY_RUNNING_WINDOW），这次终局空仓核对不替代运行中正仓证明。
**本轮积极证据**：原止损在10月19日23:22:20.088零仓自动取消，23:22:22.391新原生止损确认，**实际重挂2.303秒**（seq432/433），没再发生30s重挂超时。不能把单场景通过当全产品或24h成功。15笔成交/账本与Native逐笔一致，净收益 **-.056565USDC**（手续费.066741/资金费-.001704/净仓0）。
本次原 **entropy-grid-ordinary-linux-v6-contain.service terminal0**，新 **grid-ordinary-linux-v6-failure-containment.json finishedtrue**，fresh Native11DEX全部无仓无单、backup0、SQLallclosed/no pending/no unresolved后，停用仅v6到期timer（保留contract）、清空grants、GRID_REAL_DISPATCH=0。当前实际runtime **719771** /watchdog **719773** fresh同instancehealthy；旧套利 **693437** 本次收尾前后未变，旧目录/服务未改。66核心源码SHA与已测试1550版本全一致；没有重复完整测试/源部署/原资金请求。
本机证据 **grid-ordinary-linux-v6-day-review-snapshot.json /grid-ordinary-linux-v6-day-fault-evidence.json /grid-ordinary-linux-v6-day-terminal-review.json /grid-ordinary-live-linux-v6-containment-audit-proof.json**，原STATE/soak/finalizer/Nativeproof/cutover与新containment均已复制。人类可读报告 **planning/grid/reports/2026-10-21-v6-acceptance.md**。AI linux automation和goal继续paused，不自行恢复。若用户后续要求修复，优先处理上游断开后的安全恢复与持续运行；不能简单重开旧失败轮或弱化正式资格谓词。整体目标未完成。

# 用户安排：暂停AI定时跟进，服务器继续原v6有限运行，等用户一天后回来验收（2026-10-19北京时间）

用户提出为节省token，让线上自行运行一天后再来确认。已通过automation_update将 **linux heartbeat状态PAUSED**，原prompt/15min规则/目标thread保留；get_goal实查原goal也早已paused，无需修改goal。**不得自动恢复AI监控、开启新campaign、重放原v6资金请求或停止服务器原交易/风控/独立到期清理。** 后续等用户主动回来验收。
最近actual **g-961b88dff80248a4 RUNNING/ACTIVE/e1**，原start22eafac3-e4e3-46d8-a27a-6c0551b6b116 succeeded；真实观察开始 **1792423018071 /UTC2026-10-19 15:16:58.071 /北京时间10月19日23:16:58**，soak最近issues{}、最长181.686秒，**不是24h通过**。若中途无中断，满24h为北京时间 **10月20日23:16:58**；建议用户23:30后回来，仍按实际日志/双时钟/终局审阅判定，不能仅按日历或这个预计时间通过。主controller仍有原86460s观察上界和原cleanup_once；独立25h due_ms1792512595227不变，Native终局审计/backup由服务器原finalizer负责，无需AI持续唤醒。
**尚有独立核验问题，须届时检查而非隐藏或默认通过**：原cutover **grid-ordinary-linux-v6-cutover-proof.json finishedtrue/failed_stagenative-running**，freshreview0/installer0，原Native-running checker10分钟返回1。日志grid-v6-cutover-native-running.log为 **NO_STABLE_ORDINARY_RUNNING_WINDOW**，运行中的网格/observer没有因此停止；不能说已有Native-running正仓证明。原checker要求无PLANNED/UNKNOWN等且正仓，而实际启动较慢、持续有成交补单（之前PLANNED普通reduce-only sell一笔），**这支持检查窗口未取得稳定条件，但不能据此默认核验无须通过或直接放松正式门槛**。当前Native-running-proof文件未产出，main原controller仍在观察；后续用户回来先查原STATE/SQL/soak/原Native检查/原终局Native审计和backup，不重跑失败金融身份，确需补核验应是独立只读证明，不伪造旧时间窗或24h。
本次仅暂停AI自动跟进，未停止或重启任何服务器服务/资金程序，也未改持仓、政策、期限。无新下单。保存当前原证据，整体多交易所产品和普通实盘24h资格都未标记完成。

# 最新补充：v6原start仍初始化并有挂单进展，尚未普通24h观察；原身份/25h timer已核实（UTC2026-10-19 15:15附近）

**只查询这个原请求，不重发start或再开campaign。** gid **g-961b88dff80248a4** /RIDordinary-observe-linux-20261019-v6，原start op **22eafac3-e4e3-46d8-a27a-6c0551b6b116** 当前running/initialize；gridSTARTING/ACTIVE/e1，STATEfinishedfalse/error未出现。普通controller **entropy-grid-ordinary-live-linux-v6.service PID181101**，独立finalizer **entropy-grid-ordinary-linux-v6-finalize.service PID181099**，cutover **PID180447** active等原Native-running检查；原installer已finishedtrue（实际guardianhealthy）。**独立25h timer entropy-grid-ordinary-deadline-linux-v6.timer armed、due_ms1792512595227**，原contract不改不延长。
真实SQL进展：前次ACCEPTED1/FILLED2/PLANNED5，最近 **ACCEPTED3/FILLED2/PLANNED3**；没有新保护退出事件。新public容量调度会等待可用额度，仍有实际挂单进展，不能当卡死重放；主controller有原启动期限/原资金limits。**尚无observation_started_ms/soak、也尚无grid-ordinary-live-linux-v6-native-running-proof.json**，不能说RUNNING/启动成功/已经计24h。只读本机180秒启动监控session32638已terminal0，不要继续poll它。
当前actual runtime **180609** /watchdog **180611** 同instancefresh healthy，旧套利 **175301** active；本机已复制7份实际v6review/fresh审阅/bundle/candidate/install/cutover/完整STATE（后两者mutable/尚未finished）。最近快照 **grid-ordinary-linux-v6-latest-progress.json** 与 **grid-ordinary-linux-v6-start-check.json**。新公开1550Linux全过/源码安装/实际版本审阅均已完成，不能重跑；源包82f46712...，6处明确变更。下一步核对原启动/Native-running报告/原cutover结果；若自然失败则查原保护cleanup和finalizer/Nativeflat+backup，之后可执行已有v6-contain.py一次（不是重发金融cleanup）。阶段仍未完成，linux heartbeat保持。

# 最新覆盖：额度修正已真实Linux1550全过并部署/审阅；唯一v6资金cutover正在进行，先查原身份（UTC2026-10-19 15:09附近）

**不要再启动任何源安装/完整测试/金融campaign或重放v6请求。** 唯一 **entropy-grid-ordinary-linux-v6-cutover.service PID180447** active，脚本data/run/grid-ordinary-linux-v6-cutover.py，proof **grid-ordinary-linux-v6-cutover-proof.json**；RID **ordinary-observe-linux-20261019-v6**。最近fresh三Native+unchangedformal bundle+preflight全0，**grid-service-linux-v6-review-runner-proof.json finishedtrue**。原 **grid-ordinary-linux-v6-install-proof.json** 还finishedfalse，steps到GRID1runtimeinstalled；新runtime **180609** /watchdog **180611** 正等实际guardian ready，暂未grid_id/start/普通controller。就绪后原installer会自动派v6controller/finalizer，controller用原RID创建、start前装25h独立timer；不重放未知结果。原Native-running检查由同cutover顺序启动，必须实际正仓/止损OPEN/挂单/<=150成立后才能说启动成功。
原 **entropy-grid-protection-capacity-linux-complete.service terminal0，1550pass/0skip**；实际XMLgrid-protection-capacity-linux-complete.xml SHA **a3e8d52fa349d32e733fb3477e341857090f40e3f2ef6c10aac591cf05296726**。原GRID0 **entropy-grid-protection-capacity-deploy.service terminal0**；新 **grid-protection-capacity-install-proof.json installed/finishedtrue、legacy_unchangedtrue**；**grid-protection-capacity-version-proof.json qualifiedfalse** 绑定当前66core/原v35baseline的6处明确runtime delta/新1550XML/实际新guardian、实际Linux31versiondeadline+15controller+6HTTP全0，部署链另7gate与unitverify0。已真实调用v6preflight.validate_execution_review(root,actualversion) 返回None、全66源码SHA一致（不是只模拟证据），本机grid-protection-capacity-postinstall-check.json。四份实际源安装/版本/部署/XML已复制本机。
已审阅并实际写 **grid-linux-protection-capacity-review-v1.json**，绑定真实新version/10私有SHA/原completedprobeSHA。只允许一轮40capital/150gross/30service/25h独立deadline的v6，不增加IP额度、不延长保护期限、不改变Native权限/严格一致/24h门槛。明确额度阻塞有3条离线复现但v5原记录不能区分容量/锁，仍非唯一线上根因证明；v3stream仍未知/同IP其他进程与对端关闭仍可导致本窗失败，原失败窗不可拼接。新日志只安全phase/type/elapsed，用于定位再现问题。
**旧套利当前实际PID175301**，与本轮源安装/资金cutover时实际before/after检查一致；先前162631为历史，改变原因未核查，不归因于网格（没有任何旧套利stop/restart命令）。不要沿用旧PID或操作旧目录。GRID0源安装后runtime179747/watch179748为历史，当前资金cutover如上180609/180611，后续仍查实际proof。
原v5仍CLOSED/Native11DEXflat/backup/已contain，所有其unitterminal；不复用g-763152e3629e40d9或其失败请求。本轮新源包仍 **82f46712af63505c1185a2402083effb39b7730ec512a0f91bf0026bc13e6f65 /319files**。本机 **grid-ordinary-linux-v6-latest-check.json** 保存最近实际资金状态，v6 STATE/Native-running/最终proof尚待原进程产出，应读取后复制本机；v6-contain.py已准备并在远端data/run（未执行），若本轮失败且原finalizer Native/backup完成，按新actual身份可执行一次，不重发金融cleanup。阶段未完成，保留linux heartbeat，普通24h仍未开始/未通过。

# 最新：保护额度饥饿离线复现并修正；原1550 Linux完整测试/GRID0部署链仍在运行，尚无v6资金campaign（UTC2026-10-19 15:01附近）

**先查唯一原测试/部署unit，不再启动副本。** 原 **entropy-grid-protection-capacity-linux-complete.service PID174227** active（隔离data/run/protection-capacity-v1-stage，RuntimeMaxSec1500，目标1550pass/0skip，XMLgrid-protection-capacity-linux-complete.xml）；原 **entropy-grid-protection-capacity-deploy.service PID175663** active等待原fullunit，proof **grid-protection-capacity-deployment-runner-proof.json finishedfalse/commands[]**。脚本grid-protection-capacity-deploy-after-tests.py/RuntimeMaxSec2100，只原full成功0后7gate+stagedunitverify+GRID0新source安装+实际版本review，不自动开资金单。新install/versionproof文件应为grid-protection-capacity-install-proof.json/grid-protection-capacity-version-proof.json，未出现前不能说已部署。Mac完整exec18215已terminal0；transfer15563terminal0。
本轮起初真实核对：旧v5全CLOSED/无queuedrunning/无未决intents/grants空/GRID0；runtime169029/watchdog169031 heartbeatfresh，旧套利162631未动。v5原start/cleanup只查询不重放。本轮尚无新资金动作/新grid/v6 STATE/candidate/review/timer/普通金融unit。
**离线根因假设已复现但不宣称唯一线上原因**：原critical普通350准备在已有spent500时仍会占用保护reserve并拿到signerlock，普通买/普通reduce-only卖两用例旧代码均失败；常规risk.observe也会占用保护预算，独立旧代码用例失败。当前修改 **executor.py**：HL普通网格准备改public_read_sequence350（非紧急），等待在signerlock之前；真实whole_position native保护仍critical160、exit-flat紧急平仓仍critical350，取消路径不变。**live_risk.py /adapters/hyperliquid.py**：普通风险观察也按public350容量提示/预约，不足仅RISK_API_BUDGET_WAIT、不产生permit；底层900总额度/550public/350reserve/窗口/30s服务期限都未改变。
另executor新增静态 **GRID_NATIVE_STOP_PREPARATION_INTERRUPTED** 日志，只在原生止损取消/超时输出phase=read_capacity/account_lock/submission +elapsed_ms+exception_type，不输出账户/订单身份/请求/原始body/密钥。3个日志用例在旧代码均失败，修正后通过并验证敏感marker不泄漏/锁释放。新共8cases（4额度+1risk+3日志），相关focused119pass。
**新源包** data/grid-code-checkpoints/grid-code-20261019-protection-capacity-v1.tar.gz SHA **82f46712af63505c1185a2402083effb39b7730ec512a0f91bf0026bc13e6f65** /319files，从已部署f159...包解压，仅替换executor.py/live_risk.py/adapters/hyperliquid.py和test_grid_executor_capacity_lock.py/test_grid_live_risk.py；没混入本机其他脏套利改动。新manifest grid-protection-capacity-source-manifest.json；远端source-protection-capacity-v1.tar.gz已验SHA。隔离Mac完整 **1548pass/2skip/113warnings91.73s**，XMLgrid-protection-capacity-local-complete.xml SHA **4b91f674585612ee06e5a60b9fca3875215ecb8bf95aac0de8a71c5a75798c89**。本机grid-protection-capacity-local-verification.json保存真实记录/2+1+3旧失败基线/119focused/31+15+6+7private本机全0；不把dispatch推断成Linux成功。
已准备/上传17份新 **v6** 私有脚本（原v5→v6独立RIDordinary-observe-linux-20261019-v6，不动v35/fault-linux-v1历史）：原25h独立timer/共享oncecleanup/24h双时钟与failfast保持；严格版本门槛更新为原v35 66源码中的**6处**明确runtime delta（原5+executor.py），真实新installproof/1550Linux全过XML。31versiondeadline/15controller/6HTTP/7reviewgate本机实际全0。新源GRID0installer以实际当前service-position-history-version的66source为安装前基准、v5containmentfinished与fresh11DEXflat/backup为前提；只grid服务、保留rollback。实际versionreview在新源安装后跑Linux31/15/6并绑定源和新XML，不称qualified。
v6资金installer硬要求新 **grid-linux-protection-capacity-review-v1.json**，该文件尚不存在；没有v6普通controller/finalizer/cutover/资金unit。下一步原Linux完整和source部署链成功后，必须核对实际新源/1550XML/看护/旧套利、真实调用v6preflight.validate_execution_review(root,newversion)，复制实际proof本机；审阅剩余v3stream未知/额度排队仍可失败的风险后，才可新40/150/30/25h有限campaign。仍要新fresh3Native+unchangedformal bundle/preflight紧接原installer，避免5min过期；不盲重用旧review或失败金融身份。新日志可在未来失败时精确区分额度和锁，而非继续猜测。普通24h与最终资格均未完成，保留linux heartbeat。

# 最新覆盖：v5启动阶段保护重挂超时，已CLOSED/Native11DEXflat/backup并恢复GRID0；当前所有测试/部署/资金unit均terminal（UTC2026-10-19 14:49附近）

**没有正在运行的资金campaign，不要重放v5 start/cleanup或原安装。** gid **g-763152e3629e40d9**，RID **ordinary-observe-linux-20261019-v5**。原start **7874a2b0-bc69-410d-b0c5-78fd28a6f91d** 最终failed/initialize（启动保护触发），根本未到RUNNING/未开始soak，不能说新24h观察已启动。controller **grid-ordinary-live-linux-v5.json finished=true**，GateErrorSTART_NOT_RUNNING，cleanupunknown是当时HTTP结果，实际已由原保护退出完成。第一保护close **2303f8cc-c662-43be-8128-e8e565a73603 /protection-close:g-763152e3629e40d9:2:1** partial/cancel_orders（当时reconcile FOREIGN_OPEN_ORDER），原exit调度器第二次 **4decf8cf-b531-46be-9204-372cde9db44c /...:2:2 succeeded** 后CLOSED/STOPPED/e4，seq418/419恢复检查ok/drift[]/gaps[]。不要另发cleanup。
原独立finalizer **grid-ordinary-live-linux-v5-finalization.json finishedtrue /Nativeaudit0+backup0**；原Nativeproof **grid-ordinary-live-linux-v5-proof.json** all11DEXflat、net+.080022USDC/qty0。随后唯一 **entropy-grid-ordinary-linux-v5-contain.service terminal0** 完成又一次freshNativeflat/backup/SQLallclosed-no未决确认，停用仅v5timer（contract保留）、空policy及GRID0，proof **grid-ordinary-linux-v5-failure-containment.json finishedtrue/legacy_unchangedtrue**。当前实际runtime **169029** /watchdog **169031** fresh同instance healthy，旧套利 **162631** 安装/资金cutover/containment前后未变。全部v5controller/finalizer/cutover/containment units均terminal，原Linuxfull/deploy亦terminal。
**新失败具体证据**：seq414最后fill入账后seq415已进入service_coverage_started（已不在原POSITION_UNVERIFIED位置失败，但没有独立记录可证明本次必经新补记helper）；原native取消时间 **1792421144385**，严格deadline **1792421174385**（30s没延长），service generation74622679...，gross_qty.0044/open_notional10.56924<=150。rearm_step **submit_native_stop** 时间 **1792421146846**（取消后2.461s），停在await executor.submit_intent；seq416 **PROTECTION_SERVICE_TIMEOUT**。新重挂intent **2caf13a7-2f11-4734-8ff3-80b6530b5814** 创建2026-10-19T14:45:46.721787Z，最终EXPIRED_EPOCH且 **client_id=NULL**，表明尚未走到client-id绑定/发单路径；不能当UNKNOWN重新下单。本机详细证据 **grid-ordinary-linux-v5-rearm-failure-detail.json /grid-ordinary-linux-v5-submit-stage-inspection.json /grid-ordinary-linux-v5-failure-inspection.json**。
下一步优先离线复现**保护重挂在REST容量等待或账户锁前被普通启动请求阻塞**，不要再盲开v6。已读源码：executor.submit_intent先_submission_reads再_lock，保护whole_position用critical read_sequence160，其他普通intent用默认critical350；readbudget上限900/public ceiling550/reserved350，普通开仓也能消耗critical预留。当前client_idNULL无法区分容量等待/锁等待，**额度饥饿是待验证假设，未唯一确认**。现有 tests/test_grid_executor_capacity_lock.py 只测试已有spent700时普通350等待、保护160可过，没有覆盖spent500时普通350先占预留导致保护160饥饿的情形。可以先补真实有界离线复现，再考虑把非紧急普通订单准备放到public容量、保留原生stop/紧急平仓/取消critical；必须不提高IP额度、不延长30s、不放松delegation/Native/全部epoch门槛，经过实际新回归和源码版本审阅后再决定新的资金验证。不要未经证据就声称已修复预算问题。
当前已部署public包仍 **f159de80c0bced604c0fd8868df833783edc46b1c5c7d325d0adde07367d5e30** /319files /5处runtime delta，Linux完整1542pass/0skip/XML SHA6ec1ccf0f31a10f4a161abb375c09aa95cbbf4f41dddfc9bbaa08211701e4958；仅service_coverage.py+20newtests相对旧streamdiagnostics改变，零仓补记路径不赋予send/permit。实际install/version/deployment/postinstall验证均已完成，不再重复跑同版完整测试或安装。修正只证明离线同类时序通过，v5没有24h/持仓Native-running证明，不能把新源全测当实盘认证。
本机已复制9份v5review/正式审计/cutover/install/STATE/finalizer/Native/containment实际proof。原 **grid-ordinary-linux-v5-cutover-proof.json finishedtrue/failed_stagenative-running**：fresh-formal-review0/原one-shot-installer0/Native-running1（grid已CLOSED），不是新的Native验收通过。旧v4/v3失败窗绝不复用，linux heartbeat和active goal保留，实盘24h及终局资格仍未完成。

# 最新覆盖：修正已真实Linux1542全过并部署/版本审阅成功；唯一v5资金cutover在进行，严禁重放（UTC2026-10-19 14:41附近）

**先查原v5资金身份与账本，不要再启动任何安装/审计/新campaign。** 已在当前授权下审阅并启动唯一 **entropy-grid-ordinary-linux-v5-cutover.service PID165220**，脚本data/run/grid-ordinary-linux-v5-cutover.py，proof **grid-ordinary-linux-v5-cutover-proof.json**；顺序fresh正式审阅→原installer→Native running，非循环。RID **ordinary-observe-linux-20261019-v5**，前轮v4已终局flat不重用。最近实际fresh三Native/unchangedformal bundle/preflight全0；**grid-service-linux-v5-review-runner-proof.json finished=true**。原 **grid-ordinary-linux-v5-install-proof.json** 最近finished=false，steps到reviewed_ordinary_policy_and_grid1_runtime_installed；新runtime **165355**/watchdog **165357** 正等待实际healthy，尚未看到grid_id/资金start，不能说已RUNNING或已观察。原installer就绪后会自动派发v5controller/finalizer；新25h独立timer由controller在start前装，观察/cleanup都用原RID。查询原STATE/SQL/units才继续，不重放未知请求。
前述公开修正已完成 **唯一原Linux完整unit terminal0 /1542pass/0skip**，XML **grid-service-position-history-linux-complete.xml** SHA **6ec1ccf0f31a10f4a161abb375c09aa95cbbf4f41dddfc9bbaa08211701e4958**。唯一GRID0部署链entropy-grid-service-position-history-deploy.service亦terminal0，**grid-service-position-history-deployment-runner-proof.json finished=true**，7gate/systemdanalyze/sourceinstall/actualversion全0。新 **grid-service-position-history-install-proof.json installed/finished=true /legacy_unchanged=true**，5处明确runtime变化/319manifest/新66source/原v35baseline/1542真实XML/Native11DEXflat+backup均绑定。新 **grid-service-position-history-version-proof.json qualified=false**，实际31版本期限+15controller+6HTTP全0，真实看护fresh同instance。随后真实调用v5preflight.validate_execution_review(root,actualversion) 返回None、全66当前sourceSHA一致，核查proof **grid-service-position-history-postinstall-check.json**。不是只假数据检查，不称24h合格。
新 **grid-linux-service-position-history-review-v1.json** 已实际写出（只允许一轮40capital/150gross/30service/25h独立期限的v5），绑定当前10私有SHA、新version、实际1542LinuxXML及原completedprobeSHA。明确原v3stream根因未知/v4原比较值未保存故非唯一根因证明、断线可再失败；不放松正式资格谓词。没有先前空账户诊断充当持仓证明、没有旧窗拼接。需要等本轮actual Native running确认后才说启动成功，再真实连续86400秒和终局审阅。
**旧套利当前PID为162631**，已在这次源安装之前的 **UTC14:37:07（CST22:37:07）** 重新启动，NRestarts0/Restarton-failure；本次源install开始UTC14:38:49/结束14:38:54，安装前后legacy identity严格一致。原因未知，不归因于本次安装；不要继续沿用旧3343163。read-only证据grid-legacy-current-start-check.json，原安装脚本仅操作grid三个units。后续v5cutover仍以实际162631作before/after一致性。不得同步/改动/重启旧套利。
本机已复制实际LinuxXML、sourceinstall/version/deployment四proof及postinstallcheck；新v5review/fresh审阅/candidate/资金install/STATE/Native running/最终proof需待原进程产出后再复制；本机 **grid-ordinary-linux-v5-latest-check.json** 最近实际状态。source archive仍f159de80...；不再修改或重复测试已部署公共源，除非新证据要求。阶段未完成，保留linux自动跟进。

# 最新：零仓取消/末笔成交延迟已离线复现并修正；新包Linux完整测试和GRID0部署链在原unit运行，先查原身份（2026-10-19）

**当前没有新资金campaign。** 原v4保持CLOSED/终局Native11DEXflat+backup，GRID_REAL_DISPATCH=0/grants=[]，旧套利3343163未变；本轮最初实查runtime4043541/watchdog4043543，后续若新GRID0安装完成PID会改变，必须读实际proof。不要重启本机、原测试、原部署链或旧v4。
新增public补丁仅 **entropy_arb/grid/service_coverage.py** 和 **tests/test_grid_service_position_history.py**。两个真实离线重现（有/无queuedstreamfill）在旧代码均按预期失败，日志grid-service-position-history-baseline.log。修复仅在**已证Native reduceOnlyCanceled +独立Native仓位0但本地非0**时，从durable coverage/history-start cursor读取截至原Native快照时间的完整历史，先验证全部fills同account/market且属于本grid，再走原actor.apply_fill事务补记/去重；重新独立Native仓位必须与账本精确一致。新增路径无send、无permit、不修改coverage游标，最多5秒且受原取消时间+max_seconds剩余时间约束，epoch/config/stream代际/暂停/期限变化均fail closed；不完整/截断/超2000条/foreign/unowned/错误side/二次快照不一致均拒绝。原150/30/止损确认/24h资格谓词未改。不能称线上根因唯一确认（原比较值未留存），但已复现同类时序并覆盖。
新增20regressioncases；相关focused99passed是补最后oversized参数前的99，最终20已纳入真实完整套件。隔离Mac完整 **1540passed/2skip/113warnings/91.67s**，XMLgrid-service-position-history-local-complete.xml SHA **fea4a65bb210a3bdc974d8619e29ea47e2d991ed91d466125e83708e75fbffd6**。新包从已部署旧318包解压，仅替换上述2文件为319包，**未混入本机其他脏套利改动**：data/grid-code-checkpoints/grid-code-20261019-service-position-history-v1.tar.gz SHA **f159de80c0bced604c0fd8868df833783edc46b1c5c7d325d0adde07367d5e30**；新manifest grid-service-position-history-source-manifest.json。本机/远端stage均data/run/service-position-history-v1-stage；远端source-service-position-history-v1.tar.gz。
**先查唯一原完整Linuxunit entropy-grid-service-position-history-linux-complete.service PID158042**（RuntimeMaxSec1500、cwd隔离stage、真实应1542pass/0skip）。最近stillactive；无完整XML时不能称Linux通过，不另跑测试。Macexec98337已terminal0；scp/dispatch55272terminal0。实际LinuxXML路径grid-service-position-history-linux-complete.xml。
**唯一原有期限GRID0部署链 entropy-grid-service-position-history-deploy.service PID160333** 正在等上面原suite终局，proof **grid-service-position-history-deployment-runner-proof.json** 最近finishedfalse/commands[]。脚本grid-service-position-history-deploy-after-tests.py，RuntimeMaxSec2100；只原suiteinactive/success/0/NRestarts0后依次运行7diagnosisgate、systemdanalyzeverify、**grid-install-service-position-history-v1.py**（新installproof/原11DEXflat+SQL/backup/rollback/只grid服务）、**grid-service-position-history-version-review.py**（真实新66source+5处runtime delta/1542XML+31/15/6私有检查/实际guardian/GRID0）。没有普通实盘controller或新policy安装；不要把部署链dispatch视为已安装。需要逐项检查原proof和unit，复制真实LinuxXML/新install/version/部署proof本机。
准备了14份全新 **v5** 私有脚本（controller RIDordinary-observe-linux-20261019-v5、原25h独立timer/共享oncecleanup/24h双时钟+fail-fast；installer/finalizer/Native/preflight/runtime/reviewbundle/reviewrunner/31versiondeadline/15controller/6HTTP/7networkgate），本机及远端data/run。所有克隆只v4→v5，不改v35/fault-linux-v1历史。新preflight版本审查严格要求原v35baseline66源码中**5处**变化（原4+service_coverage.py）、新实际installproof/1542Linux全过XML/源码SHA，不复用旧1522声明。实际本机31+15+6+7均0；初次直接python导入因没PYTHONPATH失败，设置PYTHONPATH为项目根后真实全过，无源码门槛放松。
v5资金installer改要求新 **grid-linux-service-position-history-review-v1.json**，该文件**尚不存在**；没有v5 STATE/candidate/普通controller/finalizer/deadline或资金unit。下一步原新source部署及真实version审阅成功后，检查新代码/看护/旧套利，审阅本次修正与剩余断线风险，才准备新review+新freshNative3项+unchangedformal audit/preflight；如决定新小额实盘，避免过期Native（5min），使用一次性顺序fresh审阅后立即installer/Native的驱动链，保留历史快照，不重复资金请求。新的24h仍要实际连续86400秒及终局独立Native+backup，不拼接旧失败窗。

# 最新：普通v4已失败并真实终局清理，GRID0/grants空；旧套利未变，先处理零仓止损取消/末笔成交入账时序（2026-10-19）

**不要重启v4或发送新的start/cleanup。** gid **g-51d14011829f4259** / RID ordinary-observe-linux-20261018-v4 当前CLOSED/STOPPED/e3；start909fb022-3b08-4e80-a97b-a430e8aa2e97实际succeeded。原controller已finished；cleanup字段unknown只是当时HTTP结果，实际由保护退出 **protection-close:g-51d14011829f4259:2:1 / efcda164-1848-4aef-8915-8bfa769c50a0 succeeded** 完成，无需重复POST。原独立finalizer native-audit0+backup0/finished；Native11DEX全flat，账本净+.085255USDC/qty0；**没有24h通过**，soak最长1892.621796秒、DURABLE_RISK_OR_PROTECTION_EVENT1/GRID_EPOCH_CHANGED1等。
原资金入口已由唯一 **entropy-grid-ordinary-linux-v4-contain.service terminal0** 恢复关闭：**grid-ordinary-linux-v4-failure-containment.json finished=true**，再次fresh Native11DEXflat+backup0+SQL全CLOSED/无queued running/无未决intents后，停用仅v4deadline timer（保留contract）、policygrants=[]/GRID_REAL_DISPATCH=0，只重启独立网格。当前runtime **4043541** /watchdog **4043543**，旧套利 **3343163** 未变。所有v4金融unit均terminal，不复用旧失败窗。
**这次不是已知stream事件**：seq405 protection_gap_exit_requested / **PROTECTION_SERVICE_POSITION_UNVERIFIED**，发生在seq404与406之间。Native证明所有仓位被最后一笔自有网格卖单在 **1792360402504** 平完，原positionTpsl同毫秒 **reduceOnlyCanceled**；最后该笔成交直到seq406才入账（seq405已要求退出）。完整Native匹配6fills，initialIOC一次+.0135，后买+.0045再四笔卖-.0045。支持“零仓取消先于末笔fill入账”的时序问题，**未保存当时比较的Native值/队列状态，不能声称唯一根因完全确认**。本机 **grid-ordinary-linux-v4-failure-inspection.json** 和实际 **grid-ordinary-live-linux-v4-proof.json** 保留证据。下一步应先离线复现并安全处理该时序，不盲目新资金campaign、不放松position精确一致/150/30/24h谓词。
此前唯一4h只读probe已正常终止 **14404.6826s /固定deadline1792343066707 /unitinactive成功0**；记录一次CLOSE1000在16:00:22.909UTC，1.029秒后connection2opened。另最终stream_exit1006在原deadline后4.682秒、stop/context teardown阶段、无第二个CLOSE frame，不能算独立新对端CLOSE证据。终局本机 **grid-linux-readonly-stream-probe-terminal-evidence.json**；实际probe最终SHA **37e06b22193a41985eec3ff5e30b36ded16523987e7a12d444b0866f9c51de08**，events SHA8984c7be65157c909b2f37960abf14d410b110bc4fbee1949e66c2954bb0cc3b。原probe不重跑、不作持仓/24h证明；v3根因仍未知。
诊断review已审阅并写 **grid-linux-stream-diagnosis-review-v1.json**，只允许一轮有限新普通观察/明确root未知及可复发/不声称已修复/不作qualified。v4金融准备先实际readonly审计3项+原formal bundle/preflight通过；因快照过期，又由唯一 **grid-ordinary-linux-v4-refresh-cutover.py /entropy-grid-ordinary-linux-v4-cutover.service** 顺序fresh3Native、原bundle/preflight、原installer、Native running，均0；先前只读版本输出存 **grid-v4-readonly-review-before-cutover-refresh** 保留SHA。真实v4 RUNNING **1792358510285**，Native正持仓/止损OPEN/挂单/gross<=150实核通过后才观察；失败原controller按新fail-fast退出、未拼接窗口。
本轮本机已复制9份实际v4安装/refresh/运行/终局/Native/containment/正式审阅proof，正式public代码仍前述安全流诊断包 **83eb436e...**/Linux1522；没有新代码部署。新本机私有工具仅readonly刷新+cutover及containment，未改正式release_audit或资格门槛。linux heartbeat保留，阶段未完成。

# 最新：原只读probe已捕获CLOSE1000并自动重连，仍按原17:04:26.707 UTC截止（UTC2026-10-18 16:02附近）

原unit entropy-grid-readonly-stream-probe-v1.service **PID3795351未重启**。在 **1792339222909 /UTC16:00:22.909** 收到terminal_frame/CLOSE/close_code1000；原连接open_seconds10556.0065（约2h56m），无AdapterError/异常类型，随后 **1792339223938** connection2 opened（关闭到新opened约1.029s）。最新actual stream_connected=true/generation2；累计subscriptionResponse34（两轮各17）、sent_ping4693/channel_pong4693，无已记录server_error。只保存脱敏事件/计数，不读取或输出peer reason/raw账户消息。
独立诊断能在无签名/无资金动作时出现连接关闭；这只是**新协议层证据**，不能证明v3失败的唯一根因、不能证明持仓恢复或24h合格，也不能称问题已修复。原probe仍finished=false，固定deadline1792343066707不延长，继续同一进程观察至原期限，再基于实际终局审阅；不提前生成network-ready review/新资金campaign。
本机证据 **data/run/grid-linux-readonly-stream-probe-first-close-evidence.json** 保存原状态+完整脱敏events、快照SHA499254f4fdf9455dcf369a9068fa2e4f433bc7e38ae691b95e300fa02cc75817/events SHAe4a0c06134f6335c8f99eafe661e332d996ebf89da93487238f5229608278881。mutable probe以后会继续变化，不把此SHA当最终review绑定。
实际核对独立cwd/root/code/entropy-grid、GRID_REAL_DISPATCH=0/grants=[]/active_grids0/pending_operations0；runtime3823806/watchdog3823807 heartbeat<1s；旧套利3343163 active且未变。前述日志新包/1522Linux验证/版本review保持有效，无新代码或服务重启。普通24h仍未通过。

# 当前：安全流诊断新包已部署并完成真实版本审阅，GRID0/grants空；原独立probe保持运行（UTC2026-10-18 14:23附近）

**后续不要再跑安装或重启任何probe。** 唯一日志安装unit entropy-grid-install-stream-diagnostics-v1.service 已terminal0，**grid-stream-diagnostics-install-proof.json installed/finished=true、legacy_unchanged=true、probe_not_restarted=true、probe_deadline_not_extended=true**；fresh Native11DEX flat+online backup0/SQL无未决后，仅独立网格目录装318包 **83eb436eb8e6522411b8083c426b659ab0c54847147014ef38a7c5b5a3f8f287**，回滚 stream-diagnostics-v1-rollback。旧套利 **3343163** 未改。生产仍 **GRID_REAL_DISPATCH=0 /grants=[] /全grids CLOSED**；没有任何新金融POST。
原只读版本review **entropy-grid-stream-diagnostics-version-review.service terminal0**，已生成 **grid-stream-diagnostics-version-proof.json**，4处明确runtime变化/实际Linux完整1522passed/0skip/XML SHA **c5c2ce9c2c87a0330ef946b944a839baa14a900bca6e27e258c0b0aab382d468** /新66源码与units SHA /fresh看护核对全部通过。private_v4_checks实际31版本期限+15controller+6HTTP通过；qualified=false，只是已部署代码的版本审阅，不是普通实盘24h通过。当前runtime **3823806**，watchdog **3823807**（不要沿用旧3794506/3794507），实际heartbeats/看护同PID同instance healthy。
已实际调用新v4 preflight.validate_execution_review(root,new_version) 对 **真实安装/真实1522XML/真实66当前source** 返回None（不是只跑假数据）；另7诊断门槛模拟测试再次通过。新汇总 **grid-ordinary-linux-v4-post-diagnostics-preparation-proof.json** 绑定当前10私有文件SHA+新versionSHA+真实核心检查/31+15+6+7，ready_for_policy_install=false。它替代“当前准备情况”，但不覆盖旧v4-preparation-proof（旧hash作历史）。仍 **没有v4 STATE/正式candidate/network diagnosis review/新timer/资金执行unit**。新v4金融installer继续硬要求原probe完成+诊断审阅ready+probe文件SHA绑定，未放宽。
**当前待完成的是唯一原网络probe**：entropy-grid-readonly-stream-probe-v1.service **PID3795351**，安装前后进程InvocationID/birth/PID、脚本SHA和截止时间均相同，仍原加载的旧stream函数做同一观察；不能把新日志部署说成probe已跑了新代码4h。最近elapsed4664秒/connections1/exits0/connectedtrue，原deadline **1792343066707 /UTC17:04:26.707**。它不加载keys/no signer/no资金动作，不是持仓或24h认证证明。
下一步仅检查原probe的具体close/AdapterError原因与alive/terminal及账户/旧服务，直到实际到期或出现可处理证据；若未复现不能说根因已修复。新日志现在已在实际生产runtime内，下次工作负载可在journal中只按静态HL_ACCOUNT_STREAM_INTERRUPTED tag读错误码/类型，不展开任意raw消息。真实新普通24h仍要全新身份/正式gate/期限和终局Native+backup，v3失败窗不拼接。保持linux heartbeat与active goal，阶段仍未完成。
本轮新安装/版本/post-diagnostics准备/最新probe四文件已复制本机data/run；新版本审阅与安装code/source可按hash复核。此前“日志installer必须等probe结束”的临时规则已被明确替换为“GRID0纯日志安装可进行，但必须保证独立probe身份/窗口不变”；这是部署安排优化，正式金融资格门槛完全保留，见下一段说明。

# 最新部署进展：仅日志新包已实际安装，原probe未重启且仍连通；GRID0/grants空不变，实际新版本审阅进行中（UTC2026-10-18 14:20附近）

**当前不是实盘启动。** 新纯脱敏日志318包已由唯一 **entropy-grid-install-stream-diagnostics-v1.service** 真正安装，unit inactive/dead/ExecMainStatus0；**grid-stream-diagnostics-install-proof.json installed=true/finished=true**，Linux完整1522passed/0skip及SHA c5c2ce9c2c87a0330ef946b944a839baa14a900bca6e27e258c0b0aab382d468绑定，fresh Native11DEX无仓无单+在线backup/SQL全CLOSED无未决均通过。只更新独立/root/code/entropy-grid，旧套利3343163未动；rollback stream-diagnostics-v1-rollback保留。普通policy仍grants=[]/GRID_REAL_DISPATCH=0，没任何金融POST/新grid。
明确调整了**我此前临时添加的日志安装等待限制**，理由是原只读probe是独立进程，生产在GRID0下安装仅日志变更并不会重启它，等待到4h后才装日志无必要。该限制不是用户要求或正式资格谓词。替换为安装前后严格核对原probe的PID/InvocationID/ExecMainStartTimestampMonotonic、private脚本SHA和固定deadline均一致，保存probe_before/probe_after/安装前66source hash到proof；**probe_not_restarted=true/probe_deadline_not_extended=true**。没有改正式release_audit/没有放松24h/没有改v4金融installer的 **probe.finished+diagnosis-reviewSHA** 硬门槛。
**原probe仍PID3795351**，最新elapsed4584秒/connections1/exits0/connectedtrue，固定UTC17:04:26.707期限不变。安装只重启独立gridruntime/watchdog/web三个服务，不包括probe或旧套利。probe已加载旧stream函数，保留这个原网络观察作为其原版本诊断，不把新日志部署说成它已经验证的新代码连续4h。
**待核查原版本审阅**：唯一 **entropy-grid-stream-diagnostics-version-review.service** 已提交，脚本data/run/grid-stream-diagnostics-version-review.py；需要先查原unit和输出 **grid-stream-diagnostics-version-proof.json** 是否真实写出，不重复启动。它绑定当前4处runtime变更/实际1522XMLSHA/新unit source hashes、GRID0实际进程和fresh看护，并真实跑新v4版本期限31、controller15、HTTP6后出只读版本证明（qualifiedfalse，不能代替实盘）。随后应核查新的运行PID/heartbeat/旧套利和原probe，复制新安装/版本proof到本机；不能沿用旧trigger版本说新代码合格。
前轮15分钟监控改为本机 **data/run/grid-stream-probe-watch-local-v1.py** 短SSH逐次实查（每次timeout20s），原exec session79323实际terminal0，最后confirmed probe3795351 active/exits0/elapsed4424秒、全CLOSED无pending/grants空。它只是只读monitor，不是另开probe或资金任务；可复用这个本机脚本而不建立长持有远端stdin watcher。数据落本机grid-linux-readonly-stream-probe-last-monitor.json，不作4h/24h资格。
下一步先完成原新版本审阅的真实核对；再等待原probe实际结果/诊断码。仍不生成network-ready review/候选或启动v4实盘，根因未明且原4h未结束。旧V3失败及2h43m窗口不可重用，所有正式Native/故障/小残仓门槛保留，真实新普通24h及终局仍未通过。

# 最新覆盖：安全流诊断补丁两端完整测试通过，仍未部署；原4h只读probe继续且暂未复现（UTC2026-10-18 13:34附近）

本轮补充实查（UTC2026-10-18 14:01附近）：原probe3795351 active、connectedtrue/connections1/exits0/elapsed3413秒，GRID0/grants空、全CLOSED/无pending。最后监控本机data/run/grid-linux-readonly-stream-probe-last-monitor.json。15分钟只读SSH等待会话7111的远端监控已不存在，但本机SSH无终局输出而挂住；独立实查原probe健康后，仅结束本机那个精确匹配只读命令的SSH PID55223，没重启原probe/runtime/资金请求。后续避免长持有SSH stdin watcher，优先短超时一次性实查并在本机等；会话退出255只是本机读取结束，不是probe失败。

**当前等待的是原probe，不是测试/安装。** 原 **entropy-grid-readonly-stream-probe-v1.service PID3795351** 已再次实查active，elapsed1801秒/connections1/stream_exits0/connectedtrue，原固定UTC17:04:26.707期限不变。账户仍无实盘活动/GRID0/grants空，旧套利未动。没有诊断完成/根因未明，不得创建v4 STATE或开新资金单。
新脱敏日志补丁已实际全量验证：本机 **1520passed/2skip/113warnings88.50s**，XML SHA9f47e5d8ac9a95e97bab0ac0025ca5ebe38a94a14a3c200168d7ecc1ec666662；Linux唯一完整 **entropy-grid-stream-diagnostics-linux-complete.service** 已 **inactive/dead/MainPID0/ExecMainStatus0**，**1522passed/0skip/113warnings530.69s**。报告 **grid-stream-diagnostics-linux-complete.xml** SHA **c5c2ce9c2c87a0330ef946b944a839baa14a900bca6e27e258c0b0aab382d468**已复制本机。117相关和新增10场景（旧9fail/1normalpass）证据均保留。新汇总 **data/run/grid-stream-diagnostics-verification-proof.json** 记录两端真实结果、source package与未部署状态；不是断线根因修复/4h或24h资格。
补丁包318 SHA **83eb436eb8e6522411b8083c426b659ab0c54847147014ef38a7c5b5a3f8f287**/stage stream-diagnostics-v1-stage，生产当前仍旧trigger版本，尚没有grid-stream-diagnostics-install-proof.json或version-proof.json。已备好的 **grid-install-stream-diagnostics-v1.py** 硬要求probe finishedtrue才允许后续flat Native+backup安装，不要现在执行/移除此限制。它读取当前trigger版本66hash、核对旧v35 baseline中仅4处变化（adapters/hyperliquid、hl_risk、live_risk、protection），要求实际1522完整XML，回滚保留，不碰旧套利。
本轮也已明确更新未启动v4预检/版本测试到 **新4处变化/1522/新grid-stream-diagnostics安装与版本proof**，原准备版preflight/test副本保存 ...-before-stream-diagnostics.py，旧31/15/6/7 preparation-proof文件不改作历史。新版本+期限31 **本机/实际Linux均通过**，Linux证明 **grid-ordinary-linux-v4-diagnostics-gates-proof.json** +31XML已复制本机。首次四文件fake valid失败因fixture changed_runtime_files未按字典序（adapter应排最先），只修fixture排序后31通过，正式门槛未放松。
新只读版本writer **data/run/grid-stream-diagnostics-version-review.py** 已准备两端但未执行，它需要实际新包安装proof才可生成grid-stream-diagnostics-version-proof.json/实际看护与4核心source/1522SHA及v4私有检查。现在线上仍3旧runtime变化，故新v4 preflight理应不能放行（新proof不存在）；不拿旧1512证明新日志代码。原 **grid-ordinary-linux-v4-preparation-proof.json** 的10私有hash绑定旧preflight，作为历史保留；后续新实际准备审核另建汇总，不覆盖它。
下一步检查原probe进程+具体server close/AdapterError诊断；它结束后若未复现，保留“根因未知/未复现”，不要称已修复。可以在全flat和必要备份后部署已完整测试的纯日志新包，以便之后真实工作负载可查原因；这仍不自动授权网络审阅ready或绕过新普通24h。若probe揭示真正需修连接/解析，则按证据做针对性修复与必要版本验证，再全新有限普通验收。user已获关键v3失败平仓通知，正常等待时保持安静。保持现有linux heartbeat/active goal，阶段及整个产品均未完成。

# 最新进行中：官方确认需要处理无预告服务端断线；补安全生产诊断，尚未部署；两端完整测试原进程进行中（UTC2026-10-18 13:25附近）

先查原 **只读probe PID3795351**，最近elapsed1301秒/connections1/stream_exits0/connectedtrue，尚未到原4h期限；不得重启/延期/当实盘资格。线上当前仍 **GRID0/grants空/全CLOSED/无pending**，核心仍旧trigger-visibility的3处变化/1512测试包，旧套利3343163没动。上一goal turn完成v4整套准备，此turn是新的实质诊断缺口修复+测试，尚未替换线上源码。
本轮查阅官方主文档 https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket ，明确服务器可无预告周期断开、客户端应可靠重连；这 **不是本次根因证明**，也不能拿来放宽普通24h门槛。心跳官方页面说明60秒无服务端消息闭连及application ping/pong；代码已有2秒应用ping，不代表现场一定都成功发送。官方限额页面每IP10WS/30新连接每分钟/1000订阅/10unique用户/2000外发消息每分钟/100inflight post；当前probe只观察自己1连接和17ACK，不能推断全IP/旧套利总数或排除限额。官方页面没给2.7h/24h固定寿命，不应凭时间猜。整理 **data/run/grid-hyperliquid-stream-official-review-20261018.md**（已准备上传）。
明确缺口：core HyperliquidGridVenue.run_stream吞异常且收到CLOSE不记录原因。已在本机 **entropy_arb/grid/adapters/hyperliquid.py** 补 **仅脱敏warning**：日志tag HL_ACCOUNT_STREAM_INTERRUPTED，记录frame类型、有限整数close_code、generation；异常仅可信HL_[A-Z0-9_]枚举或异常类型，不含异常body/对端reason/账户地址/订阅消息/签名。不改重连、gap、generation或风险交易逻辑。新tests/test_grid_stream_recovery.py **10场景**，旧代码实际9fail/normal-stop1pass（baseline日志grid-stream-diagnostics-baseline.log）；修复后含相关4文件 **117passed**。覆盖服务端error/JSON异常/transport异常/不可信AdapterError/订阅超时/心跳超时/CLOSE1001/CLOSED1006/ERROR/正常停止，断言恢复冻结保持、无签名/资金动作、私有哨兵与OWNER均不出日志。这不是已修好原断线根因，只保证未来实盘能查原因。
新318包 **data/grid-code-checkpoints/grid-code-20261018-stream-diagnostics-v1.tar.gz** SHA **83eb436eb8e6522411b8083c426b659ab0c54847147014ef38a7c5b5a3f8f287**，相对已部署trigger包只有上述adapter+stream_recovery测试2文件变化。新manifest grid-stream-diagnostics-source-manifest.json。本机隔离stage data/run/stream-diagnostics-v1-local-suite，完整 **exec session7492**，XML/log **grid-stream-diagnostics-local-complete.***；实际1520passed/2skip/113warnings88.50秒，XML SHA9f47e5d8ac9a95e97bab0ac0025ca5ebe38a94a14a3c200168d7ecc1ec666662，session7492已terminal0。服务器archive source-stream-diagnostics-v1.tar.gz及隔离stage stream-diagnostics-v1-stage已校验318，唯一实际完整单元 **entropy-grid-stream-diagnostics-linux-complete.service PID3803017**，runner grid-stream-diagnostics-linux-tests.py，XML/log **grid-stream-diagnostics-linux-complete.***，预期1522/0skip，尚未确认终态。先查这些原unit/XML，绝不因观察超时重跑，不把预期称实际。
已准备但 **未执行** installer **data/run/grid-install-stream-diagnostics-v1.py**，要求 **原probe finished=true** 才能开始；随后严格完整Linux1522、当前trigger-version66哈希、旧v35历史baseline中明确4处core变化（hl_risk/live_risk/protection/adapters/hyperliquid）、fresh Native11DEX flat+backup/SQL无未决，才可部署独立目录并保留rollback。将生成grid-stream-diagnostics-install-proof.json/rollback stream-diagnostics-v1-rollback。当前没有这个新安装proof，不应声称已部署；原probe加载旧代码保持不动。
如果后续确实安装此仅日志新包，还需新 **grid-stream-diagnostics-version-proof.json** 绑定实际1522XMLSHA与4处source变化，不能覆盖旧trigger/v35版本。已准备的v4 preflight/版本tests目前仍期待3处/1512/trigger证明，因此 **必须明确更新到新4处/1522/新proof并针对性检查**，不能绕门槛；旧v4-preparation-proof绑定旧私有文件hash，应作为历史保留，新准备汇总另建文件。若probe实际根因需要其他核心修复，则基于原因新版本包含日志补丁做必要测试/审阅，不盲继续改。
等待完整测试/原probe期间，不要启动v4 installer、生成network diagnosis review或fresh会过期的候选，不额外连接/重复持仓试验，不重启本机writer/旧套利。已有linux heartbeat继续。

# 最新补充：v4整套有限验收准备完成/仍未启动；原4h只读连接诊断继续（UTC2026-10-18 13:20附近）

当前真实资金状态未变：GRID0/grants空、全库CLOSED/无pending操作，v3清理已成功、原Native终局全flat。原唯一 **entropy-grid-readonly-stream-probe-v1 PID3795351** 仍active，最近connectedtrue/generation1/connections1/stream_exits0，固定结束UTC17:04:26.707，不重启/不延期。上一goal turn是已完成v4 fail-fast修复，当前turn继续完成可执行的离线准备，并实际确认原probe live；不能当普通24h进展或完成。
已把 v4新identity的 **deadline/finalizer/preflight/正式bundle reader与review runner/installer/Native只读check/暂存runtime unit/版本期限与HTTP测试** 生成并复制独立网格data/run，保留所有旧v3文件和失败记录。替换用了v3(?!数字)避免误改历史loop v35；Linux真实持仓故障proof仍原linux-v1，核心66文件、旧1512完整包及正式audit_release不变。新v4 installer新增硬门槛：必须存在 **grid-linux-stream-diagnosis-review-v1.json**，review_complete和ready_for_new_ordinary_trial都true，并且 **原probe finished=true且文件SHA绑定review** 才能走正式审核/安装。这份review当前不存在，不能直接启动installer或伪造review跳过未完成诊断。
**两端离线通过**：版本+期限31项、本机/实际Linux controller15场景、HTTP6场景、实际installer只读网络门槛7场景（从原installer AST只执行5条读证明/断言，未执行installer），以及Linux staged unit的systemd-analyze verify。7项包含缺失/未审阅/明确拒绝/诊断未完成/哈希篡改/探针缺失均拒绝，有效绑定才允许；这些是临时文件模拟，不是任何真实4h/24h稳定性或资金资格。
服务器 **grid-ordinary-linux-v4-preparation-proof.json** 实际写入，绑定10个私有关键文件hash、31/15/6/7真实测试结果、production_core_unchanged/financial_actions0/prepared_onlytrue；对应31XML **grid-ordinary-linux-v4-preparation.xml**已复制本机。controller15两端proof和前轮failfast汇总继续保留。当前 **没有v4 STATE/没有新grant候选/没有deadline合同或timer安装/没有v4交易unit启动**；仅data/run暂存文件。正式审核及freshNative/真实资金必须等诊断实际结果审阅后再进行，不提前创建会过期的候选。
下一步只查原probe的live handle与诊断码/close原因及账户状态；有实际结果再决定修复和新普通验收。若4h无断线只能称未复现，不能称根因已修复。若需要改核心连接逻辑，必须新针对性回归/必要完整测试/更新明确版本审阅，不能沿用旧1512证明新代码。旧套利3343163和本机writer限制不变。

# 最新补充：原4h只读诊断继续；下一版私有验收控制器已补及时收尾，15场景两端通过，未重开实盘（UTC2026-10-18 13:17附近）

生产实际仍下文的 **GRID0/grants空/全grids CLOSED/原v3cleanup succeeded**，runtime3794506/watchdog3794507健康同instance/旧套利3343163未动；原 **entropy-grid-readonly-stream-probe-v1 PID3795351** 正常，最近connections1/stream_exits0/connectedtrue，按原UTC17:04:26.707固定期限运行。没有可复现断线码前不盲开新实盘；诊断结果尚未完成，不得提前算稳定4h或24h资格。
等待诊断时完成明确已知私有controller缺口：新增 **data/run/grid-ordinary-live-linux-v4.py**，原v3脚本/STATE/失败窗全部保留。新版本记录开始epoch；优先尊重用户新pause/stop_keep/配置授权接管，再遇真实epoch变化或可信observer报告issues立即结束本轮并进入原cleanup_once；报告须绑定本轮GID/grant/scope ordinary，外来报告fail closed，仍不重POST/不重建身份，不降低真实86400秒/双时钟要求。仅该私有controller和对应offline-check复制到服务器，**没有安装candidate/没有创建v4 STATE/没有启动v4资金或observer/没有修改已部署66核心/没有重启原probe**。v4依赖的完整deadline/preflight/正式候选等尚未生成，不能把此控制器准备当v4可直接启动。
**15个离线场景本机和实际Linux均通过**：保留原11个grant/预览/期限失败/真假24h/人工pause/自动风控/stop_keep/配置授权变化与existing STATE禁止重放；新增流已恢复但epoch变化/活observer首次issues/运行后新issues/外来GID报告4项，断言只一次原cleanup、活observer终止且不继续等待。人工接管场景同时携带issues仍必须交还用户。所有RPC/进程/时钟均为double，无真实资金/24h。proof本机 grid-ordinary-live-linux-v4-offline-proof.json 和 ...-offline-proof-linux.json，汇总 **grid-ordinary-linux-v4-failfast-preparation-proof.json**（controller SHA及两端proof SHA）。Linux原始proof在远端...-offline-proof.json。首次生成把旧v35模板名误替成v45导致纯离线FileNotFound，已明确修回真实v35，再实际15场景通过；不得更改历史35/伪造证明。
下一步仍优先检查原probe真实code/close原因，依据结果再修复/验证，随后完整新身份重新普通24h；现在不需要重跑1512完整生产包（66核心未改）或新增连接试探任务。已有linux heartbeat保持，状态无变且无可执行事项时安静。

# 最新覆盖：普通v3因真实账户流断线恢复打断连续窗，已CLOSED/Native全flat；GRID0/grants空；唯一4h只读连接诊断运行中（UTC2026-10-18 13:04起）

**不要重开v3/不要沿用其观察或身份/不要重启现有只读诊断。** g-6669f5c7ac7c4c30当前实际CLOSED/STOPPED/e4，原cleanup RID ordinary-observe-linux-20261018-v3-cleanup /operation **baef6cec-caa4-4589-81f9-2abdbe441e23** 实际SQL succeeded/remaining仓位0。原controller STATE的cleanup仍保存较早running响应，不能据此误重POST。finalizer grid-ordinary-live-linux-v3-finalization.json finished=true，Native审计exit0+backup exit0，最终proof grid-ordinary-live-linux-v3-proof.json observed_ms1792328469267/11DEX零仓零单；实际inventory空、全库grids CLOSED、无queued/running、无未决intents。普通24h **失败**，最长连续 **9788.149201277643秒（2h43m8秒）**，不能拼接后续窗口。未改普通认证门槛。
实际故障：events seq388 stream_recovery_required收到 **UTC12:53:38.355091**、epoch1→RECOVERING/e2；389/390 recovery_checked ok=true/无drift/无gaps；seq392 stream_recovery_attempt在 **12:58:11.736251** attempt2/oktrue/retryfalse，自动重建恢复RUNNING/e3，约273.381秒。运行器和看护原PID3728423/3728425未因这个断线而退出；这是账户数据WebSocket流中断触发恢复，不要称为用户暂停/亏损风控或现场已证明某个网络根因。soak durable issues GRID_EPOCH_CHANGED:2 /GRID_NOT_RUNNING:54；终局可以增加清理期间issues，首个中断证据已保存。根因目前 **未知**：core run_stream except把AdapterError/WS CLOSE原因吞掉，原runtime日志故障窗口0行，不能凭~2.7h猜成服务器固定断开/心跳/网络。
发现私有普通controller仅在CLOSED/STOPPING/NEEDS_ATTENTION或observer退出时收尾，**不会在活observer首次issues/epoch变化后立即终止失败验收**，故其在自动恢复后仍会继续跑至原期限，虽不会伪造24h通过，却无益占用资金。为本次安全收尾，先保存STATE/soak不可覆写副本，再用pidfd严格核对原observer3730459/GID/scope并只SIGTERM该只读observer，使原controller走其既有cleanup_once和原RID；**没有额外金融POST/没有停止交易runtime/没有换清理身份**。记录 grid-ordinary-linux-v3-stream-failure-proof.json。未来新controller应补首次可信issues/epoch变化fail-fast及离线回归，保留scope/用户接管/同RID一次清理；不可改正在结束的v3脚本/伪造旧STATE。
已完成收敛 **grid-ordinary-linux-v3-failure-containment.json finished=true**：fresh Native<5min+原终局两命令0+SQL全flat后，禁用仅v3对应25h timer（合同保留），备份已装grant到grid-live-release-policy-before-ordinary-v3-failure.json、旧unit到grid-runtime-before-ordinary-v3-failure.service；当前 **GRID_REAL_DISPATCH=0 /grid-live-release-policy.json grants=[] /无trial参数**。只重启独立网格runtime/watchdog为 **3794506 /3794507**；旧套利 **3343163** 不变。当前源码仍原新trigger-visibility包（66runtime里3处明确改，Linux1512passed），不因这次断线而盲改核心交易/不重复全量测试。

**当前可执行诊断**：唯一unit **entropy-grid-readonly-stream-probe-v1.service PID3795351**，脚本 **data/run/grid-linux-readonly-stream-probe-v1.py**，状态 **grid-linux-readonly-stream-probe-v1.json**，脱敏事件JSONL **grid-linux-readonly-stream-probe-v1-events.jsonl**。已实际启动，首查connections1/stream_exits0/stream_connected=true/generation1，17个真实订阅ACK及spot/allDex/openOrders/funding/pong数据都已收到。这是 **无key/无signer/write_enabled=false的独立只读诊断**，不是持仓故障或24h资格证明，不要将其成功代替实盘。使用实际安装的HyperliquidAccountVenue.run_stream和相同stream_ping_interval2/heartbeat15，UnifiedObservation所有11DEX订阅。只有启动时官方perpDexs一个公开HTTP读；之后probe transport.post所有路径都拒绝，WS仅subscribe白名单/ping，无order/post/action方法。包装只记录静态异常类型、HL错误码、CLOSE码和计数，不记录账户正文/密钥/签名。
诊断固定 **4h /14400秒**，started_ms **1792328666707 = UTC2026-10-18 13:04:26.707**，deadline_ms **1792343066707 = UTC17:04:26.707**，另systemd RuntimeMaxSec14460兜底；不得延期/重启来拼稳定性。脚本部署前6类离线安全检查（send allowlist/action reject/payload redaction/known error diagnostics/origin restriction/固定时长）真实通过；不称为1512额外回归。只读诊断不修改生产核心/DB/旧套利/资金。每10秒更新状态、最多100条last_events，完整脱敏事件流JSONL。
下一步先查 **原probe** 是否active/terminal、stream_exit/terminal_frame/server_error/connect_exception及具体adapter_code/close_code，和GRID0/账户仍flat/旧套利PID；若它复现相同断线，依据实际码修复或调整连接，并做针对性回归与必要版本审阅。尚不能证明生产那次根因，仅对照诊断观察。若4h无断线仍应保留“未复现”而非宣称问题修复；不能盲反复实盘开单碰运气。新普通24h必须新身份（v4或以后）、原正式审核/刷新实际Native证据、独立有期限退出，满真实86400s+终局Native/backup方可通过。现有linux heartbeat继续，不能因为v3失败/只读probe运行就停用。
本轮11份关键故障/原STATE/soak/Native终局/备份结果/containment/初次probe状态已复制本机data/run（注意探针状态是初始副本，后续先读远端）。用户已收到关键通知：连接恢复导致本轮24h失败，账户已清仓，正在无资金诊断，不重开。没有更改旧/root/code/entropy/套利3343163，无本机writer启动。

下文“普通v3仍RUNNING/北京时间明天18:10可验收”等为历史；当前以以上CLOSED/GRID0及远端为准。

# 当前唯一实际普通v3已RUNNING；新止损确认修复通过真实启动，24h从UTC2026-10-18 10:10:28.441起（本轮最终记录）

**先查这一个原请求/观察，禁止重新部署/再创建/重启任何执行器或观察。** gid **g-6669f5c7ac7c4c30**，RID **ordinary-observe-linux-20261018-v3**，RUNNING/ACTIVE/e1，原start operation **0f6bb1dd-dc56-4293-ba59-151ef6302c78** 实际SQL succeeded。原controller **entropy-grid-ordinary-live-linux-v3.service PID3728802**；原soak子进程 **3730459** 已实查/proc argv为正确GID/scope ordinary；finalizer **entropy-grid-ordinary-linux-v3-finalize.service PID3728800**。runtime **3728423** /watchdog **3728425**，实际心跳/看护instance一致且fresh<3s；旧套利 **entropy-console PID3343163**保持原进程。
实际新观察开始 **1792318228441 = UTC2026-10-18 10:10:28.441 = 北京时间18:10:28.441**，最早满24h **UTC2026-10-19 10:10:28.441 /北京时间10月19日18:10:28.441**；主观察宽限deadline +60s。仍必须双时钟连续86400秒/无issues+准确scope/grant/runtime实例，不能提前完成/不拼接v1-v2失败窗。当前初次soak issues={}、仅几十秒/分钟、completed_without_observed_issues=false；ordinary_24h_observation=true只是scope。
**真实Native运行核对已通过**：唯一只读 entropy-grid-ordinary-native-running-linux-v3.service terminal0，proof **grid-ordinary-live-linux-v3-native-running-proof.json**（本机+远端）。实际owned .0176 ETH /3cross，6个工作订单含Native full-position Stop OPEN，全部CID/OID/价格/数量/方向/reduceOnly绑定正确，gross **53.285320 USDC <=150**，仅一个initial-position intent。Native/observer/启动原请求真实绑定 proof **grid-trigger-visibility-v3-actual-start-proof.json**（两端）。新stop真实SENT 10:07:23.792999Z→UNKNOWN 24.620663/stage trigger_ack_unresolved→STILL_UNKNOWN25.070180，之后同一intent恢复OPEN才初始建仓；查询窗口在实际启动已发挥作用，未重新提交保护order。不要把这段启动成功当24h认证通过。
独立25h timer **entropy-grid-ordinary-deadline-linux-v3.timer active/waiting**，合同 **grid-ordinary-linux-deadline-v3-contract.json** 0600，固定due_ms **1792407984655**，原cleanup RID **ordinary-observe-linux-20261018-v3-cleanup**；主控制器与期限worker共用独立cleanup journal/lock，不明结果只能查原请求，不重复POST。不得延期/重武装。按原策略用户新pause/stop_keep或配置授权变更则交还控制。
本轮门控流水 **entropy-grid-trigger-visibility-v3-progress.service** finished=true/所有4steps exit0；新源码部署、版本审阅、三份真实Native终局刷新和原正式audit_release/bundle/preflight、普通安装均真正成功，proof **grid-trigger-visibility-v3-progress-proof.json**、**grid-trigger-visibility-install-proof.json**、**grid-trigger-visibility-version-proof.json**、**grid-service-linux-v3-review-runner-proof.json**、**grid-service-linux-v3-bundle-audit.json**、**grid-service-linux-v3-reviewed-candidate.json**、**grid-ordinary-linux-v3-install-proof.json**两端已复制。新Linux完整1512passed/0skip/113warnings473.42s，**grid-trigger-visibility-linux-complete.xml** SHA **b769cde75b99aa8ef4e1f64c2a1b73cf7c052c40ae4e4610ee789fb9dda6fde3**已复制。Mac1510passed/2skip，74相关+16新增回归；私有版本/期限31、控制器11、HTTP6两端通过（只是离线准备）。
部署公开包318文件 SHA **0c6a8eccea00021b865e850d2000a66a911fbec0918696eadadb087084c47cef**。原v35的66runtime中只明确改 **hl_risk.py/live_risk.py/protection.py**，新v3 preflight严格绑定这3处、真1512完整XMLSHA与安装proof，正式release predicate未改。旧v35/order-race/v1/v2历史记录未重写。回滚保留data/run/trigger-visibility-v1-rollback，当前正在真实持仓，严禁在观察中随意换代码/重启。
下一步只检查原v3真实账本/原start状态/上述controller与observer/soak issues/Native风险及期限；状态正常无可执行事项保持安静。若新风险事件按诊断kind与原金融身份核查，保留原失败窗口、不盲重POST。**新普通连续24h及终局实际Native审计+backup完成**才可停用现有linux heartbeat并汇报阶段验收结果；不标记整个多交易所产品完成。finalizer会在实际CLOSED后审计/备份，仍需独立复核其退出码、Native最终flat及真实24h。当前不得停用跟进/宣称全部上线完成。
本机网格runtime/watchdog仍不运行，只隧道；旧/root/code/entropy没有同步/重启/覆盖，与用户昨天套利修改无关。资金仍只走CLI/API；本轮所有初始/保护/普通单都经既有正式网格HTTP/API流程、既定独立账户/40总资本/150敞口/30s服务保护和独立25h期限。

下文“正在等安装/待启动/普通开单关闭”等为历史，以以上实际RUNNING和服务器为准。

# 最新进展：Linux1512完整通过，修复已部署+正式审核通过，普通v3原installer正在等真实runtime就绪（UTC2026-10-18 10:05后）

原唯一Linux完整unit terminal0：1512passed/0skip/113warnings473.42秒，XML grid-trigger-visibility-linux-complete.xml SHA b769cde75b99aa8ef4e1f64c2a1b73cf7c052c40ae4e4610ee789fb9dda6fde3。性能10万事件导致约3分钟无输出，已实际完成，未重跑测试。新代码已由flat Native11DEX+在线备份后只在独立网格部署，grid-trigger-visibility-install-proof.json installed/finished=true；新version grid-trigger-visibility-version-proof.json包含3处明确runtime变化、真1512XMLSHA、实际runtime/watchdog、31+11+6离线通过。原v35/order-race历史proof未动。
新原流水 grid-trigger-visibility-v3-progress-proof.json steps flat-deploy/version-review/formal-review全部exit0，current_stage ordinary-install、finished=false。三份新-review-v3 Native审计/原正式audit_release/bundle和Linuxpreflight全exit0，candidate已生成，普通v3 installer grid-ordinary-linux-v3-install.py已经切换GRID1正式grant，正在原180秒实际status/metrics/guardian一致性就绪窗口；proof grid-ordinary-linux-v3-install-proof.json finished=false，steps至reviewed_ordinary_policy_and_grid1_runtime_installed。先查原unit及proof/DB，不重复启动/不重POST。
已唯一启动 **entropy-grid-ordinary-native-running-linux-v3.service** 只读helper（data/run/grid-ordinary-linux-v3-native-check.py/OUTPUT grid-ordinary-live-linux-v3-native-running-proof.json），最多600秒等原v3实际RUNNING+正仓后核对；不要重启helper，更不把prepared/运行中当证明通过。刚读库active=[]，v3 STATE尚无；后续待原installer启动controller/finalizer。只读查询误用了不存在的operations.error_code，实际表应select request_id,status后按result_json查看；无资金操作。

下文Linux测试/尚未部署文字为较早状态，以上实际proof为准。

# 最新：启动保护单延迟可见修复已完成本机测试，Linux完整测试及v3门控流水进行中（UTC2026-10-18 10:02附近）

上一轮v2仍CLOSED/零成交/终局Native平仓，不可重开。当前生产起点仍GRID0/grants空；实际只读核对全grids CLOSED，runtime3720474/watchdog3720477/旧套利3343163。首个SQL误用了不存在的grid_instances表，纯只读失败后更正为真实grids核对，无交易操作。
本机protection.py新增_prepare确认辅助：同一durable intent仅查询，固定5秒monotonic窗口/.25秒paced poll，每个await用剩余deadline约束；新UNKNOWN和existing UNKNOWN/DISPATCHING/OPEN复用都覆盖，前后严格epoch/STARTING/ACTIVE，FOUND_OPEN且cumulative_known=false，未知不放行/不重签/不重POST。保留后续全量verify、neutral多侧全部确认后才取消旧backup。Adapter参数/身份校验未弱化，executor全局unknown策略未改，30秒service门槛未延长。
新增16回归：3个延迟可见首次/复用/neutral在旧代码真实全部失败；修复后通过。另12参数化never-visible/terminal/错scope/错参数/epoch变化/暂停首次与复用仍fail closed且不取消旧stop/不普通下单；1 stalled query由deadline取消。74相关测试通过。本机完整1510passed/2skip/113warnings91.55s，grid-trigger-visibility-local-complete.xml SHA0b4fc6c3ea607df45cc1bffdd111b585324cc67fd86a7204e9645b64d3185473。私有新v3版本/期限31测试、controller11模拟场景、HTTP6场景通过，不能作真实24h证明。首次直接运行私有脚本PYTHONPATH未指定而import失败，修正运行环境后通过，不涉及资金。
新318公开包data/grid-code-checkpoints/grid-code-20261018-trigger-visibility-v1.tar.gz SHA0c6a8eccea00021b865e850d2000a66a911fbec0918696eadadb087084c47cef，较已部署order-race包仅protection.py和position_protection测试变。远端source-trigger-visibility-v1.tar.gz/隔离stage trigger-visibility-v1-stage已校验318。实际Linux完整唯一unit entropy-grid-trigger-visibility-linux-complete.service（原PID3724011）运行中，XML/log grid-trigger-visibility-linux-complete.*，目标应1512无skip；先查原unit/XML，不重复跑。Macsession52290已terminal0。
已准备明确新v3身份RID ordinary-observe-linux-20261018-v3，全部新STATE/controller/finalizer/deadline合同timer路径，绝不复用v1/v2。新preflight明确原v35的66 runtime里仅hl_risk/live_risk/protection三处变，要求实际1512完整Linux XMLSHA+新安装proof，正式audit_release不改。真实持仓故障引用仍grid-service-position-fault-linux-v1.json。
**待核查原单次流水** entropy-grid-trigger-visibility-v3-progress.service /脚本data/run/grid-trigger-visibility-v3-progress.py，proof grid-trigger-visibility-v3-progress-proof.json。它至多等既存Linux完整测试600秒，真1512无失败/skip才运行flat Native全DEX+备份安装（grid-install-trigger-visibility-v1.py/proof grid-trigger-visibility-install-proof.json，rollback trigger-visibility-v1-rollback），再新版本review与31+11+6检查/实际看护，再新3份Native审计副本-review-v3和正式bundle/preflight，通过才唯一普通v3 installer启动controller/finalizer。每阶段O_EXCL日志及proof，不明结果只读原状态，禁止重启流水/重复金融请求。
新版本proof grid-trigger-visibility-version-proof.json/new fullXML绑定3处变化，历史v35/order-race proof不改。新普通installer grid-ordinary-linux-v3-install.py，proof grid-ordinary-linux-v3-install-proof.json；实际controller entropy-grid-ordinary-live-linux-v3/finalizer entropy-grid-ordinary-linux-v3-finalize。此前grid0直到门控全部通过。Native只读helper grid-ordinary-linux-v3-native-check.py已准备但尚未启动，流水仅启动普通controller/finalizer；实际GID出现RUNNING后须唯一entropy-grid-ordinary-native-running-linux-v3来确认正仓/订单全参数/NativeStop/3cross/gross<=150。
下一步先查流水steps/failed_stage和实际Linux完整unit/XML，绝不因为观察连接超时重跑。若普通v3实际启动，独立25h期限应在create之后/start之前由原controller武装，核对timer/contract原身份。真实观察仍从新RUNNING起完整86400秒；失败前窗不拼接。只更新独立/root/code/entropy-grid；旧/root/code/entropy和套利3343163不动。新增流水准备无资金操作，但后续通过正式门控会按既有授权真实开单。

下文“下一步尚未编码”已被本段覆盖；v2失败证据仍完整保留。

# 最新覆盖：v2启动阶段保护订单未确认，零成交已关闭；GRID0/grants空，待修复短暂查询可见性（UTC2026-10-18 09:49）

已安装并发订单观察修复（Linux完整1496passed/0skip，版本与proof见下文），此包仍当前生产核心。普通v2不是在观察中复现旧risk mismatch：它在进入RUNNING之前，原start ordinary-observe-linux-20261018-v2-start/operation530800bb-1b18-420b-a7a4-7b3903a003e1 failed/PROTECTION_ORDER_UNVERIFIED。g-0ea7d28c572a4a39最终CLOSED/STOPPED/e2，原cleanupRID...-v2-cleanup succeeded/remaining0。controller报START_NOT_RUNNING是外层症状，其state.start保存较早running响应，不可据此重发start。没有启动soak/没有任何成交，PNL/费用均0；最终Native11DEX零仓零单+备份exit0，证据source/finalization/proof已复制本机。
保护intent4bbeec4b-e7d6-4696-9420-ec7eddd8b393，CID0x978dfbcc2070f4b70000000000000205，NativeOID578208472970。Native stop实际创建timestamp1792316765459，正常cleanup取消status canceled/statusTimestamp1792316768078（不是reduceOnlyCanceled），最终显示参数ETH whole position/size0/stop2350/limit2303。dispatch_attempts：SENT09:46:04.845738Z；UNKNOWN09:46:05.650565Z/stage trigger_ack_unresolved；3个STILL_UNKNOWN时间05.980197/06.274587/06.570130。清理recovery_checked unknown_resolved1。不能把UNKNOWN直接当拒单或盲重发，原身份已Native接受后清理证明，失败资金未增加。
已在freshNative<5min、SQL全CLOSED/无queued-running、finalizer两命令0后收敛：grid-ordinary-linux-v2-failure-containment.json finished=true；备份已装grant到grid-live-release-policy-before-ordinary-v2-failure.json/旧unit grid-runtime-before-ordinary-v2-failure.service。当前普通policy再次grants=[]，/etc entropy-grid.service GRID_REAL_DISPATCH=0且无trial参数，只有独立runtime/watchdog重启，旧套利3343163未动。v2 timer disable --now，因为对应grid已CLOSED/Nativeflat；合同/失败STATE保留不可重用。v2只读Native helper未启动，不要把已准备当Native实盘运行证明。

下一步具体修复方向（尚未编码，必须复现测试后再改）：ProtectionManager.prepare位于entropy_arb/grid/protection.py约119-146，已有UNKNOWN/STILL_UNKNOWN后的两轮resolve_unknown立即查询，每轮executor5次/50ms backoff，现场15次查询在约1秒内耗尽。whole-position waitingForTrigger ACK不会提供OID，adapter立即按同CID lookup后UNKNOWN(stage trigger_ack_unresolved)；这条路径在新仓进入前仍必须严格FOUND_OPEN/cumulative_known=false。可以做单一固定有期限（例如5秒）的同CID确认窗口，paced query-only、绝不重新submit/重签/换CID、不把未知当OPEN；deadline前确认真实OPEN才允许后续初始IOC，超时/终态/epoch变化/异常仍拒绝，原30秒service保护期限不延长。要考虑existing UNKNOWN保护复用路径、neutral多侧保留旧stop，不能只修理想首次路径。先用Native-shaped test模拟初次waitingForTrigger且延迟可见，证明旧代码误失败；同时测试永不可见、终态/错误参数/epoch变化不得放行、一次signed保护order/原身份查询。
相关tests/test_grid_position_protection.py setup(rt)返回mock，并处理positionTpsl waitingForTrigger，configure(rt,gid,cfg,neutral=...)便于prepare复现；约210行manager_confirms_all_sides和255行neutral备份不撤测试可参考。Adapter.submit在hyperliquid.py约868行触发查询，executor.resolve_unknown每轮最多5次/0.05sec，不能全局增加未知订单自动重发。现有prepare最后仍两次verify全量保护，新窗口不要绕过它。
如修改第三运行文件protection.py，需要新的公开包/完整Linux测试/新版本审核，不能悄悄放宽v2 preflight期望“仅hl_risk/live_risk两处”的验证；新v3 review应明确绑定新变化+测试，并继续正式audit_release。新普通验收必须v3新RID/STATE/timer/grant候选，从零计时86400秒，v1/v2不复活/不拼接。
本轮复制本机12份关键proof含新version/安装/1496XML/v2正式审核/候选/安装/终局及containment。新v2正式reader首次错误引用historical故障v2文件的问题已修复回真实v1，不改证明内容；原review-proof保留initial_failed_stage和read_only_repair，正式审阅通过。新version删去从旧baseline继承的过时runtime_status/previous_v34字段，installed_unit_sha256已实查，baseline历史文件未改。

下文“v2安装正在等待/即将观察”等为历史；当前v2已终态关闭，普通开单关闭，以此段和服务器为准。

# 最新：并发观察修复已部署，普通v2安装已提交/正在等待实际runtime就绪（UTC2026-10-18 09:43后）

本机完整公开包1494passed/2skip/113warnings；Linux完整单次1496passed/0skip，XML grid-owned-order-race-linux-complete.xml SHA cbd96d1f03bbfbbfbfecb15d0c057c6df72307abdb5a458354e1faff50cf3625。旧1102只是网格子集，不要再称完整。318文件归档SHA7d23198769d6238329dfca897f07b5b0f6f4ff074631e5943a6be3dda250847a。已由单次entropy-grid-install-owned-order-race-v1.service在fresh Native11DEX全flat+在线备份+旧66源码核对后安装到独立/root/code/entropy-grid。proof grid-owned-order-race-install-proof.json installed/finished=true、legacy_unchanged，运行文件仅hl_risk.py/live_risk.py变，保留data/run/order-race-v1-rollback。不涉及旧套利源码或服务。
已生成新grid-owned-order-race-version-proof.json：新66hash、旧v35版本SHA、仅2运行文件变化、实际1496完整XMLSHA、新loaded identity与GRID0/健康看护检查。没改v35历史文件。新增v2 preflight validate_execution_review核对以上review与installed proof+真实XML1496无失败/跳过，再核对当前66source与旧正式audit_release，不弱化准入。10项版本门槛+21项独立退出测试在本机/Linux均通过，11个controller场景和6个dummyHTTP也通过；proof grid-ordinary-linux-v2-preparation-proof.json，仍非真实24h资格。
普通v2完全新身份：RID ordinary-observe-linux-20261018-v2，controller data/run/grid-ordinary-live-linux-v2.py/STATE同stem.json，units entropy-grid-ordinary-live-linux-v2.service 与 entropy-grid-ordinary-linux-v2-finalize.service。独立deadline脚本grid-ordinary-linux-deadline-v2.py，contract grid-ordinary-linux-deadline-v2-contract.json，timer/service entropy-grid-ordinary-deadline-linux-v2，cleanup journal/lock grid-ordinary-linux-v2-cleanup.*。不复用v1失败STATE/观察/退出请求/合同。
复审用新的只读source副本*-review-v2.json（内容保留原loop35/faultv1/smallv1身份），Native输出*-review-v2-proof.json；不覆盖v1候选引用的旧Native文件。新candidate grid-service-linux-v2-reviewed-candidate.json和grid-service-linux-v2-bundle-audit.json经真实audit_release通过，Linuxv2 preflight实际通过。首次新私有bundle错误把历史grid-service-position-fault-linux-v1.json重命名引用v2而找不到，在任何grant安装/资金请求前失败；已修正回原真实v1故障proof，无改证明/谓词。原review-runner-proof保留失败及read_only_repair，再append repaired formal/preflight成功，最终formal_bundle_passed/linux_preflight_passed=true。
**当前待核实单位** entropy-grid-ordinary-linux-v2-install.service，PID3717648，最近actual active；proof grid-ordinary-linux-v2-install-proof.json finished=false，steps已完成flat_account_online_backup_complete/only_independent_flat_runtime_stopped/reviewed_ordinary_policy_and_grid1_runtime_installed。已安装GRID1审核grant，正在180秒status/metrics/applied_runtime/guardian就绪等待，controller STATE尚未出现。先查该原unit/proof/实际DB，绝不因超时重启或重发。就绪才启动上述普通controller/finalizer，目前不能说v2已RUNNING或24h已开始。旧套利3343163不变。
已准备并在远端保存单次只读Native helper grid-ordinary-linux-v2-native-check.py（未启动），OUTPUT grid-ordinary-live-linux-v2-native-running-proof.json；应v2 grid实际创建并开始后，唯一unit entropy-grid-ordinary-native-running-linux-v2启动它，等待RUNNING+正持仓再做CID/OID/全部参数/Native止损/3cross/gross<=150核对，不产生资金动作。不要误用v1 helper或覆盖v1初次Native证明。
下一步：核查原v2安装/创建/start实际状态；成功后实际Nativecheck与soak/实际observer进程/原startRID确认，记录全新真实起止时间并等待满86400秒。若安装/预检失败先查原状态修复原因，不重新提交金融请求。此新修复只证明离线并发缺陷已解决，不能提前宣称v1现场唯一根因已修好；新kind诊断将在后续真实失败时保留binding/parameters/missing，稳定差异继续拦截。

下文“未部署/GRID0空grants/普通v1仍RUNNING”等均历史，以本段与真实服务器为准。

最新修复进行中（未部署）：本机已改entropy_arb/grid/live_risk.py与hl_risk.py，新增8项tests/test_grid_live_risk.py回归。已真实离线复现：frontendOpenOrders抓取旧快照后、本地确认cancel先入账，旧guard误BLOCKED/RISK_HL_OWNED_ORDER_MISMATCH。修复只在真实owned_order_checkpoint在collection期间改变且原粗码确为OWNED_ORDER_MISMATCH时，丢弃整次观察为RISK_OBSERVATION_CHANGED_WAIT（不签名/不发单、不产permit），新鲜稳定匹配后才允许；稳定错价/外来挂单/仓位/资金异常仍BLOCKED。新增安全kind诊断binding/parameters/missing原样随risk_paused持久化，未保存任意接口正文。这个并发缺陷已复现修复，但不能断言现场v1唯一根因；现场旧记录只有粗码。
本机8新回归通过，90项重点测试通过，86个test_grid文件1102通过（21warnings）。此1102只是网格子集，不是旧1488全包，随后已启动318文件全包tests目录的完整检查：本机exec session68561（检查实际是否仍运行/已终态），XML/log grid-owned-order-race-local-complete.*；Linux entropy-grid-owned-order-race-linux-complete.service等待先前网格子集unit终态后跑完整tests，XML/log grid-owned-order-race-linux-complete.*。先前Linux网格子集unit entropy-grid-owned-order-race-linux-full.service，XML/log grid-owned-order-race-linux-full.*。不得因观察中断而重启测试；先核对原unit/XML。
新318公开包data/grid-code-checkpoints/grid-code-20261018-order-race-v1.tar.gz SHA7d23198769d6238329dfca897f07b5b0f6f4ff074631e5943a6be3dda250847a，服务器data/run/source-order-race-v1.tar.gz；隔离stage data/run/order-race-v1-stage。SOURCE-MANIFEST.json已核对318文件，生产源码尚未修改，服务器仍旧核心+GRID0/grants空+全flat。本机完整隔离目录data/run/order-race-v1-local-suite。原manifest比当前公开源码仅4处变化：hl_risk.py/live_risk.py/tests/test_grid_live_risk.py及此前本机LINUX-CUTOVER.md文档。其余公开318旧基线相同。Linux测试runner最初stdin多行code转义SyntaxError，在任何语句执行前失败；已用repr修正后仅一次真正stage及测试启动，无资金影响。
完整测试通过后，下一步应fresh Native全DEX平仓+在线备份，校验服务器旧66源码绑定后仅独立网格目录安装已测试新包，保留回滚。不要改v35历史版本hash；新建grid-owned-order-race-version-proof.json绑定新66hash、旧v35版本SHA、仅2运行文件变化、实际完整测试XML和新loaded identity。然后为普通v2新建明确candidate/preflight/controller/deadline/finalizer/RID/STATE/timer，保留v1失败与定时器合同，重新满真实86400s，不拼接。新preflight应核对新版本审阅/完整测试/源码hash，继续调用原正式audit_release，不能为了hash变化绕过正式门槛。新普通授权前仍须三份原Linux35/fault/small审计刷新<5min并正式组合审核；原准入谓词代码没改。不要直接重开v1或盲目部署测试未通过的代码。
用户最新询问昨天套利改动是否被更新：已答本次只更新独立/root/code/entropy-grid，没有向旧/root/code/entropy同步代码、没有重启entropy-console。刚实际核对旧套利PID仍3343163/启动2026-10-18 00:47:48 CST；旧Git HEAD d8afb75(10-11)，但有大量未提交线上改动，不能仅凭HEAD日期说昨天改动缺失或已生效。所有部署proof旧PID前后相同/legacy_unchanged。后续禁止覆盖旧套利改动，不展开全机进程命令行或环境。

# 最新覆盖：普通v1在约1h57m后因挂单一致性风控退出，24h失败；账户已Native平仓，普通开单已关闭（UTC2026-10-18 09:09后）

g-fab447639ce94f50当前CLOSED/STOPPED/e3。原观察PID3660283已退出，controller已终态，原cleanup ordinary-observe-linux-20261018-v1-cleanup/operation a465ffff-cb66-4187-b8d0-3b516ba23e94最终succeeded；主state中cleanup保留较早running回复，不应据其误判原请求未终态。必须按SQL/原RID查询，禁止重投。finalizer已finished，Native审计+备份exit0，11DEX零仓零单；net PnL +.118882（gross.1584/fees.038193/funding-.001325）。原soak longest_continuous7040.548492秒，issues DURABLE_RISK_OR_PROTECTION_EVENT/GRID_EPOCH_CHANGED/GRID_NOT_RUNNING/NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE，completed=false，正式24h未通过，不能拼接后续新轮。
真正首个事件seq376 risk_paused code RISK_HL_OWNED_ORDER_MISMATCH，checked_ts2026-10-18T09:08:18.398124Z/原epoch1，暂停后epoch2，position_retained=true。用户没有手动暂停，controller按授权规则收尾，不能说用户接管。此前seq374自然归零重建窗口started1792314471850，旧止损Native reduceOnlyCanceled同刻；seq375 confirmed1792314474631（2781ms）新止损OPEN，未触发服务保护超时。旧OID578122326975、新OID578184121497；终局Native新止损status canceled/time1792314569556，是正常cleanup撤销，不是自然取消；不能声称新止损自动取消导致故障。最后4笔initial sell自然归零时间早于暂停，实际具体挂单差异没有在旧risk_observation中持久化，只保存code/epoch，当前不能假称根因已明确。
终局source/soak/finalization/Nativeproof已复制本机。正式完整bundle此前真实通过仍作历史证据，不能当本次24h成功。为了修复前阻止新开单，全库CLOSED/无queued/running、fresh Native全flat+备份确认后，仅独立网格runtime/watchdog正常重启：当前/etc unit GRID_REAL_DISPATCH=0（仍无trial参数），grid-live-release-policy.json已置空grants。保留旧grant data/run/grid-live-release-policy-before-ordinary-v1-failure.json/旧unit grid-runtime-before-ordinary-v1-failure.service；containment proof grid-ordinary-linux-v1-failure-containment.json finished=true，旧套利PID3343163未动，资金动作0。原v1 deadline timer disable --now，因对应grid已实际CLOSED且独立Nativeflat；保留原合同/失败窗口，后续不能重用v1 STATE/RID/timer来重新观察。

接下来修复：先核对实际运行状态/原请求，继续分析hl_risk.py owned_order_parameters_match和owned_account_capacity/portfolio_account_capacity与LiveRiskGuard._observe。当前只有HL_OWNED_ORDER_MISMATCH这个粗码，可能是Native采样与已变账本顺序/原生订单状态/参数不一致，尚无现场差异证据，不可一概忽略或放宽。现有guard仅对RISK_HL_OWNED_POSITION_MISMATCH且during-collection真实owned_execution_checkpoint变化转为RISK_OBSERVATION_CHANGED_WAIT，其他真实不安全观察仍保留；不要直接把所有order mismatch改wait。必要时增加固定安全字段差异诊断并用Native-shaped离线复现，完整测试/正式版本审阅后才部署（现在全flat可更改核心）。如启动新真实验证，必须新明确RID/状态/timer、原期限政策、满86400秒，不能复活这轮失败记录。正式源码66hash绑定仍v35旧基线，改核心后必须新版本审核/测试证据，不能静默更新v35历史hash或弱化preflight。
第一次排查SQL错误使用不存在events.ts列，已改只读seq/event_type/payload_json，非交易故障。曾ps全机输出意外包括旧控制台token，后续禁止全机命令行/环境输出，只查固定unit的PID与已知安全字段，勿复述token。

下文“普通观察仍运行/明天15:11验收”等已过时，以本段及真实服务器为准。

# 当前：普通实盘已实际运行，真实连续24h观察中（UTC2026-10-18 07:10:53起）

普通grid g-fab447639ce94f50，固定RID ordinary-observe-linux-20261018-v1，实际RUNNING/ACTIVE/e1；正常网站HTTP preview/create/start已执行，原start operation5e175f64-8bc7-4d7b-96e4-794dc69da7a7 succeeded。不是operator-trial，已安装原完整审核通过的grant，GRID_REAL_DISPATCH=1，加载unit无trial参数。原trial政策文件仍作历史留存，不在当前ExecStart引用。
运行服务entropy-grid.service PID3658333，看护entropy-grid-watchdog.service PID3658335，普通main entropy-grid-ordinary-live-linux-v1.service PID3658725，普通finalizer entropy-grid-ordinary-linux-v1-finalize.service PID3658723。真实只读soak观察子进程PID3660283，已核对/proc argv含正确scope ordinary/GID，不能重启观察来拼接24h。旧套利entropy-console PID3343163不变，本机只隧道、禁止重启本机执行器。
真实观察开始observation_started_ms1792307453477=UTC2026-10-18 07:10:53.477/北京时间15:10:53.477。满24h为UTC2026-10-19 07:10:53.477，主观察宽限deadline_ms1792393913477=07:11:53.477；不能提前成功。私有只读soak报告ordinary_24h_observation=true只是scope标志，不代表已跑满；当前completed_without_observed_issues=false、issues={}、continuous仅秒/分钟，实际未完成。必须报告真实连续86400秒+双时钟+无issues+准确grant/runtime实例，再原退出及终局Native审计/备份。
独立25h timer已真实安装并武装：entropy-grid-ordinary-deadline-linux-v1.timer active/waiting，due_ms1792397211040（UTC2026-10-19 08:06:51.040，calendar向上取整到08:06:52）；固定合同grid-ordinary-linux-deadline-contract.json0600，不可延期/重武装。对应.service由timer触发；与主controller共用原cleanupRID ordinary-observe-linux-20261018-v1-cleanup/独立flock+cleanup journal。未知结果只查询原身份。只清理该创建GID/完整livecontext；用户新pause/stop_keep或改配置/授权则交还控制。不能因普通控制器表面超时重启、重发start/create或另开cleanupRID。
正常网格初次独立Native检查通过，proof data/run/grid-ordinary-live-linux-v1-native-running-proof.json（远端+本机）：实际持仓.0176ETH，3cross，所属订单CID/OID/方向/数量/价格/reduceOnly全部匹配，Native止损OPEN且触发价对应配置，gross53.0640<=150；同epoch前后仓位/运行实例稳定，看护健康。read-only检查unit entropy-grid-ordinary-native-running-linux-v1已terminal0，不用重复启动。此证据不是24h证明。
普通安装proof grid-ordinary-linux-v1-install-proof.json finished=true，fresh preflight+在线备份+全flat切换、真实applied_runtime/status/metrics/guardian及旧套利未动都通过。候选/三份Native/fault/原生观察证明及本机记录均已复制。普通finalizer最多27h只读等CLOSED，之后对普通SOURCE执行通用Native审计+备份，记录grid-ordinary-live-linux-v1-finalization.json；真实满24h后还需独立终局复核，不能只看controller_claims字段。

下一步：不再创建辅助准备或反复跑全量；先查上述真实unit/observer/原请求/账本+心跳+soak issues。无变化保持安静，按已存在linux heartbeat/active goal验证等待；有风险先按原身份与政策处理。只有真实普通连续24h+终局审计/备份完成才停用linux跟进并汇报阶段成果，不将整个多交易所产品目标标complete。若观察中断/出现issue，保留原失败证据，不拼接窗口/不伪造满时长，排查后按新明确身份重新验收。下文“普通切换进行中/尚未启动”等均历史，以本段和实际服务器为准。

# 最新覆盖：Linux闭环/真实持仓故障/小残仓全部完成，正式bundle审核通过；普通24h切换进行中（UTC2026-10-18 07:05）

35 g-10ced5cc7fbb4c35已CLOSED/e6：实际4笔refill、双向maker；原cleanup f725ce8e-880c-4b3a-9136-e34eeea84f6c succeeded/remaining0；原finalizer Native审计+备份exit0，11DEX零仓零单。原网络断线恢复/自然归零重建/再次入场/带仓管理证据均保留。net PnL -.014089。不能再复活35。
Linux独立真实持仓故障 g-0ab0a9790f3c4e53/RID service-fault-linux-20261018-v1 已CLOSED/e4：真实初始IOC .0132ETH、5工作单+Native止损OPEN后一次pidfd SIGSTOP，replacement8.839954s/RPC恢复70.941508s/closed133.178178s，原故障proof passed=true，无fallback/failure，看护PID3614375不变。系统原protection-close:...:2:1 partial后同scope第2轮succeeded，不能把partial说成第一次成功。独立Native最终11DEX零仓零单，审计+备份均0，net PnL -.052439；证据grid-service-position-fault-linux-v1.json/finalization/proof/version均已复制本机。未放宽30/180门槛。
Linux独立小残仓 g-95863244dd314fda/RID service-small-linux-20261018-v1 已CLOSED，原start 3c7d75b3-8198-4c78-af23-97a2c5469abd succeeded，原tiny-close 22dd0e8e-dd48-451f-8f83-529d3394d3e6 succeeded/remaining0；Native审计+备份0/11DEX零仓零单，net PnL -.036619。小残仓脚本首次仅market只读超时，无config/grid/任何dedup或operation；核对真实原身份无资金请求后仅把market读取超时30改120，同RID/原deadline1792302126814续跑，最终成功。修复proof grid-linux-small-v1-readiness-repair.json，金融未知请求仍只查询不重投，原30min期限没延长。
私有fault启动器首次错误读取metrics['healthy']，在create前被拦住；原进程已终态/库全CLOSED/无fault请求后，从已装有限policy和真实status健康检查继续绑定新版本证明，未二次重启runtime、未延期政策。记录grid-linux-fault-v1-launch-proof.json/readiness_repair。读取旧read-only SSH watcher曾输出初始后连接不返回，已确认交易实际终态后仅停止那个本机SSH PID44716，没重投交易/没动隧道；不要因watcher表面活着认定35仍运行。

实际完整审核：data/run/grid-service-linux-review-runner.py顺序刷新3份Native审计（均<5min），grid-service-linux-review-bundle.py用35/上述Linuxfault/Linuxsmall三个不同GID和真实native-flight证据，调用原audit_release，无stub；07:03:39UTC all_local_evidence_passed=true，66个grid均CLOSED。候选grid-service-linux-reviewed-candidate.json已生成，grant_id97938074ff35dda0c6aa8a8cc9453f3f29cc3b9abd650018865e06def0690665，scope该独立账户/凭证版本/ETH long immediate或deferred/GTC/40USDC/3cross/150-30。grid-ordinary-linux-v1-preflight实际通过，不能复用早期Mac20/23或31。
当前正在执行单次entropy-grid-ordinary-linux-v1-install.service，脚本data/run/grid-ordinary-linux-v1-install.py，proof grid-ordinary-linux-v1-install-proof.json；刚提交单位，需先核查其真实状态，勿重复启动。它先fresh preflight+备份，全flat再仅停独立runtime/watchdog，装审核grant与已验证ordinary unit(GRID1/无trial参数)，真实applied_runtime+status/metrics/guardian通过后才启动以下2单位。旧套利entropy-console PID3343163保留。
未来/可能已启动的普通控制器entropy-grid-ordinary-live-linux-v1.service，状态grid-ordinary-live-linux-v1.json，固定RID ordinary-observe-linux-20261018-v1；真实网站HTTP preview/create/start、单次STATE、独立25h timer成功武装后才start。普通finalizer entropy-grid-ordinary-linux-v1-finalize.service/脚本grid-ordinary-linux-v1-finalize.py，proof grid-ordinary-live-linux-v1-finalization.json，等待实际CLOSED后Native审计+备份，尊重control_handed_back，最多27h等待。首次安装尚未等到结果，必须核查后更新本段；不能称24h开始或通过。
独立deadline仍data/run/grid-ordinary-linux-deadline.py，合同grid-ordinary-linux-deadline-contract.json，单位entropy-grid-ordinary-deadline-linux-v1.timer/.service；控制器与deadline共用cleanup journal/lock/原RID，未知只查。此普通timer仅在普通create所属核对后才武装，安装grant本身不会武装。真实24h仍双时钟+连续86400秒+终局审计，未观察不计成功。当前不用新增测试/辅助准备，先把已授权ordinary实际启动观察跑起来。

下文“35仍RUNNING、fault/small未执行、候选不存在”等均历史，以本段和真实服务器为准。

最新普通24h验收准备（Linux，尚未启动）：data/run/grid-ordinary-live-linux-v1.py已去除Mac/plist及失败31引用，仅用loop35模板、Linux reviewed candidate和applied_runtime实查已加载systemd/argv/GRID1。沿用正常网站HTTP preview/create/start/stop_flat，原request ID与一次性STATE，正式grant/真实市场资格不满足则拒绝；24h仍需双时钟真实86400秒、连续窗口及scope/grant身份，不接受提前返回的“通过”。
独立有期限退出已准备data/run/grid-ordinary-linux-deadline.py：创建普通grid且验明所属后，必须先成功武装独立25h systemd timer，才允许start。timer同时固定UTC日历/Persistent与单调25h，合同0600/O_EXCL、不可重武装或延长；失败必须不启动并正常收尾READY。独立worker只管该create RID/完整live context/GID，尊重用户pause/stop_keep/配置或授权变更。worker与主控制器共享flock+原cleanup RID+一次提交journal；丢回执/已存在原operation/重启只查询，不第二POST。worker不改主STATE，不把兜底退出当24h成功；未知退出或接口故障保留未完成状态，后续需核查原请求，不能宣称已清仓。
本机及实际Linux：21项退出/并发/丢回执/合同/时钟/武装失败测试、11项控制器假时钟场景、6项本地dummy HTTP场景全部通过；渲染的timer/service实际systemd-analyze verify通过。证据grid-ordinary-linux-controller-preparation-proof.json/XML和两个offline-proof已复制本机。这些均私有准备，无资金动作，不证明真实25h兜底或24h资格；/etc未安装timer、未创建deadline合同、普通grants仍空。首次Linux测试包装器的输出索引IndexError已修正，再跑全部隔离项通过，未触及资金。
最近实际核查now_ms1792298109466：35仍g-10ced5cc7fbb4c35 RUNNING/ACTIVE/e5，actual refill3（不是4），原start/pause/resume均succeeded、无cleanup请求/保护故障事件；heartbeat1097ms/watchdog healthy321ms。原runtime3575391/watchdog3575393、原观察3593351/finalizer3575588已确认active，旧套利3343163未动，原deadline1792315059392未延长。当前不再需要额外准备；等待原35自然第4笔或原期限，先查原单位/账本/请求，实际CLOSED后执行既定Native审计+备份，再独立Linuxfault/small。严禁把本段准备当普通实盘启动，或修改当前交易代码/政策/区间来催成交。

最新普通切换准备：发现旧grid-ordinary-v1-preflight.py及grid-ordinary-live-v1.py仍依赖Mac launchd/plist和失败31，不能直接在Linux运行。已准备data/run/grid-ordinary-linux-guard.py、grid-ordinary-linux-v1-preflight.py、grid-runtime-ordinary-linux-v1.service（仅data/run暂存，未安装）。15项本机/Linux隔离测试通过；实际systemd-analyze verify暂存unit通过，实际只读preflight正确拒绝REVIEWED_CANDIDATE_NOT_READY。证据grid-ordinary-linux-preparation-proof.json及XML已复制本机；普通grants仍空，当前runtime/政策/资金未改。
新预检只引用loop35及grid-service-linux-reviewed-candidate.json，核对当前66运行文件hash、全库CLOSED/无未决意图、空旧普通授权；普通服务严格production、GRID1、funding/commissioning0、无trial参数、固定root/DB/socket/heartbeat/正式policy、UMask0077/KillModecontrol-group。对EnvironmentFile/ExecStartPre/重复环境/错误金额/旧目录/重复参数等隐性覆盖一律拒绝。正式fault artifact必须platform=linux-systemd-pidfd，三份Native审计均需新鲜<5min，再调用原正式audit_release；没有stub代替真实审核。
下一步（当前loop自然等待期间可完成）：将24h普通HTTP控制器迁到Linux，去除旧Mac路径和31引用，使用上述applied_runtime核对真实已加载unit/argv/GRID1。现有Linux服务暂存只验证语法，不等于真实普通运行。尤其需补独立25h退出期限兜底，避免原24h控制器意外退出时留下无期限验收；可用独立systemd单调timer，在普通create已归属、start之前成功武装，按该普通grid/原cleanup RID操作，尊重用户接管。24h观察仍必须双时钟+真实86400秒，兜底timer不得当观察通过。
普通候选尚未生成；应在35+Linuxfault+Linuxsmall全部实际关闭并审计后，顺序刷新三份Native审计、生成新SHA引用和完整正式审核，不复用Mac20/23或失败31。所有这些准备不代表已开普通交易或完成24h。

最新后续准备：Linux独立持仓故障main driver data/run/grid-service-native-linux-fault-v1.py、template grid-linux-fault-v1-template.json，以及fault/small各自的grid-service-linux-fault-v1-finalize.py与grid-service-linux-small-v1-finalize.py已在本机/服务器保存，均未启动，独立政策尚未创建，未新增资金动作。fault main固定RID service-fault-linux-20261018-v1/源grid-service-native-linux-fault-v1.json，要求对应grid-service-operator-policy-linux-fault-v1.json精确授权、small_close_probe=false、无其他活动grid。
它沿用40/20/ETH long/immediate/3cross/GTC/5格/150-30，真实初始IOC后仅观察故障，不调用pause/resume抢注入窗口、不等待4refill；原观察deadline成功start后30分钟持久化，不延长。后续操作者应在该main创建gid之后，核对future-version-proof（必须含request_id、controller、source_file_sha256），再单次启动已准备的grid-service-position-fault-linux-v1.py，它会等待真实RUNNING+持仓+开仓工作单才SIGSTOP。不要提前把该injector指向当前35。
main在原watchdog自动退出期间不会抢先cleanup；会先按SQLqueued/running状态及原request_id查询已有操作，读故障/unknown不视为终态，只有原操作实际终态才允许本driver原cleanup RID。3项本机/Linux测试通过，含读故障与到期继续锁住已有退出；proof grid-linux-fault-driver-preparation-proof.json及XML已复制本机，native_held_fault_qualification=false。
两个独立finalizer只等待各自实际CLOSED再原生审计+备份，未产生资格候选或普通grant。当前35核心66文件、runtime和政策都未修改，原loop待第4笔refill，不为该准备重启当前35。

# 最新覆盖：第35 RUNNING/e5；真实网络断线恢复Native验证通过（UTC2026-10-18 04:04后）

第35 g-10ced5cc7fbb4c35仍在原RID/期限/40-20/3cross/ETH long/immediate/GTC/150-30下运行，普通grants=[]。目前3笔refill maker成交（sell .0044@2421.4，buy .0044@2426.6，buy .0044@2424），已有双向maker，但还未满>=4，未给正式资格。原deadline1792315059392/政策到期10:12:34UTC不变。

## 已完成的两个真实关键环节
- 自然归零与保护重建/再次入场：原四笔持仓全部自然卖出，旧原生止损Native reduceOnlyCanceled，时间与service_coverage_started精确对应；3197ms内新止损Native OPEN确认，再真实maker买入.0044@2426.6。证明grid-service-native-v35-natural-rearm-reentry-proof.json（远端+本机）包含原生旧止损/新OPEN证据/前后全量fills及再次入场NativeTID/OID/CID/qty/price/fee，actual_natural_rearm_and_reentry=true。第一次只读核对旧OID误传数字字符串导致422，改为int后通过；不是交易服务故障。
- 真实连接中断与带仓恢复：私有CLI data/run/grid-service-network-check-v35.py先--check-only通过（实际fd复制，不读socket内容、不改变网络），再由单次entropy-grid-network-disconnect-v35.service运行--disconnect。通过pidfd_getfd固定原runtime的socket并验证inode/UID/实例、各peer系统CA+api.hyperliquid.xyz证书；仅对自身4条交易所TCP连接做一次shutdown，未动UnixRPC/SSH/任何旧套利socket，无签名/发单/重启。
- 中断前正持仓.0088ETH，原生止损OPEN、工作开仓单所属/敞口<=150、无PLANNED/UNKNOWN/DISPATCHING/进行中的operation，guardian新鲜，Nativelease剩余>=15s才放行。真实epoch3→RECOVERING/e4→RUNNING/e5，seq328 stream_recovery_attempt ok=true/gaps=[]/drift=[]；Native正持仓.0088/新epoch保护/初始IOC仍仅1个已独立验证。实际恢复事件约266515ms，独立Nativeproof距中断266975ms（约4分27秒），不能混同3197ms的原生保护重建。恢复期间没有保护退出或guardian失效事件。
- 证据network-disconnect-proof/stream-recovery-proof/network-recovery-summary均已复制本机；actual_successful_stream_recovery=true、held_after_recovery=true。原runtime PID3575391/InvocationID不变，旧套利PID3343163未动。正式资格仍false，只此条件通过。

## 验收driver等待条件修复（未改交易核心）
发现原私有driver只看>=4refill和historical native_confirmed，可能在重连STARTING时提前cleanup，令真实恢复证明来不及完成。data/run/grid-service-native-v35.py已增加RUNNING/ACTIVE要求；本轮注入过真实断线时，还必须等同grid、正持仓Native恢复proof、恢复epoch更新、观测晚于断线时间，才可判候选。
9项本机/Linux等待条件测试全部通过。只停止并恢复原观察helper一个unit：主任务PID3575590→3593351，原RID/GID/deadline/已完成start-pause-resume状态全部复用，未重投这些金融请求；原交易runtime与66个加载文件、政策、保护门槛未变。metadata grid-service-native-v35-harness-recovery-gate-change.json含前后helperSHA/PID及相同runtime身份。不要把helper替换说成交易引擎重启。新driver仍按原deadline normal stop_flat，原cleanupRID不变；原finalizer仍等待实际CLOSED才Native审计/备份。

后续只需本轮再等自然>=4refill、原cleanup终态+Native审计/备份与正式精确审核。Linux独立持仓+工作单SIGSTOP及小残仓都仍待实际执行（已准备/测试，见下面记录），不能用上述TCP断线或临时子进程测试代替持仓进程故障，也不能把本轮观察计成普通24h。

下面RUNNING/e3和refill2/1/0等描述均历史，以本段及实际服务器为准。

最新准备（Linux小残仓）：data/run/grid-service-native-linux-small-v1.py和grid-linux-small-v1-template.json已在本机/服务器保存；未来固定RID service-small-linux-20261018-v1、源状态grid-service-native-linux-small-v1.json，要求单独私有政策grid-service-operator-policy-linux-small-v1.json且small_close_probe=true/request_ids精确仅此RID。政策尚未创建或安装，driver未运行，没有新增资金动作。
该流程沿用40/20/ETH long/immediate/3cross/GTC/5格/150-30；初始IOC真实成交后仅一次operator_trial_stop_probe，不再等待另一轮4笔refill才测残仓。核心原先已实现的单次分阶段退出保留一个qty_step后正常清零；完成仍必须有实际小额成交、原生审计和正式组合核验，准备测试不算小残仓资格。
新增settle_request在不确定/断线后只查原RID，不二次提交；进程续跑先处理原pending探测再判断仓位，结果unknown/running到期则保留pending和未完成状态，不放行另一减仓。原30分钟deadline_ms持久化不随续跑延长，正常收尾仅有独立180秒退出宽限；当前loop35政策及期限未变。9项本机与Linux测试全部通过（含丢回执、unknown/running、重启、读故障、期限、failed/partial终态栅栏）。证明grid-linux-small-driver-preparation-proof.json及-tests.xml已复制本机，native_tiny_close_qualification=false。66交易运行文件未变，不重复跑全量。
后续必须第35闭环终态+原Native审计/备份后，新建有限期独立small政策/版本证明，不能把该driver直接指向当前35；未来fault和small必须不同grid。处理原probe未知时先status原RID，绝不直接重启driver/另开请求。

最新补充（Linux故障准备）：独立未来故障脚本data/run/grid-service-position-fault-linux-v1.py已在本机和服务器准备，尚未启动/注入故障/创建故障grid；固定未来源grid-service-native-linux-fault-v1.json及RID service-fault-linux-20261018-v1，version-proof需含request_id、controller、source_file_sha256。已适配SystemdController+pidfd，核实固定watchdog unit的UID/cwd/python/module/root/manager/InvocationID；拒绝身份变化、过期Native、无开仓工作单、无正持仓等。保持原replacement<=30s/close<=180s，未改正式verifier。
9项保护测试本机通过+真实pidfd项跳过；Linux10项全部通过，真实信号仅针对测试自己创建的一次性子进程，未向网格runtime发信号。另只读核实当前runtime3575391/watchdog3575393身份，证据grid-linux-held-fault-preparation-proof.json（native_position_fault_qualification=false）。当前66个交易运行文件与政策未修改。不要把准备测试当真实持仓故障通过；后续必须当前loop结束、新RID/有限政策、独立fault campaign和Native审计才执行此脚本。

最新成交补充：第35首笔refill卖出.0044ETH@2421.4已maker成交；Native OID/CID/TID/qty/price/fee/crossed与账本精确匹配。成交派生的下笔买单2418.8已在Native OPEN。证据grid-service-native-v35-first-refill-execution-proof.json（远端+本机）。这只是1笔sell方向refill成交，距离双向>=4及自然归零重建/再次入场/真实断线恢复仍未满；原期限不延长，不注入当前loop故障。

# 当前：Linux第35 RUNNING/e3，带仓管理Native复验通过（UTC2026-10-18 03:32后）

## 当前活动任务与不可重复请求
- SSH root@taoli.coinfetcher.xyz，独立目录/root/code/entropy-grid。密钥/Users/alex/Work/entropy/Pendle_Proxy.pem，IdentitiesOnly/BatchMode/StrictHostKeyChecking始终yes；旧套利/root/code/entropy及entropy-console.service（PID3343163）保留不动。本机网格runtime/watchdog/web仍disable+bootout，只有SSH tunnel，禁止本机双写。
- g-10ced5cc7fbb4c35，RID service-loop-20261018-v35；runtime PID3575391，主任务entropy-grid-trial-v35.service PID3575590；health/native-flight/stream/held-management/finalize-v35均独立unit。当前RUNNING/ACTIVE/e3，原helper等待自然闭环。普通GRID_REAL_DISPATCH=0、grants=[]，未开放普通实盘。
- 原start a8aa1bf1-f3ad-4e5b-9e5c-7f17ac601cc1 succeeded，placed5/intents5/pending0。原pause a70c1a23-d581-4457-8739-425efc076f7a succeeded，epoch2/cancelled4/leftover0/expired1；原resume 469b645e-4f58-4016-9a60-7f73c0e3da0c succeeded，placed5/intents5/pending0。这三个操作都已终态，禁止重发。
- 原观察deadline_ms=1792315059392（6h，不延长）；独立政策data/run/grid-service-operator-policy-v35.json到期UTC2026-10-18T10:12:34.461963+00:00，仅本RID、capital/cumulative50上限；实际40total/20reserve/ETH long/immediate/GTC/3cross/5格/2.6间隔，保护150USDC/30s。原helper按原RID-cleanup stop_flat，finalizer只审计+备份，不自动安装普通资格。结果未知先查询原RID/原unit/账本，禁止重投订单。

## 第35最新真实证据
- 初始IOC仅一个intent，计划.0132、实际.0044；按实际部分成交重建。两次初始恢复对账和暂停后对账都ok=true/gaps=[]/drift=[]。没有复现第34的初始对账失败，不能说第34根因已修好。
- 持仓暂停/恢复后独立Native验证通过：grid-service-native-v35-held-management-proof.json，held_management_verified=true/held_after_resume=true，正持仓与账本一致，当前epoch的原生保护OPEN；只读observer要求owned_qty>0，空仓不计。proof已复制本机。
- 新鲜持仓/挂单核对：grid-service-native-v35-working-orders-proof-20261018T0332Z.json（远端+本机）。Native持仓.0176ETH，6个所属订单（5普通+1原生止损），所有CID/OID/方向/数量/价格/reduceOnly与当前epoch账本匹配；持仓加开仓单最大敞口53.23780USDC，低于150。其中真正refill挂单只有1个；早期工具输出refill_orders=5误标了所有普通挂单，已在proof明确regular_working_order_count=5/actual_refill_working_order_count=1，不能算5笔refill。
- 截至此核对共4笔买成交（初始IOC1+GTC3），全部taker；refill成交仍0，不能算闭环或maker资格。补单成功不等于补单成交。尚无成功stream recovery proof，也没有自然归零后的service coverage generation，不得假报。
- 1119个1s健康样本max heartbeat1113.228ms，最后runtime/watchdog均healthy且身份匹配。尚无本轮guardian/保护退出故障。原main/finalizer仍active，finalization.finished=false。

## 当前部署与已验证功能
- 当前318文件归档data/grid-code-checkpoints/grid-code-20261018T0310Z-recovery-audit.tar.gz SHA9996a6468ad2ad5c4b1617af1dcc5610669d8d340b988438a57d6c7f34018a02，66个运行文件hash绑定grid-service-native-v35-version-proof.json。data/run/grid-recovery-audit-install-proof.json已证实fresh11DEX flat+backup后安装，旧套利PID不变。活动资金期间不得改已加载交易代码或保护门槛。
- 五scheduler取消后不等待外部异步清理，保留旧worker槽位且只cancel一次；最多8个合作事件循环。7项复现通过，未改guardian3s/stale5s/Native30s。grid_main只记录慢阶段名/耗时，没有独立伪造心跳。新版空仓SIGSTOP替换11.411820s/消费marker，只是空仓，不算Linux持仓故障。
- 逐次recovery_checked事件保存epoch/之前状态/是否被新epoch替代/ok/gap和drift符号，保留原失败而不被cleanup覆盖；不保存接口正文/签名/密钥，不改任何放行条件。36相关测试通过；本机完整1501passed/2skip/113warnings；Linux完整单次1488passed/0skip/113warnings（129.74s），报告grid-recovery-audit-linux-full.xml已复制本机。不要无新改动反复跑全量。
- 独立公网https://taoli.coinfetcher.xyz/grid/已发布：Basic认证，严格公开Origin，资源/API前缀和sessionStorage键隔离；后台仍loopback8789。HTTPS证书、匿名401/正确登录200/异源403、42个JS/CSS验证通过；旧根站响应hash和服务PID不变。proof grid-public-release-proof.json；登录仅本机data/run/网格网站登录.txt及grid-public-login.json（0600），勿打印密码。旧站根路径仍套利。

## 接下来按真实证据推进
1. 当前第35正常观察自然双向>=4refill成交、maker、自然归零后原生保护重建/再次入场及真实断线恢复，不主动撮合、不改区间/期限、不为故障测试破坏当前loop。原期限或异常由原cleanup收尾，先核对原身份。
2. 第35终态后Native审计、备份和精确正式核验；不得复活31/33/34/35。Linux仍需独立真实持仓+开仓单的故障替换及退出、小残仓验证。formal service verifier要求fault/loop/small不同grid，不能把空仓SIGSTOP、Mac旧测试或本轮暂停恢复替代Linux持仓故障。
3. 如需故障准备，最新Mac私有参考data/run/grid-service-position-fault-v8.py（第23、有工作单），需要适配SystemdController+pidfd并验证Linux固定watchdog unit身份；不得直接运行旧Launchd脚本。原正式fault门槛replacement<=30s/close<=180s、正持仓/原生止损OPEN/工作单所属/敞口<=150等均不可放宽。小残仓参考v20的operator_trial_stop_probe政策，必须新RID/新有期限small_close_probe授权；不得插入本轮35。
4. 完整正式审核精确账户/凭证版本/ETH/方向/GTC/金额/150-30后才能开普通网站新网格，实际连续24h+终局审计/备份后停用linux跟进。不把当前私有trial或未满观察当24h完成，也不把整个多交易所产品目标标complete。
5. 已有每15m heartbeat linux和active goal，不再创建重复自动化；无变化保持安静，只关键进展/故障/风险/需用户时通知。多币种/第二真实交易所/回测推荐/高级功能仍保留产品范围。

## 历史资金与问题
- 第34 CLOSED/e2，原start INITIAL_POSITION_UNRESOLVED，原cleanup741817b5-b2ed-4a3b-90d7-64177c961924 succeeded/remaining0；Native11DEX零仓零单、198.709458USDC、net-.008919，审计/备份exit0。具体旧recover原因没持久化，不能假称已定位HL_UNIFIED_OBSERVATION_CHANGED；第35新增诊断才可准确捕获后续故障。此余额是第35前历史，当前有仓位，不能当当前余额。
- 第33 CLOSED/e5，guardian HEARTBEAT_STALE5305.967ms触发退出，Nativeflat198.718377、net-.05605，审计/备份0。离线复现调度清理阻塞，但无现场栈，不宣称唯一原因。
- 第32正常退出flat198.774427、net-.019946，初始IOC未实际执行，不能当完整immediate资格；第31Mac合盖失败不可复用。
- 详细前史：RESUME.history-through-v35-start-20261018.md、RESUME.history-through-v33-20261018.md、RESUME.history-linux-bootstrap-20261017.md、RESUME.history-through-v31-20261017.md。实际服务器账本/原请求始终优先。

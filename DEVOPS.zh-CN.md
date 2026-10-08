# entropy-arb 开发运维手册 / DEVOPS

> 写作时间：2026-09-29 · 维护纪律：**改了行为就改这里**（本手册是唯一要保
> 持最新的总览，其余文档都是专题快照）
> 操作形态：**一切日常操作从 console 网页完成**，终端只用于基础设施
> （systemd / git / 看日志文件）。CLI 工具全部保留作为故障后备。

**与其他文档的关系**（本文不重复的内容）：

| 想了解 | 去看 |
|---|---|
| 首次部署一台新服务器 | [deploy/DEPLOY.zh-CN.md](deploy/DEPLOY.zh-CN.md)（已按本机核查，大部分"已就绪"） |
| 从密钥到上线一条线的图文流程 | [deploy/OPERATIONS.zh-CN.md](deploy/OPERATIONS.zh-CN.md) |
| maker 策略设计与安全论证 | [MAKER-DESIGN.md](MAKER-DESIGN.md) |
| Backpack 接入设计 / 状态 | [BACKPACK-PLAN.md](BACKPACK-PLAN.md) |
| 基差/选标的方法论 | [BASIS-EXPLORE.md](BASIS-EXPLORE.md) |
| 历史交接快照（写就不再改） | [HANDOVER.md](HANDOVER.md)、[HANDOVER-LIGHTER-KATANA.md](HANDOVER-LIGHTER-KATANA.md) |
| 用户视角说明 | [README.zh-CN.md](README.zh-CN.md) / [README.md](README.md) |

---

## 0. 30 秒速查

| 项 | 值 |
|---|---|
| 正式入口 | `https://taoli.coinfetcher.xyz/?token=<token>`（token 在 gitignored 的 `deploy/console-token.env`） |
| 本机入口 | `http://127.0.0.1:8788`（未显式传 `--token` 时 loopback 免 token；本部署 unit 始终带 token，本机访问同样需要） |
| worker 状态端口 | 8801 起，每个 worker 一个（只读，console 反代） |
| 仓库 / 分支 | `/root/code/entropy`，`main` → `github.com/rumbuying/entropy-arb` |
| 测试 | `python3 -m pytest tests/`（当前 179 passed，推送前必须全绿） |
| 告警 | watchdog 每 2 分钟一轮 → Telegram（`/etc/default/entropy-watchdog`） |
| 高频问题 | LIVE 被拒→§2.2①；启动即 errored→§3.1；EXPOSED/HALT→§3.2/3.3；改代码不生效→§4.1 |

---

## 1. 系统架构

```
                    浏览器（token）
                        │ https (nginx 443 反代)
                        ▼
  systemd: entropy-console ── console.py :8788（aiohttp）
      │  supervisor：spawn/adopt/stop worker 子进程
      │  /api/*（auth 中间件）：profiles / secrets / workers /
      │  venues / analytics / diagnostics 🩺 / flatten ⚠
      ▼
  engine worker（main.py，每线一个进程，--web 8801+N 只读总览）
      │  ws 行情：HL 官方 ws / zkLighter / Katana / Backpack
      │  策略：taker band 或 maker（互斥）
      │  recorder：1s 采样 → 分钟 CSV
      ▼
  交易所 REST/ws（下单路径 Ed25519 / EIP-712 签名）

  systemd 副服务：entropy-probe(-rh).service（采集对标）
                 entropy-autoband.timer（每 5 分钟校准 band）
                 entropy-watchdog.timer（每 2 分钟巡检+告警）
                 entropy-backup.timer（周日 03:00 备份配置与密钥）
```

### 1.1 systemd 单元

| 单元 | 周期 | 作用 | 紧急停用 |
|---|---|---|---|
| `entropy-console.service` | 常驻+开机自启 | 控制台本体（含 supervisor） | `systemctl stop entropy-console`（会优雅停掉所有 worker） |
| `entropy-autoband.timer` | `*:0/5` | 按美东时段校准各 profile 的 band（写回 profile，引擎 60s 热加载） | `systemctl disable --now entropy-autoband.timer` |
| `entropy-watchdog.timer` | 每 2 min（`OnUnitActiveSec`） | worker 崩溃 / HALT / 交易所断连 / **距强平<10% / 保证金用满90%** → Telegram 告警一次、恢复通知一次（后两项直查交易所，引擎挂了也报） | `systemctl disable --now entropy-watchdog.timer` |
| `entropy-probe.service` / `-rh.service` | 常驻 | 主网 / rh 的 Lighter↔Katana 多标的采集（`tools/basis_probe.py`，改标的=改 unit 里 `--symbols` 后 `daemon-reload`+restart） | 同上模式 |
| `entropy-backup.timer` | 周日 03:00 | config.yaml + profiles + .env → `/root/backups`，留 8 份 0600 | — |

### 1.2 数据文件地图（都在 `logs/`，已 gitignore + logrotate）

| 文件 | 写入者 | 用途 |
|---|---|---|
| `minutes[-<SYM>-<a>-vs-<b>].csv` | recorder / basis_probe | 分钟级行情与溢价条——**Analyzer 与 auto_band 的唯一输入**。recorder 打开时若发现文件无表头（轮转脚本剥掉了）会**就地补表头**、历史留在原文件（auto_band 只读这一个文件；2026-10-09 曾因轮转成 `.old` 让 ANTH 中枢 -176→-231）；表头 schema 不一致才轮转到 `.old` |
| `trades-<SYM>-<hedge>.csv` | taker 引擎 | 成交流水（FIFO 已实现盈亏按平仓日记账，Venues 页引用） |
| `maker-trades-*.csv` / `maker-selection-*.csv` | maker 引擎 | 每批对冲的毛/净边际；逆向选择样本（成交时/+1s/+10s 溢价） |
| `engine-*.log` / `engine.log` | 引擎 | 全量日志（Runs 页日志窗口 tail 的是这里） |
| `console-server.log` / `console.log` | 控制台 | 含 AUDIT 行（密钥改动/启动/平仓，永不含值） |
| `watchdog.log` | watchdog | 告警历史 |

### 1.3 代码模块地图

| 模块 | 职责 |
|---|---|
| `main.py` | 引擎入口；`--symbol/--hedge/--base` 每次显式给 |
| `entropy_arb/config.py` | YAML 严格校验 + .env 解析；venue 枚举与 VenueConf 构造（**加 venue 的第一站**） |
| `entropy_arb/engine.py` | 两腿策略循环：taker band 扫描、net-delta 对冲、reconcile、maker 循环与安全梯 |
| `entropy_arb/maker.py` | venue 无关的 maker 契约 + 报价数学（锚定/clamp/库存偏斜/vol widen） |
| `entropy_arb/venue_{hl,lighter,katana,backpack}.py` | 各所适配器（统一契约，见 §5.2） |
| `entropy_arb/feeds.py` | 三个家族的行情 ws（Lighter snapshot+nonce / HL l2Book / Katana·Backpack snapshot+seq） |
| `entropy_arb/{recorder,analysis,autoband}.py` | 分钟采集 / Analyzer+回测引擎 / band 校准 |
| `entropy_arb/state.py,web.py,dashboard.py` | 引擎只读状态、内嵌 web、终端仪表盘 |
| `entropy_arb/console/` | supervisor / server / secrets / profiles / analytics / venues / **ops（诊断+平仓）** |
| `entropy_arb/webui/` | 前端（无构建 ES modules，`/static/` 直出） |
| `tools/*.py` | analyze / backtest / auto_band / basis_probe / basis_matrix / flatten_line / backpack_check / isolated_margin / day_assess（全部有 console 等价按钮或属后台服务；CLI 为后备） |
| `tests/` | 离线 pytest；`maker_contract.py` 是 venue 无关的契约套件 |

---

## 2. 日常运维（全部网页完成）

### 2.1 访问

- 正式地址带 `?token=`；token 存 sessionStorage，换浏览器需重带完整链接。
- 控制台只绑 127.0.0.1，外网经 nginx 443 反代；所有 `/api/*` 无/错 token 一律 401。
- Console V2 并行入口：`/console-v2`（与旧页同 token 机制；旧页 `/` 保留为后备）。
  V2 引入 `data/console-v2.sqlite3`（gitignored）：持久策略身份 / run / 操作 / 配置版本 /
  导入事件 / 实验草稿，仅 console 进程写。worker 事件采集写到 `logs/events/<run_id>.jsonl`
  （由 console 启动时经 env 传入 run 身份；adopt 的旧 worker 无此环境，保持 CSV 采集）。
  开发进度见 `CONSOLE-V2-DEVELOPMENT-SPEC.zh-CN.md`。

### 2.2 SOP：新上一条线（以 backpack 为例，全程 ≤ 7 步）

1. **密钥**：交易所侧建好 key（只开 Trade）→ API Keys 页对应卡片粘贴 →
   徽章变 `✓`（即时报错=格式不对，backpack 是 base64-32 字节校验）。
2. **🩺 诊断**：同一张卡片输品种 → **勾选挂撤单测试** → 应得
   market/signer/equity/position/order_path 全 ✓（这一步等价于旧 CLI
   `tools/backpack_check.py --order-path`）。
3. **建 profile**：Strategy Config → 新建（名字建议 `<标的>-<线>`）；能保存
   = `load_config` 严格校验通过 = 启动一定过配置关。
4. **采集**：Runs → Start，模式 **RECORD-ONLY**，跑 ≥1–2 天（隔天更好，
   溢价有日内 regime）。
5. **定参**：Analyzer 选该 profile → 看分布/触发表/建议阈值 → 一键应用；
   maker 线以 `costs_bps`（maker 费+对冲费+半点差）与 sd 的比值判定可行性。
6. **LIVE 最小档**：`max_position_usd` 从 $100–200 起步，Runs → LIVE（输入
   品种名二次确认）→ 盯 Overview 30–60 分钟。
7. **观察**：maker 看 `maker-selection-*.csv` 的 prem_fill/1s/10s；taker 看
   trades CSV。之后 watchdog 接管，可以关页面。

> backpack 两条候选线的 profile 已备好：`profiles/lighter-rh-hype-backpack.yaml`
> （BP 挂单+rh 对冲）、`profiles/backpack-hype-katana.yaml`（katana 挂单+BP 对冲）。

### 2.3 日常例行

- **每周**：Analyzer `hours=24` 复测各 live 线（阈值漂移是最大的慢性亏损源；
  有 autoband 的 profile 会被自动校准，删 `auto_band.enabled` 才是固定参数）。
- **改参数/加仓**：profile 编辑 → 保存 → Runs 页 **restart** 该 worker
  （只有 thresholds 三个 band 字段是 60s 热加载，其余都要重启）。
- **服务器重启后**：console 会被 systemd 拉起并"收编"幸存 worker 的记录，
  但 **worker 进程要靠系统重启前 systemd 停掉**；若 worker 死了，Runs 页手动
  restart（设计如此：不自动复活一个可能带着旧状态的引擎）。

### 2.4 停线 / 平仓

- 正常停：Runs → Stop（SIGTERM → 结算在途单 → 对账 → 退出，≤25s）。
- **有残留持仓的线**：Runs → **⚠ 平仓**（输 SYMBOL 确认）：自动先停引擎，
  再用 reduce-only IOC 双腿平残留——reduce-only 数学上不可能反向开仓；两腿
  行情未就绪时一单不发；结果与逐腿日志直接回显在弹窗。
- 平仓后 `flat=false` → 弹窗日志就是现场，按 §3 处置。

---

## 3. 事故处置 Runbook

> 通用原则：先看 Runs 页日志尾部（等于 `logs/engine-*.log` 尾部），再动手；
> 一切自动熔断都是"停止新风险、保留现场"，不会乱平仓。

### 3.1 启动即 errored / 卡 starting
- 日志尾部找第一条 ERROR：密钥与 `--hedge` 部署不匹配（lighter 主网≠rh）、
  市场不存在（核对 profile 的 `hedge.symbol`/`entropy.symbol` 别名）、
  凭证缺失。卡 starting = 某侧 ws 连不上（网络/代理）。

### 3.2 maker 线 EXPOSED（对冲连续失败，已撤单+停机）
- 触发：`max_hedge_failures`（默认 3）次对冲失败 → 引擎清光挂单并 HALT。
- 处置：日志找失败原因（限频？保证金？对冲腿断连？）→ 修复 → ⚠ 平仓按钮
  核对残留=0 → 修复项确认后 restart。
- 历史根因参考：Lighter nonce 冲突（§6.4）、rh 保证金率按标的而异（§6.5）。

### 3.3 taker 线 HALTED（连续执行错误）
- 触发：`max_consecutive_errors`（默认 3）→ 引擎自停，**持仓保持**。
- 处置：日志定位（多半是单腿拒单/未决）→ ⚠ 平仓核对双腿 → 修复 → restart。

### 3.4 交易所 API 不可达（DOWN）
- 连续 3 次持仓查询失败 → 该 venue 标记 DOWN，暂停交易并每
  `venue_probe_sec`（30s）探测，恢复自动 RESUMED（日志+watchdog 通知）。
- 多为交易所维护，等恢复即可；若长时间不恢复，⚠ 平仓退出该线。

### 3.5 watchdog 强平/保证金告警
- 立即处置项：Overview/venues 表核对各腿持仓与"距强平"；优先 ⚠ 平仓减风险；
  事后查保证金率假设是否错了（§6.5）。

### 3.6 单腿敞口核对纪律（血泪教训）
- **平任何仓之前先核对方向**。账户接口的 `position` 字段可能是**无符号**
  量（zkLighter：方向在 `sign` 字段）；Backpack 的 `netQuantity` 是带符号的。
  引擎 reconcile 首次运行会打印两侧持仓，眼睛过一遍再信。
- 2026-09-25 rh↔katana 首跑把正确的对冲当裸露平掉、制造 4.4 HYPE 裸空的
  事故即源于此（HANDOVER-LIGHTER-KATANA §5）。

---

## 4. 部署与升级

### 4.1 改了代码，怎么生效（最容易踩的坑）

| 改动 | 生效方式 |
|---|---|
| `entropy_arb/webui/*.js`（前端） | **浏览器刷新**即可（`/static/` 已禁缓存） |
| profile 的 `thresholds` 三个 band 字段 | 引擎 **60s 内热加载**（autoband 5 分钟会按数据纠回） |
| `entropy_arb/engine.py`、`venue_*.py`、`feeds.py` 等 | Runs 页 **restart 对应 worker** |
| `entropy_arb/console/*`、`console.py` | `systemctl restart entropy-console` —— **worker 不受影响**：unit 是 `KillMode=process`，只杀主进程，新 console 启动时自动收编（adopt）所有在跑 worker；要换引擎代码再用 Runs 页逐个 restart |
| `deploy/*.timer` 单元 | `systemctl daemon-reload` + restart 对应 timer |

### 4.2 代码升级 SOP

```bash
cd /root/code/entropy
git pull                      # 本地开发推送后
systemctl restart entropy-console
# → 浏览器重新进入（token），Runs 页逐个 restart 需要新引擎代码的 worker
```
- 升级前：`python3 -m pytest tests/` 全绿再上（服务器上直接跑一次最稳）。
- 无数据库、无迁移；回滚 = `git checkout <旧commit>` + 同样两步重启。
- 备份：周日自动；大升级前手动 `./deploy/entropy-backup.sh`。

### 4.3 必须保留终端的两类事

1. systemd / nginx / 防火墙 / 日志轮转（`deploy/DEPLOY.zh-CN.md` §3 的速查命令）。
2. git 拉取与推送（§5.7）。—— 其余一切都有网页等价物。

---

## 5. 开发指南

### 5.1 环境与测试

```bash
pip install -r requirements.txt          # 采集/分析/全部离线测试
pip install -r requirements-live.txt     # 实盘签名依赖（eth-account、lighter-sdk、cryptography）
python3 -m pytest tests/                 # 必须全绿；当前 179 passed
```
测试**全部离线**：HTTP 用 `FakeSession`（`tests/maker_contract.py`），ws 帧
直接注入 feed 的 `_handle*`，签名用固定密钥的 golden 向量。禁止写需要外网
的测试。

### 5.2 venue 统一契约（引擎只认这个，加所不改引擎）

任何腿必须实现（参考最新实现 `venue_backpack.py`）：

| 接口 | 契约 |
|---|---|
| `load_market()` | 解析符号别名（如 `SOL`→`SOL_USDC_PERP`）、tick/step/min_base、市场状态检查 |
| `init_signer()` | 懒加载签名依赖（离线/采集环境不装也能跑 record-only） |
| `start_tasks(stop, notify, live)` | 公共行情 feed 必启；live 加私有订单流 |
| `ready_to_trade()` | maker 模式下**私有流就绪才许报价**（盲腿不报价是安全特性） |
| `send_taker(is_buy, qty, limit_px, reduce_only)` | IOC 限价+滑点保护，同步结算 → `{status, filled_base, avg_px, err, unresolved}`；**超时/5xx → unresolved=True**（引擎升级到 reconcile）；限频错误必须带 `RATE_LIMITED:` 前缀 |
| `place_maker(...)`（maker_capable 才需要） | post-only；**交叉被拒不是错误**：`status="canceled", reason="would_cross"`；响应带 `took_liquidity`（post-only 绝不应吃单） |
| `cancel_orders(order_ids=None)` | None = **market-wide 原子全撤**（安全路径，必须单请求）；按 id 撤可逐单（`expects_batch_cancel=False`，见 maker_contract） |
| `on_fill(cb)` / 私有流 | fill 事件**增量**数量 + 幂等去重（per-fill id 为主、累计量差分兜底），去重表活过重连 |
| `fetch_position() / fetch_equity()` | 带符号净持仓；（equity, free） |

契约强制：`tests/maker_contract.py::run_contract(MakerCase)` —— 新 venue 写
个 shim（照 `tests/test_backpack_maker.py`）跑一遍即可，契约测试会失败而不是
引擎在实盘里失败。

### 5.3 新增一个 venue 的完整清单

venue 已收敛为**单一注册点**（`entropy_arb/venue_registry.py`）：config 三元组、腿构造、
engine/console 工厂、secrets 校验、funding 矩阵、discovery、v2 前端卡片与下拉全部从注册表
派生。接入手册（含前置调研三问：断线撤单语义 / 私有流认证形态 / fill 判别实盘核对）见
**[ADD-A-VENUE.zh-CN.md](ADD-A-VENUE.zh-CN.md)**。速查：

1. `entropy_arb/venue_<x>.py`：Venue 合同 + 三个模块钩子（`make_venue` /
   `make_public_feed` / `list_markets_catalog`），尽量用 `venues_common` 的基类
2. `venue_registry.py`：一条 `VenueSpec`（+ `UI_CARDS` 卡片；lighter 式 per-leg 覆盖
   才需要 `NEED_GROUP`）
3. `config.py`：新 Creds dataclass（`.complete`）
4. `tests/test_<x>.py` + 契约 shim；`test_venue_registry.py` 会自动查漏
5. 样板：README×2、`.env.example`、`config.example.yaml`、试点 profile ×2
6. **实盘前**：console 🩺 诊断（含挂撤单）→ 记录该所限频/费率到文档 → 私有 WS fill
   判别实盘核对后才开 maker

### 5.4 maker 引擎要点

- 报价**锚定对冲腿可成交价**（非中间价），`clamp_to_maker_book` 保证不穿越
  maker 自身盘口（基差大的对必须的，否则 post-only 连续被拒烧订单预算）。
- requote 是"撤+挂"两次配额：`requote_bps` 太紧会打满 `max_orders_per_min`
  （rh↔katana 实测 1bp 太紧，3bp 起步）。
- `vol_widen` 从 recorder 的 minutes CSV 播种波动率——**引擎测试必须把
  `recorder.csv` 指进 tmpdir**（test_maker_engine 曾因读到真实部署数据必红，
  已修，别回退）。
- 重启 live maker 前确认无孤儿挂单（启动时 cancel-all 是引擎层待办 §6.8）。

### 5.5 配置体系

- profile/config 走同一 schema **严格校验**：拼错的键是启动错误，不是静默
  失效。console 能保存 = `load_config` 通过 = 启动必过配置关。
- `.env` 是密钥唯一事实源，`load_dotenv(override=True)`——console 存的密钥
  立即对下一次 worker 启动生效；不要在 shell 里 export 覆盖它。
- 密钥永不回显、不入 profile、不入 git（见 §5.7 检查清单）。

### 5.6 前端（`entropy_arb/webui/`）

- 无构建步骤的原生 ES modules，`/static/` 直出且 `Cache-Control: no-cache`
  ——改完刷新即生效。
- venue 名单硬编码在四处：`secrets.js`（GROUPS 卡片）、`runs.js`（两个下拉）、
  `profiles.js`（profile 编辑器 + 新建对话框）——加 venue 别漏，全部要配
  `i18n.js` 英文+中文两份。
- 后端 API 有行为变化时，前端对应 js 与 `tests/test_console_api.py` 一起改。

### 5.7 git 约定与提交前检查清单

- 提交信息风格：`域: 祈使句摘要`（如 `maker: adaptive volatility widen…`），
  正文讲 why；一个主题一个 commit。
- 推送前清单：
  - [ ] `python3 -m pytest tests/` 全绿
  - [ ] `git status` 里没有 `.env`、`logs/`、`config.yaml`（均 gitignore）
  - [ ] 密钥扫描：`git diff --staged | grep -inE "private[_-]?key\s*[=:]\s*['\"]?[0-9a-fx]{20,}|api[_-]?secret\s*[=:]\s*['\"]?[A-Za-z0-9+/]{16,}"`
  - [ ] 行为变化同步到了本手册 §1/§3/§6
- 远端：`origin = git@github.com:rumbuying/entropy-arb.git`（main）。

---

## 6. 坑位清单（都会再咬人）

1. **`position` 无符号**（zkLighter）：方向在 `sign` 字段；Backpack
   `netQuantity` 带符号。平仓前先核对方向（§3.6 事故）。
2. **保证金率按标的而异**：rh ANTH 20% vs HYPE/ZEC 50%；Backpack 是
   `imfFunction`（sqrt 型，base+factor×√名义）。按统一费率估仓位会打出
   意外强平距离，开线前先读实际值。
3. **post-only 交叉拒绝**是正常事件：必须映射 `would_cross`，绝不能计入
   连续错误——否则快市场里重挂会烧光订单预算并误触发 HALT。
4. **Lighter nonce 按 (account, api_key) 计数**：多 worker 共用一个 key 会
   互相踩（21104）。已有撞 nonce 自动刷新+重试一次；根治是每 worker 独立 key。
5. **post-only 的 bool 有两种形态**：签名串里是小写文本（`postOnly=true`），
   JSON body 里是真布尔——签名与发送分离（Backpack 实测踩过，已测试锁定）。
6. **签名时钟窗口**（Backpack 默认 5000ms）：启动时从 `/api/v1/time` 校准
   偏移；服务器 NTP（chrony）必须健康。
7. **测试封闭性**：任何"从文件/环境读状态"的逻辑（如 vol_widen 播种
   minutes.csv）在测试里必须指向 tmpdir，否则在部署机上必红。
8. **重启 live maker 会留孤儿单**：启动时先 cancel-all 是引擎层待办（未做），
   当前靠人工确认 0 挂单再启动。
9. **薄盘假行**：远离盘口的孤立挂单会造出几百 bps 假溢价——`max_signal_edge_bps`
   幻影过滤必须保留（recorder 侧还有 `max_spread_bps` 采样过滤）。
10. **session key 会过期**（katana 委托密钥 ~30 天）；Backpack API key 长期
    有效但也可能被平台风控——🩺 诊断是发现这类问题的第一手段。
11. **让新代码生效**：引擎改动要 restart worker，console 改动要 restart
    console，前端只要刷新（§4.1 表格）——"改了没生效"九成是这层没对上。

---

## 7. 控制台 API 一览（开发用；全部受 token 保护）

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/meta` | GET | 控制台元信息（profiles 目录、.env 是否存在、token 模式） |
| `/api/profiles[...]` | GET/POST/DELETE | profile CRUD + `/validate`（与 load_config 同一校验）+ `/new` 模板 |
| `/api/secrets` | GET/POST | `.env` 键状态（掩码）与写入（逐键校验+审计） |
| `/api/workers` | GET/POST | 列表 / 启动（live 必须 `confirm=<symbol>`，先过凭证预检） |
| `/api/workers/{id}/state·logs·ws` | GET/WS | 只读快照 / 日志 tail / 实时事件桥 |
| `/api/workers/{id}/stop·restart`、DELETE | POST/DELETE | 生命周期（删除需先停止） |
| `/api/venues` | GET | 跨 worker 的交易所维度汇总（权益取 max、持仓求和、FIFO 已实现） |
| `/api/diagnostics` | POST | 🩺 venue 体检：`{venue, symbol, role, dex, order_path}` → 分步 ✓/✗ |
| `/api/flatten` | POST | ⚠ 停 worker + 双腿 reduce-only 平仓：`{wid, confirm}` → 逐腿结果+日志 |
| `/api/analyze`（GET）、`/api/backtest`（POST）、`/api/minutes`（GET） | 混合 | Analyzer/History 页数据（console/analytics.py） |

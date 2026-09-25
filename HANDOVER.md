# 交接文档 / HANDOVER

> 本文档面向"换一台电脑继续运行/开发"的场景：当前代码状态、如何跑起来、
> 如何从本地浏览器访问服务器上的控制台、已验证范围与待办。
> 最后更新：本次 Web 控制台交付时（见 git log）。

---

## 1. 项目现状一句话

entropy-arb（双交易所永续套利机器人）在原有 CLI/TUI 之上，已交付**完整的
Web 管理控制台**（P1–P4 全部完成）：引擎内嵌只读网页总览 + 独立控制台
（密钥管理 / 策略配置可视化 / 运行管理 / 分析工作台 / 历史数据）。
**42 个测试全部通过**，并已用真实交易所行情做过端到端冒烟。

本次新增代码约 4900 行；**零新增第三方依赖**（控制台/网页全部基于已有的
aiohttp + 原生 ES modules，无构建步骤）。

> **2026-09-25 更新**：本轮工作（基准腿解耦 `--base`、Katana 委托密钥、
> maker 报价夹逼、Lighter↔Katana 实盘试点与其 nonce 阻塞）单独整理在
> **[HANDOVER-LIGHTER-KATANA.md](HANDOVER-LIGHTER-KATANA.md)**，接手该线请先读它。
> 测试数已增至 132。

## 2. 新机器快速开始

```bash
git clone git@github.com:rumbuying/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # 控制台/采集就够了
cp config.example.yaml config.yaml         # 传统 CLI 方式才需要；控制台用 profiles/
cp .env.example .env                       # 或者直接在控制台"密钥管理"页里填
```

实盘签名 SDK（交易时才需要）：

```bash
pip install -r requirements-live.txt
```

### 2.1 启动控制台（推荐入口）

```bash
python3 console.py                # 默认 http://127.0.0.1:8788
```

- 控制台本身**不交易**，只管理配置/密钥/引擎子进程。
- 它启动的每个引擎 worker 独占一个回环端口（默认从 8801 起分配）用于只读
  状态上报。

### 2.2 从本地电脑访问服务器上的页面（重要）

控制台默认只绑定 `127.0.0.1`（安全默认）。两种访问方式：

**方式 A：SSH 隧道（推荐，免 token）** —— 在本地电脑执行：

```bash
ssh -L 8788:127.0.0.1:8788 -L 8787:127.0.0.1:8787 用户@服务器
```

然后本地浏览器打开 <http://localhost:8788>（控制台）；
8787 是单引擎总览页（引擎带 `--web` 启动时才有）。

**方式 B：绑定公网/局域网地址（带 token）**：

```bash
python3 console.py --host 0.0.0.0
```

非回环绑定强制要求 token：首次启动自动生成（打印 + 存 `logs/console-token`，
该文件已 gitignore），并打印一条带 `?token=...` 的完整链接，浏览器直接打开
即可；前端会把 token 附到所有 API/WebSocket 请求上。已验证：
无 token → 401，带 token → 200，错 token → 401。注意放行防火墙端口。

### 2.3 单引擎 + 网页总览（传统 CLI 方式，可选）

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh --web
python3 main.py --symbol SNDK --hedge lighter-rh --web        # 实盘
```

`--web [端口]`（或 config.yaml `web.enabled: true`）提供**只读**总览页；
终端 Rich 仪表盘照旧可用（两者互不影响）。

## 3. 日常使用流程（控制台六页签）

1. **密钥管理**：粘贴 HL / Lighter 密钥 → 保存（写入 `.env`，0600 权限，
   保存后永不回显，只显示尾 4 位；格式即时校验）。同一组 LIGHTER_* 密钥
   服务于主网/Robinhood 两个部署，务必填与启动 `--hedge` 一致的部署。
2. **策略配置**：新建 profile（`profiles/<名字>.yaml` + 同名 `.json`
   sidecar 存 symbol/hedge）。保存必须通过真实 `load_config()` 校验——
   **能保存的配置，`main.py --config <profile>` 就一定能启动**。
   阈值区可加载实测溢价分布做带宽预览。
3. **运行管理**：先 `RECORD-ONLY` 启动收数据（每 profile 独立的
   minutes/trades/log 文件，互不干扰）→ **分析工作台** 选该 profile 跑
   分析/回测 → 一键把建议阈值填回编辑器 → 保存 → 切 `LIVE`
   （需输入品种名二次确认；所选交易所密钥不完整时直接拒绝启动）。
4. **历史数据**：查看采集的溢价与双向可成交 edge 时间序列。
5. **总览**：每个运行中引擎一张实时卡片（4Hz WS 推送）。

停止：`pkill -f console.py`（会连带优雅停止所有它启动的 worker；
worker 收 SIGTERM 会先结算在途订单并对账，正常 0.1–0.3s 退出）。

## 4. 架构与文件地图（本次新增/修改）

```
console.py                    控制台入口（supervisor + profiles + secrets + 分析 API）
entropy_arb/
  state.py                    引擎快照纯函数（TUI 与 Web 共用同一份取数逻辑）
  web.py                      引擎内嵌只读 Web 服务（/api/state /api/health /ws）
  logbuf.py                   日志环形缓冲（从 dashboard 抽出，web 不依赖 rich）
  analysis.py                 analyze/backtest/minutes_series 共享实现
  console/
    secrets.py                .env 安全读写（校验/掩码/审计/0600/逐行 patch）
    profiles.py               profiles CRUD + load_config 校验复用 + sidecar
    supervisor.py             worker 子进程生命周期/端口分配/日志 tail/优雅停止
    server.py                 控制台 HTTP API + WS 桥接 + token 鉴权中间件
    analytics.py              /api/analyze /api/backtest /api/minutes
  webui/                      无构建前端（原生 ES modules + canvas 图表，双语）
    engine.html               引擎只读总览页
    console.html              控制台（六页签）
    engine-view.js / charts.js / i18n.js / fmt.js / api.js / yaml-lite.js
    overview.js / runs.js / profiles.js / secrets.js / analyze.js / history.js
main.py                       新增 --web/--no-web/--log-stdout
tools/analyze.py, backtest.py 改为 analysis.py 的薄封装（CLI 输出不变）
profiles/README.txt           profiles 目录说明（配置不含密钥，是否入库自定）
```

设计铁律（后续开发请延续）：
- **单一事实源**：控制台编辑的就是引擎启动读取的同一批文件，无第二配置系统；
- **校验复用**：任何配置写入都走真实 `load_config()`；严格 schema、未知键报错；
- **Web 只读优先**：引擎内嵌服务无控制端点；控制类操作只存在于控制台，
  且 LIVE 启动需显式确认；密钥永不回传前端。

## 5. 测试与验证状态

- `python3 -m pytest tests/` → **42 passed**（新增 test_state / test_web /
  test_console / test_console_api 四个文件；test_dashboard 的历史失败已修）。
- 端到端冒烟（在本会话服务器上，真实行情）：
  - 引擎 `--web` 启动 → 连上 HL/RH 官方 ws → `/api/state` 返回
    `status: recording` 实时快照；
  - 控制台建 profile → 启动真实引擎 worker → 代理快照/WS 桥接 →
    分析（1683 分钟真实数据）→ 回测 → 历史序列 → 停止/重启循环；
  - worker SIGTERM 优雅退出 0.1–0.3s（修复过 aiohttp cleanup 挂死），
    exit_code 正确记录；
  - token 鉴权矩阵（无/对/错）全部符合预期；
  - `tools/analyze.py`、`tools/backtest.py` 输出与重构前一致。
- **未验证**：浏览器里的页面视觉效果/交互（本环境无浏览器）。这是换机后
  第一件要做的事：打开六个页签过一遍。

## 6. 安全模型要点

- 控制台回环绑定免鉴权（与"能读 .env 的人本来就能开控制台"的信任模型一致）；
  非回环绑定强制 token（`--token` 或自动生成）。
- 密钥只在后端：API 永不返回明文（只回"已设置 + 尾 4 位"）；写入审计日志
  不含值；`.env` 权限 0600。
- LIVE 启动 = 二次确认（输入品种名）+ 审计日志 + 密钥完备性前置检查。
- `.env`、`config.yaml`、`logs/`、`logs/console-token` 均已 gitignore；
  `profiles/` 不含密钥（是否提交由你决定，当前 README.txt 已入库）。

## 7. 已知问题 / 注意事项

1. **rich ≥ 15**：`force_terminal` 下忽略 width 参数（测试里已用
   `force_terminal=False` 规避）；TUI 在窄终端会省略号截断长词，属正常降级。
2. **`--hours` 过滤**：分析/历史接口按 `minute_ts` 过滤，机器时钟明显不准
   时会"看不到数据"（沙箱曾复现）；传 `hours=0` 可看全部。
3. **端口占用**：控制台默认 8788，worker 从 8801 起找空闲端口；冲突时换
   `--port` / `--base-port`。
4. **worker 日志**：控制台通过 `--log-stdout` 让引擎同时输出到 stdout，
   便于页面查看；直接看文件则去 profile 里配置的 `logging.file`。
5. 前端 token 存 sessionStorage —— 换浏览器标签需重带 `?token=`。

## 8. 待办 / 待决策（P5 候选，均未实现）

| 事项 | 说明 | 状态 |
|---|---|---|
| 密钥连通性测试 | 控制台"验证"按钮发只读账户查询（需真实密钥才能开发验证） | 待用户确认后做 |
| 阈值热更新 | 免重启改 thresholds（走同一校验路径） | P5 可选 |
| 告警推送 | HALT/DOWN/margin → Telegram/webhook | P5 可选 |
| LIVE 二次确认形态 | 当前为"输入品种名"，可改长按式 | 待用户偏好 |
| 多 worker 聚合大屏 | 总览页已具备基础，可加强 | P5 可选 |

## 9. 环境要求

- Python 3.11+（开发与验证均在 3.11.6）；依赖见 requirements.txt
  （控制台零新增依赖）与 requirements-live.txt（实盘签名 SDK）。
- 现代浏览器（ES modules / WebSocket / ResizeObserver）。

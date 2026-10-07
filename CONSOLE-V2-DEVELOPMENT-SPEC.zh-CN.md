# 策略控制台 V2：需求与开发实施说明

版本：1.0  
编写日期：2026-09-30  
核对代码基线：fbc98271201b545eb32cf5f37887805fbc40510f  
项目：entropy-arb；本地目录 /Users/alex/Work/entropy。  
使用者：接手开发的 AI Agent 与验收者。本文要求开发实现，不表示这些能力已经完成。

## 0. 给接手 Agent 的执行指令

先读第 1～4 章，再按第 16 章逐个完成任务。每个任务完成后记录修改文件、验证方式、剩余缺口。不要同时重写交易引擎、网站和交易所适配器。

交付目标：用户能判断策略的收益与证据，追到收益原因，制定和评估调整，同时完整使用旧版密钥接入、配置、启动停止、重启、诊断、平仓、实时状态、日志和数据研究。

1. 使用现有 Python / aiohttp 后端和原生 ES modules 前端；不引入 React、Next.js、Vite 或另一套服务。
2. 原型不是生产实现：它无 API 连接，禁用密钥输入，动作只演示流程。
3. 不把“待核对”实现成 0，不把旧 edge / 会话 MTM 改名冒充真实净收益。
4. 不硬编码历史策略、交易所组合、收益或建议到生产页面。历史样本只进入 fixture / 明确标注的演示模式。
5. 不删旧功能简化任务。新版切换前必须完成迁移清单。
6. 不因改网站而调整手续费、报价、对冲、规模、nonce、风控、签名或下单行为。涉及交易行为的修复单独提交、单独验收。
7. 本文是开发说明，不能据此自动部署、启停实盘、平仓、修改生产密钥或策略。测试用临时目录、假凭据和 stub。
8. 缺历史事实则显示未知。不可用插值、盘口推断或人工填值“补成已核对”。
9. 未完成全部阶段时，报告实际完成阶段。静态页面和语法通过不等于完整交付。

关联资料：

- CONSOLE-V2-PLAN.zh-CN.md：产品规划。
- DEVOPS.zh-CN.md：开发运维约定；行为改变后同步更新。
- MAKER-DESIGN.md、BACKPACK-PLAN.md：专题资料。
- 原型本地位置：/Users/alex/.codex/visualizations/2026/09/25/01a0d965-675e-7ae1-88f1-06a90a668f13/strategy-workbench.html。该路径不是仓库依赖；不存在时以本文为准。

历史文档 / 源码注释中“锁定边际就是已实现利润”“结构保证盈利”等说法不作为新版盈利保证，采用第 6 章口径。

## 1. 产品目标与范围

### 1.1 用户问题

1. 选定期间真实净赚多少，核对过吗？
2. 哪个策略贡献收益，哪些尚不能判断？
3. 盈亏来自哪个方向、交易周期、费用、资金费、库存变化或执行问题？
4. 证据支持继续观察、调整验证、处理风险还是补齐数据？
5. 如何接入账户、修改配置、启停实例、查异常和处理残仓？

网站提高判断和操作质量，不承诺盈利，不自动扩大资金。

### 1.2 两种策略分别解释

| 类型 | 代码判定 | 用户解释 | 证据 |
|---|---|---|---|
| 双边吃单基差 | maker.enabled != true | 两腿持仓，等价差回归后退出 | 完整开平仓、分腿仓位、费用、资金费、估值 |
| maker 后对冲 | maker.enabled == true | 挂单成交后完成 taker 对冲，库存之后仍需退出 | maker 成交、对冲分配、事件盘口、费用与库存周期 |

maker 腿通常是配置的 hedge 交易所（当前 Katana / Backpack），taker 对冲在 base。不能把字段 hedge 无条件称为 maker 策略的实际 taker 对冲腿。额外显示 maker_venue、taker_hedge_venue。

### 1.3 阶段

- A：完整运维与页面框架；收益区域可明确缺失。
- B：稳定策略身份、历史记录与可信账本；来源不足继续未知。
- C：收益解释、成交复盘、可追溯建议。
- D：调整草稿、配置版本、启用 / 回退、比较。

“暂停新增风险且继续对冲 / 减仓”是独立引擎能力，不能用 Stop 代替。未实现前不提供可执行按钮；后续独立任务与测试。

## 2. 代码地图与技术决策

### 2.1 阅读入口

| 文件 | 当前作用 | 本次处理 |
|---|---|---|
| console.py | token、adopt、服务生命周期 | 保持入口 / 参数兼容 |
| entropy_arb/console/server.py | HTTP / WS、鉴权、worker / ops | 增量注册新接口，保留原接口 |
| entropy_arb/console/supervisor.py | 启停、重启、adopt、日志 | 增加持久 run 映射 |
| entropy_arb/console/profiles.py | YAML + JSON sidecar、CRUD、校验 | 保留字段，增加版本 / 身份 |
| entropy_arb/console/secrets.py | 凭据校验、掩码、更新 | 复用，不返回原值 |
| entropy_arb/console/ops.py | 诊断、挂撤单、账户市场平仓 | 准确作用范围；只读预检 |
| entropy_arb/console/venues.py | running 聚合、旧收益估计 | 保持兼容，不用作新账本 |
| entropy_arb/console/analytics.py | 统计、回测、分钟历史 | 复用；新增绝对区间过滤 |
| entropy_arb/config.py | schema、venue 枚举、凭据解析 | 字段 / 枚举权威来源 |
| entropy_arb/engine.py、maker.py | 策略、成交、maker 事件、CSV | 独立任务增加采集，不改变决策 |
| entropy_arb/state.py | 只读快照 | 保留结构，增量字段 |
| entropy_arb/recorder.py、analysis.py | 行情采集、统计、回测 | 行情不能替代成交 |
| entropy_arb/autoband.py、tools/auto_band.py | 自动写回 band | 版本记录必须涵盖此写入 |
| entropy_arb/webui/console.html | 框架、tab、shell | 保留旧入口，另建 V2 |
| entropy_arb/webui/api.js | 鉴权 fetch、WS、轮询 | V2 复用 |
| webui 下 runs/secrets/profiles/analyze/history/overview/venues.js | 原有页面 | 先复用，再逐页调整 |
| entropy_arb/webui/engine-view.js | 实时引擎组件 | 复用到实时运行 |
| entropy_arb/webui/charts.js、fmt.js、i18n.js、yaml-lite.js | 图表、格式、双语、YAML | 保留能力，正确显示 null |
| deploy/entropy-console.service | 服务配置 | 保持 KillMode=process |
| tests/ | 离线验证 | 复用临时目录 / stub 模式 |

### 2.2 实现约束

1. 新入口 entropy_arb/webui/console-v2.html，模块放 webui/v2/；旧 console.html 保持可用。
2. 新后端建议拆 strategies.py、ledger.py、performance.py、experiments.py；不全塞进 server.py。
3. SQLite 用标准库，默认 data/console-v2.sqlite3，加入 .gitignore。阶段 A 就建立 storage、schema migrations、基础 run / config_versions / operations / audit 记录，供运维使用；阶段 B 扩展完整策略关联和账本。只有 console 写数据库；worker 用每个 run 独立的追加事件文件。
4. 金额 / 价格 / 数量计算用 Decimal，新 API 返回十进制字符串。新 HTTP 区间 / as_of 用 ISO 8601 UTC 字符串；原始事件时间用 Unix 秒数值，耗时字段明确 ms / sec。禁止 NaN / Infinity。
5. 保持无构建部署，静态资源走 /static/，不依赖外部 CDN。
6. 历史解析 / 计算放 executor 或后台任务；刷新不全量扫描。
7. 页面模块 mount(container, context) 返回 refresh / destroy；离开页面关闭轮询、WS、ResizeObserver 与读请求。
8. 公共状态仅含页面、strategy_id、时间、timezone、profile / run 选择；不存凭据输入。

## 3. 迁移清单与现有行为

### 3.1 必须迁移

| 原入口 | 新入口 | 保留能力 |
|---|---|---|
| Overview | 详情 → 实时运行；运行管理 | 盘口、数据龄、信号、订单、仓位、maker 报价、事件 |
| Venues | 账户与风险 | 权益、可用保证金、仓位、相关策略 |
| Runs | 运行管理 | record / live 启动、停止、重启、删除、日志 |
| flatten | 运行管理 / 账户与风险 | symbol 确认、先停、逐腿结果、残仓 |
| Profiles | 策略配置 | 新建、编辑、删除、校验、完整 YAML、可视化 |
| Secrets | 交易所接入 | 全部凭据、替换 / 删除、格式错误 |
| diagnostics | 接入 / 运行详情 | venue、symbol、role、dex、可选订单路径、逐阶段结果 |
| Analyze | 数据研究 | profile / 时间 / 费用、统计、回测、阈值填回 |
| History | 数据研究 → 历史行情 | 分钟序列、时间筛选、缺口 |
| 全局 | 全局 | token、中文 / 英文、API / WS 状态 |

“保留”必须可调用并展示结果，不是同名占位按钮。

### 3.2 不能误读的行为

- Supervisor registry 在内存，w1 等会变化 / 重用。历史不能依靠此 id。
- restart 返回新 worker，前端更新目标；停止后的 restart / flatten 后端可以执行，V2 不沿用旧 UI 的错误禁用条件。
- Stop 不自动平账户仓位，不等于暂停新增风险。
- run_flatten 平每腿 fetch_position() 的账户市场仓位，不隔离其他策略 / 人工仓位。
- ops 返回 HTTP 200 也可能 ok:false，网络成功不等于操作成功。
- secrets.status().venues 大体代表字段非空，不是鉴权成功；还要检查 keys[key].valid。
- realized_today 的 maker edge 不作新版已核对净收益。
- /api/venues 只聚合 running worker，按交易所 max 权益，不能证明 account_id 去重。
- 旧日界线为服务器本地午夜；V2 显式 timezone。
- thresholds 三项约 60 秒热加载；其他通常重启。auto-band 可再次写阈值。
- KillMode=process 与 adopt 可接管幸存进程；但 graceful console shutdown 调 supervisor.shutdown() 停 worker。不能承诺任意重启都不影响 worker，需验证 PID。

## 4. 页面结构与公共交互

### 4.1 导航

默认“策略工作台”，桌面左导航，窄屏换行：

- 策略判断：总览、复盘、调整与验证、账户与风险。
- 运维与接入：运行管理、交易所接入 / API Key、策略配置、数据研究。

每日复盘作为总览视图即可，不要求开发两套平行网站。

### 4.2 时间与路由

收益页共用今天、昨天、近 7 天、自定义；默认 Asia/Shanghai。自定义包含结束日，转换为 UTC [start,end)。昨天为完整当地自然日；近 7 天为今天前第 6 天零点至当前，显示准确起止和“本日未结束”。

运维显示当前 as_of，不因历史筛选对历史实例执行当前动作。操作弹窗明确“当前操作”。

深链接保留 strategy_id、时间；日志附 run_id / order_id。建议：

~~~text
#/strategies
#/strategies/<id>?view=source|executions|next|runtime
#/experiments
#/accounts
#/runs
#/connections
#/profiles
#/research
~~~

### 4.3 状态与交互

每区区分加载、真实空结果、无数据、部分数据、请求失败、过期缓存；展示最近更新时间与重试。

401 表示授权失效，停止自动写入，沿用 token 机制。503 worker 不可达不等于停止 / 零仓位。404 无行情与对象不存在分别解释。

用 AbortController / request sequence 防迟到请求覆盖新策略 / 日期。轮询不覆盖正在编辑的凭据 / 配置；离开脏表单有保留 / 放弃提示。一次操作提交后锁按钮。

### 4.4 视觉要求

正文 14～16px、辅助不小于 12px；支持 320 / 768 / 1280px。图表有单位、区间、来源、数值详情；颜色配文字。货币、bp、ms、秒、base 数量明确。

收益曲线仅画连续可靠估值，缺口断线。区间累计第一点的 0 是累计基准，不是零资产。归因图缺组件不能硬凑完整瀑布；maker 批次边际另图显示。

建议先打开证据 / 草稿 / 预检，不直接交易。

## 5. 逐页需求

### 5.1 总览

顶部三项：

1. 期间净收益：所有纳入策略都有可合并已核对账本才显示总值，否则“待核对”；已核对部分合计另标覆盖策略。
2. 资金 / 风险：account_id 去重，身份或估值缺失为未知；部分覆盖独立标注。
3. 待处理：真实敞口、执行问题、数据缺口，点击到证据 / 处置。

表列：名称、symbol、类型、两腿及 maker 角色、期间净收益、证据、运行状态、原因、下一步。同币种同口径收益才排序，未知最后；停止 / 报错 / 历史策略保留。缺资金时间序列时不显示资金利用率。

### 5.2 策略复盘

首屏：身份、角色、期间、净收益 / 待核对、证据与缺失、事实结论、独立运行状态。

- 收益来源：毛已实现、浮盈变化、资金费、实际费用、其他成本，方向 / 周期贡献，未归因项。
- 成交复盘：分页周期 / batch，订单、成交、价量、实际费用、分配和时间线。
- 下一步：理由、支持记录、缺失、验证目标。
- 实时运行：复用 engine-view，明确会话 MTM / 入场 edge 与期间净收益不同。

快捷入口带当前 profile、真实两腿、run；多个 run 明确选择，不默认写最新 live。

maker 展示报价时基准、成交时基准、实际对冲价及时间。缺成交时盘口则分摊 null，分钟 OHLC / 中间价 / 成交后 1s 不能替代。

taker 按数量匹配开平仓，展示未退出库存；双腿完成不等于整个周期完成。

### 5.3 交易所接入 / API Key

| 展示名 | 接入语义 | 全部字段 |
|---|---|---|
| Entropy（HL） | base=hl，结合 dex | HL_PRIVATE_KEY；可选 HL_ACCOUNT_ADDRESS |
| tradeXYZ | hedge=tradexyz | 可选 HL_PRIVATE_KEY_XYZ、HL_ACCOUNT_ADDRESS_XYZ；未覆盖共享 HL |
| Lighter 主网 | lighter | LIGHTER_ACCOUNT_INDEX、LIGHTER_API_KEY_INDEX、LIGHTER_API_PRIVATE_KEY |
| Lighter RH | lighter-rh | 当前共用上述默认三项，不是另一份同名独立存储 |
| Lighter 基础腿覆盖 | lighter-base 凭据检查 | LIGHTER_BASE_ACCOUNT_INDEX、LIGHTER_BASE_API_KEY_INDEX、LIGHTER_BASE_API_PRIVATE_KEY |
| Lighter 对冲腿覆盖 | lighter-hedge 凭据检查 | LIGHTER_HEDGE_ACCOUNT_INDEX、LIGHTER_HEDGE_API_KEY_INDEX、LIGHTER_HEDGE_API_PRIVATE_KEY |
| Katana | katana | KATANA_API_KEY、KATANA_API_SECRET、KATANA_PRIVATE_KEY；可选 KATANA_WALLET |
| Backpack | backpack | BACKPACK_API_KEY、BACKPACK_API_SECRET |

BASE / HEDGE 是交易腿，不能当主网 / RH。lighter_creds() 按字段取覆盖再取默认；status 对覆盖三项“任一已填则要求完整”。不自行改变规则；不完整则提示补齐或明确删除覆盖。

tradeXYZ completeness 可能仍依赖共享 HL，不能用独立字段存在跳过现有启动检查。展示真实来源，兼容修复另有测试。

字段状态：未设、尾号、格式无效。连接状态：未诊断、检查中、阶段通过、失败、过期。诊断缓存按部署 + role + dex + 市场 + 凭据修订键控，保存后失效。

**密钥提交必须区分保留与删除：**

~~~js
const updates = {};
for (const [key, input] of fields) {
  const value = input.value.trim();
  if (value !== "") updates[key] = value; // 空白：省略，保留
}
// 只有明确选择并确认删除的字段才 updates[key] = ""。
// updates 没变更时不调用保存。
~~~

POST /api/secrets 整批校验，一个错误整批不写。显示 errors 对应字段；成功清空输入，再读掩码。password 输入不持久化到 URL / localStorage / sessionStorage / 日志 / 实验 / 分析记录；既有 console token 的 sessionStorage 机制保留。

保存前列受影响实例，保存不自动重启；当前进程通常未加载新密钥。明确删除单字段 / 整组，不用空格作为操作语义。

诊断可选部署、role、symbol、dex；逐行显示 steps。默认 order_path:false。当前订单路径测试是真实 post-only 下单再按 id 撤单，不承诺绝无成交。撤单失败保留订单供检查，不自动全市场撤单。

### 5.4 运行管理

列：run_id、当前 worker id、profile、市场、base / hedge、record / live、进程状态、引擎状态、启动时间、版本、残仓可用性、异常。

worker running 可同时引擎 halted；两状态不合成“正常”。

启动选 profile / symbol / base / hedge / mode。record 文案“仅记录行情，不模拟成交”；live 输入 symbol，前后端校验，显示真实账户。

重复点击不重复提交，后端也校验重复 / 共享账户市场冲突。

Stop 后再次获取进程状态，不声称仓位归零。停止实例也可重启、读历史和处理残仓。

restart 显示配置 / 实盘模式；HALT / EXPOSED 展示原因，明确恢复操作。重启生成新 run，旧 run 留存，不因页面刷新自动恢复。

delete 只移除停止的运行列表记录，保留账本 / 日志 / 策略 / 版本。日志保留 tail 与事件，新增历史分页；断线明示。

### 5.5 账户与风险

以真实 account_id + deployment 去重。显示“账户仓位”和“策略归因仓位”，不重复累计。

停机残仓仍展示；不可达显示最后时间，不用旧快照声称已平。人工交易、充值提现、未归因费用等单列，不默默按比例分摊。

部署和抵押币可见；缺可靠汇率时原币展示，不默认 USDG / USDC = USD。

### 5.6 平仓预检与执行

弹窗明确“关闭这两个账户在对应市场的现有仓位”，不能无条件称只平当前策略。

新增只读预检读取账户、市场、signed position、挂单 / 数据龄、相关实例、配置 / 凭据修订。不是从会话虚拟 position 生成数量。

同账户同市场有其他 running 实例则后端拒绝并列冲突；用户自行处置，不自动停其他策略。身份 / 范围无法确定时限制执行，不能显示无冲突。

执行流程：

1. 校验预检引用和账户 / 配置版本，获取操作锁。
2. 停目标并确认退出；停止失败不发平仓。
3. 两腿 fresh feed 就绪后 reduce-only IOC，不改成普通开仓订单。
4. 每腿 flat / remaining / 错误 / 日志；部分成功与超时明确。
5. 超时 / 断网先查操作状态 / 账户，禁止自动重试。

预检和持久操作状态是新增能力（第 10.4 节）。旧客户端也走后端冲突与操作锁，兼容不等于绕过校验。

### 5.7 策略配置

可视化覆盖旧表单全部字段；高级 YAML 支持完整 schema。未在表单暴露的合法字段保持，不因重新生成 YAML 丢失。

组：thresholds、entropy（dex / symbol 别名）、hedge（symbol 别名）、sizing、inventory、execution、recorder、logging、auto_band、maker、web。具体类型以 config.py 为准。

保存：dirty → 服务端 validate → 差异 / 生效方式 → 写入 → 读最终版本。validate HTTP 200 的 ok:false 仍失败。

保存带 expected_version，后端实际检查；运行 profile 删除 409。thresholds 显示“待热加载”，snapshot 确认才显示生效；其他显示“已保存，需重启”。auto_band 开启时告知可能再写阈值。

### 5.8 数据研究

保留统计、回测、分钟历史、阈值填回编辑器。填回不是保存 / 启用 live。

旧 hours 是相对窗口，日期 picker 需要后端新增 start/end；未实现不能伪装支持绝对日期。

保留费用、cap、slice、edge_mode、scale；说明模型范围。band 回测不声称已模拟 maker 队列、成交选择、真实延迟和所有资金费。

### 5.9 调整与验证

草稿包含问题、证据、假设、差异、观察期、样本目标、净收益 / 执行 / 敞口条件与代价。启用 / 回退需实际版本校验，展示生效方式，不自动重启实盘。

保存配置不直接进入“观察中”，需生效证据。3 天 / 100 周期可作可调研究目标，不代表显著性或盈利保证。净收益未完整时不推荐放大。

## 6. 收益计算：不可更改的口径

### 6.1 期间净收益

~~~text
期间净收益 =
  期间毛已实现盈亏
  + 期末未实现盈亏 - 期初未实现盈亏
  + 期间资金费净收入
  - 实际交易费用
  - 其他实际成本
~~~

毛已实现不含手续费 / 资金费。交易所提供的 realized 若已净扣费，先规范化到毛值，记录源字段语义，不能再重复扣费。rebate 可作为负交易费用；资金费收入为正、支出为负；费率与配置成本不能替代实际扣费。

执行滑点已体现在成交价格中。滑点 / 逆向选择图仅解释结果，不能第二次从净收益扣除。maker.costs_bps 用于报价 / 成本假设，不能用其代替实际费用。

期间包括边界前已有库存。需要读取 start 之前的开仓历史或可核对的期初持仓成本，不是只加载当日 CSV 做 FIFO。

公式用于当前支持的线性永续合约，数量 / 合约乘数 / 报价币种需由适配器明确。不能把股票、逆向合约、不同合约单位都套 qty × price。品种别名不证明两个合约单位一致。

### 6.2 四类数字分开

| 名称 | 含义 | 可当净收益吗 |
|---|---|---|
| 期间净收益 | 本节公式，完整来源及对账 | 是，仅已核对 |
| 会话 MTM | 当前引擎会话的现金 / 库存估值 | 否，不跨重启，也未必完整含资金费 |
| 成交价格边际 | maker 与对冲实际价差，或 taker 入场边际 | 否 |
| 预期边际 / 回测 | 报价 / 模型假设 | 否 |

账户 equity_delta 是账户级结果，扣除已知转入转出后可作对账，不直接等于某条策略盈利。

### 6.3 核对状态与样本状态分开

reconciliation_status：

- reconciled：数据完整、边界估值完整、归因成立、对账通过。
- estimated：有明确假设 / 估算方法的参考结果，放独立 estimated_net，不占正式净收益。
- incomplete：缺成交、费、资金费、边界估值、身份或归因。
- no_data：没有可用记录。

sample_status：

- insufficient、observation、criteria_met。由明确研究目标判断，不等于策略有效 / 无效或统计显著。

晋级 reconciled 至少满足：

1. 实际成交与唯一标识，数量一致；各腿历史完整且期初成本可追溯。
2. 手续费 / 返佣 / 资金费源及覆盖区间明确；零费用是查得为零，不是缺记录。
3. 起止头寸、成本、估值、抵押币转换有来源和时间。
4. 订单 / 成交 / run / strategy / account 关联明确；无影响该策略收益的待归因事件。
5. 账户持仓与现金对账通过，有 reconciliation run id、容差和残差记录。

不满足就不能用绿色“已核对”。已核对盈利一天也不证明可放大。

### 6.4 账户共享与归因

账户事实以 deployment + account + subaccount + instrument 分区。策略归因使用订单映射，不按 symbol、profile 名或资金占比分配。

多策略 / 人工共用市场会相互净掉仓位；维护策略虚拟子账本，同时对账账户实际净仓位。不能把同账户 fetch_position() 复制为每条策略仓位。

资金费只有账户市场总额时，缺策略库存时间线不能分配。后续采用明确的时间点仓位分摊规则也必须有 policy_version 和时间点证据；人工 / 未归因部分不能消失。

独立策略数据完整但同账户存在无法解释差异时，记录此差异是否影响该策略。不能因账户总金额相同就自动给全部策略对账通过。

### 6.5 金额、估值与时间

保存 quote_currency、fee_currency、funding_currency、结算币、合约乘数和转换来源。展示原币与报告币 USD；转换缺失则报告币金额 null。

boundary valuation 记录时间、价格、价格类型、来源、数据龄、汇率。期初 / 期末报价必须符合预设新鲜度规则；不拿当前盘口估历史浮盈。

UTC 存储，用 zoneinfo 转当地边界。查询统一 start <= ts < end；结束零点不能重复计入前一天。timestamp 以来源说明，不混淆交易所 event time、接收 time、CSV 写入 time。

数量匹配按 Decimal 和市场量步长；不以浮点误差制造无限小库存。展示可四舍五入，计算不按 UI 精度提前舍入。

### 6.6 图表归因规则

收益组成可相加到净收益，缺少任何必要组件时不画完整合计。执行归因（预期到实际差价）与收益组成不是同一个可相加集合。

maker 的数量 q：

~~~text
卖 maker / 买 hedge 的毛价格边际 = q × (maker_sell_px - hedge_buy_px)
买 maker / 卖 hedge 的毛价格边际 = q × (hedge_sell_px - maker_buy_px)
~~~

这仅适用于已核对相同 base 单位的匹配数量。不要用 ratio_bps × 任意价格得到与上述不一致的美元边际。

盘口归因需同样数量的可执行 VWAP；只有 top-of-book 时标明参考。报价到成交时变化、成交到对冲时变化分别说明，不把 spread / 预设 slippage 再扣作实际费用。

### 6.7 分腿库存计算步骤

标准化后的每个成交是实际增量，按 event_ts + 来源序列 + event_id 稳定处理。唯一标识先去重；来源时间不能确定的并列成交保留排序方法与不确定性。

1. 对每个 strategy/account/instrument 保持 FIFO long / short lots。买入先关闭最早 short，卖出先关闭最早 long；余量才建立新 lot。
2. 关闭 long 的毛 realized = matched_qty × multiplier × (close_px - open_px)；关闭 short = matched_qty × multiplier × (open_px - close_px)。
3. 部分关闭只减少对应 lot 的 remaining；跨过零仓位时分成关闭旧 lot 与新开反向 lot。不能把整笔都作关闭。
4. 多币成本先在原币保存；转换必须来自事件对应时间。手续费单独记在实际发生时间，不混入 gross lot 成本后又在公式扣。
5. 边界浮盈：long 为 qty × multiplier × (mark - open_cost)，short 为 qty × multiplier × (open_cost - mark)，两腿与所有剩余 lots 合计。
6. 在 start/end 均计算库存与浮盈；gross_realized、funding、fee 仅取 [start,end) 的事件。期初队列要包含以前的合法开仓 / 调整。
7. 来源只提供“已扣费 realized”时必须先按准确源语义还原毛值；无法还原用 incomplete，不猜 fee。
8. 平仓事件按真实成交计账，不以订单 submitted / response status=success 作为成交。平仓请求超时，账本等待成交来源。

实际账户仓位与策略 lots 的合计核对；差值进入未归因，禁止插入无来源的盈利 / 成本事件使差值消失。独立账户 FIFO 方法并不自动适用于缺订单归属的共享账户。

## 7. 策略身份、存储与历史迁移

### 7.1 身份

strategy_id 是持久 UUID，与 worker id、profile 文件名、symbol、参数 hash 无关。

同一策略调参数、重启、停机不变 id；换交易类型、实际市场 / 合约、部署、账户绑定构成新的策略身份，保留 parent_strategy_id 关联。profile 重命名不改变策略身份。

启动实际覆盖 sidecar 的 symbol / base / hedge 时，用最终启动选择解析策略，不能只把 profile 的 id 塞给不同组合。一个 profile 可关联多个明确策略；启动遇到不匹配创建新身份，不能静默串历史。

run_id 是一次真实进程生命周期的 UUID，restart 新建。console adopt 存活进程应恢复同一 run，不创建重复历史；映射依据已持久 run + pid / 进程启动时间 / 命令身份，不能仅靠 pid。无法恢复时建 provisional run 并标缺口。

record run 可关联同策略研究，但 mode=record 永不进入真实成交 / 净收益统计。

阶段 A 已持久 run_id，但尚未完成策略映射时 strategy_id 可为 null / provisional；阶段 B 迁移后补显式关联。这个缺口必须可见，不能为赶进度仅按 symbol 合并。操作及配置版本存储不得等到阶段 B 才建立。

### 7.2 SQLite 最小模型

所有表含 schema_version 或版本化迁移；外键与事务启用，索引包含 strategy / 时间、account / market / 时间。

| 表 | 必要字段 / 用途 |
|---|---|
| strategies | id、name、symbol、type、base/hedge deployment 与市场、账户绑定、parent_id、created_at、archived_at |
| profile_links | profile 名、strategy_id、最终启动身份 |
| runs | run_id、strategy_id、worker_id、pid/start_time、mode、start/end、状态、初始 config_version、事件路径 |
| config_versions | id、profile、完整无秘密 YAML / sidecar 快照、内容 hash、来源 manual/autoband/import、时间、changed_by、parent |
| config_applications | run_id、config_version、requested/effective_at、生效证据；覆盖热加载 |
| accounts | opaque id、venue/deployment、公开账户身份的受控映射、抵押币、identity_status |
| normalized_events | event_id、dedupe_key、event_type、strategy/run/account、时间、version、标准化 payload、source_ref |
| execution_links | maker_fill / hedge_fill / allocation_qty、关系、batch_id、状态；支持一对多 / 多对一 |
| inventory_lots | strategy/account/market/side、open fill、数量、成本、剩余数量、方法版本 |
| valuations | 边界 / 时序估值、position、mark、汇率、来源、时间 |
| reconciliations | scope、interval、规则版本、状态、缺口、容差、残差、source_refs |
| import_sources | 路径、文件身份、offset、header_version、hash、异常行、导入批次 |
| experiments | 草稿 / 状态 / 目标 / 差异 / 版本引用；见第 12 章 |
| operations | operation_id、目标、类型、request_id、状态、开始结束、逐腿结果、错误 |
| audit_events | 操作与配置变化；不含秘密 |

配置版本基于保存的完整规范化内容，不能只 hash 三个阈值。配置变化未实际热加载 / 重启生效时，不标该 run 已采用新版本。

策略归档不删账本。DELETE worker 只隐藏会话列表，物理历史仍可查询。保留和备份策略与部署手册一致。

### 7.3 历史发现与导入

发现来源：现存 profile + sidecar、已记录 runs、各 profile 明确指向的 CSV / 日志及轮转档案。不要仅扫 running；不要凭文件名猜 maker 角色和账户。

CSV importer 流程：

1. 解析已知 header 版本，记录来源与时间语义。
2. 用显式映射连接 strategy / run；未知归属保留 unresolved。
3. 实际成交 id 可用时唯一去重；只有旧 CSV 的稳定文件身份 / 行 offset 去重是导入幂等，不等于证明交易去重。
4. 尾部半行等待下轮；坏行记录原因 / 数量 / 可读 source_ref，不无声跳过。
5. 重跑、轮转、重叠文件可幂等；无法判断重复则标不确定，不能删除疑似真实成交。
6. 输出 coverage 与缺口，不能靠字段名填成已核对。

旧 taker CSV 有方向、fill 数量、edge 等，但缺完整成交价、订单 id、实际费用和边界；旧 maker CSV 是 hedge batch，可能缺 maker_px、n_fills 不可信。适合作部分执行证据，不足以直接做完整账本。

历史缺数据允许从交易所只读成交 / 费用 / 资金费 / 仓位历史接口补齐。只有现有 API 实际支持且有完整分页与覆盖证明才接入；未支持的适配器明确 unsupported。不通过猜测补齐。

## 8. 新成交采集与已有问题

### 8.1 事件模型

追加事件文件建议 logs/events/<run_id>.jsonl，旧 CSV 同时保留。新格式至少：

~~~json
{
  "schema_version": 1,
  "event_id": "evt-example",
  "event_type": "fill",
  "strategy_id": "str-example",
  "run_id": "run-example",
  "account_id": "acct-example",
  "venue": "katana",
  "deployment": "katana",
  "instrument_id": "ZEC-example",
  "event_ts": 1790311200.0,
  "received_ts": 1790311200.1,
  "order_id": "order-example",
  "venue_fill_id": null,
  "side": "sell",
  "liquidity": "maker",
  "quantity_base": "0.10",
  "price": "1599.49",
  "fee": {"amount": null, "currency": "USDC", "source": "missing"},
  "source_ref": "event-file:offset"
}
~~~

这是形状示例，不是历史成交。event_ts 数字不是文档历史样本的时间凭证。

另有 order_submitted / quote / cancel / hedge_request / hedge_fill / funding / transfer / position_snapshot / valuation / config_applied / execution_error 等事件。

quote / maker fill / hedge request / hedge fill 关联实际 order / client id、batch 和分配。记录报价与事件时盘口、可成交深度 / 数据龄、目标数量及实际数量。缺任一字段用 null + reason，不能填 0。

真实 fill id 缺失时保留 source event 的稳定 id、累计成交 / 增量语义和去重方法；现有 FillEvent 只有订单与增量等字段，没有通用唯一 fill id。不能用 order_id 唯一化，因为一个订单有多笔部分成交。fee 默认值也须确认是否来源真实、币种和增量 / 累计语义。

多个 maker fill 对一 hedge、部分对冲、一 fill 多 hedge、相反 maker fill 被净额抵消都合法。不能用“时间接近”唯一配对，不能把净额 hedge_qty 当全部 maker 成交数量。未对冲和未归因保留。

### 8.2 采集路径约束

事件写入采用有界队列与单 writer，不在同步 fill 回调做慢数据库 / 网络操作。队列满 / 写盘失败显示采集缺口、告警事实并降级账本可信度，不改变当前下单流程。

只有 console 导入 SQLite，多 worker 不共写同一个事件文件。崩溃尾行、重复回报、乱序事件由 importer 处理。开始交易前记录 run identity 与无秘密配置版本；无法关联的已存活旧 worker 继续按旧采集并显示未升级，不为了采集重启实盘。

增加可靠成交采集前审查各适配器是否能返回真实 avg_px、手续费、成交 id；无法提供则旧估计保留在 estimate 中，不声称采集已完整。

### 8.3 已核对基线里的风险点

| 位置 | 现状 | 对新版的要求 |
|---|---|---|
| engine._on_maker_fill | cash 使用 mk.fee_bps（通常配置 taker 费），未直接按实际 fill fee 入账 | 不拿此 cash 作为审计账本；实际费用采集与交易行为修复分开 |
| engine._maker_log_batch | net=gross-maker.costs_bps | 作为日志成本假设，不当实际净利润 |
| _mk_batch_fills | 初始化 / reset，但当前 fill 回调没有递增 | 不用 n_fills 当可信成交数；修复单独验证 |
| maker batch price queue | 价格队列清空与净額聚合 / 部分对冲关联有限 | 新 execution_links 逐数量分配，旧批次缺口明示 |
| venues.realized_today | maker hedge edge 被解释为 realized | 新收益完全独立，不覆盖旧字段语义去凑结果 |
| CSV 轮转 / 路径 | 模板及旧 .old 档案可能共用 / 缺归属 | 来源可追溯、冲突明确、导入幂等 |

任何源码基线变化后重新核对；这些是风险点，不是要求本次网站开发顺带自动修改实盘策略。

## 9. 现有 API 合同（已存在）

以下按核对基线记录。旧 API 的金额仍可能是数字；新接口用字符串，不强改旧结构。

| 方法 / 路径 | 请求关键字段 | 返回 / 注意 |
|---|---|---|
| GET /api/meta | 无 | profiles_dir、env_exists、token_required、ts |
| GET /api/profiles | 无 | 数组；name/symbol/base/hedge/maker/midline_bps/upper_bps/lower_bps/recorder_csv/updated_ts/running；不可假设有 thresholds 子对象 |
| GET /api/profiles/new | symbol、hedge query | {yaml} |
| POST /api/profiles | name、yaml、symbol、hedge、base | {ok,error}；创建失败 400 |
| GET /api/profiles/{name} | name | name/yaml/symbol/base/hedge/updated_ts；不存在 404 |
| POST /api/profiles/{name} | yaml、symbol、hedge、base | {ok,error}；省略 base 保留已有 base |
| POST /api/profiles/{name}/validate | yaml、symbol、hedge、base | {ok,error}；HTTP 200 也可能校验失败 |
| DELETE /api/profiles/{name} | name | {ok}；running 拒绝 409 |
| GET /api/secrets | 无 | exists、keys{set,tail,kind,valid,error}、venues |
| POST /api/secrets | {updates:{KEY:value}} | {ok,errors,status}；空串删除，省略保留，格式错 400 |
| GET /api/workers | 无 | 当前 registry 数组，不是全部历史 |
| POST /api/workers | profile、symbol、base、hedge、mode、live 的 confirm | worker status；重复 profile 409、检查失败 400 |
| GET /api/workers/{wid}/state | wid | engine snapshot；不可达 503 |
| GET /api/workers/{wid}/ws | token 兼容 query | worker 只读事件桥 |
| GET /api/workers/{wid}/logs | tail 默认 120 | {lines:[string]}，内存 / seed tail |
| POST /api/workers/{wid}/stop | 无 body 必填 | {ok}，false 不能当成功 |
| POST /api/workers/{wid}/restart | 无 body 必填 | 新 worker status；原 run 结束 |
| DELETE /api/workers/{wid} | wid | {ok}；running 409 |
| POST /api/diagnostics | venue、symbol、role、dex、order_path | {ok,steps:[{name,ok,detail}]} |
| POST /api/flatten | wid、confirm=SYMBOL | {ok,go,legs,log,error?}，可能部分失败 |
| GET /api/venues | 无 | exchanges、strategies、total_pnl_mtm、asof；旧聚合 |
| GET /api/analyze | profile、hours、min_samples、fees_bps | 分布与建议，no_data 404 |
| POST /api/backtest | profile、hours、midline、upper、lower、fees_bps、cap_usd、slice_usd、edge_mode、scale | 当前 band 模型输出 |
| GET /api/minutes | profile、hours、min_samples、max_points | 分钟序列，max_points 上限 20000 |

启动 mode 只有 live / record；symbol trim 后 upper。base 枚举来自 BASE_VENUES，hedge 来自 HEDGE_VENUES，二者不能相同；maker hedge 来自 MAKER_VENUES。不要只根据旧 README 的“固定 Entropy 腿”写死 base。

诊断显示名与参数映射：

- Entropy：venue=hl、role=base、dex 用户所选（常见 io；空 dex 可指 HL 主 dex）。
- tradeXYZ：使用旧 UI 映射 venue=hl、role=hedge、dex=xyz，不直接把卡片 id xyz 当 API venue。
- Lighter / RH：venue=lighter / lighter-rh，role 决定覆盖凭据。
- Katana / Backpack：venue=katana / backpack。

所有新 /api/ 路由走现有 auth middleware，不能新开不鉴权的收益 / 配置 / 数据导入路由。fetch 使用 Bearer；WS 兼容现有 token query。静态 no-cache 语义保留。

## 10. 新增 API 合同（待实现）

### 10.1 公共规范

读取请求 start/end 使用 ISO 8601 UTC 带 Z，timezone 为合法 IANA 名称。缺任一则 400，不默认服务器午夜；start >= end、非法时间 / timezone 拒绝。range 上限与分页限制可配置并返回清楚错误。

列表 limit 默认 50、最多 200，opaque cursor；稳定排序 timestamp + id。执行明细 / 日志不一次返回全部。每个响应带 schema_version、as_of、period 和来源覆盖。

新错误：

~~~json
{
  "error": "config_conflict",
  "message": "配置已被其他操作修改，请重新读取差异",
  "details": {"current_version": "cfg-current"},
  "request_id": "request-example"
}
~~~

error 是稳定代码，message 是可显示说明。常见：invalid_range、unauthorized、not_found、no_data、unsupported_source、config_conflict、operation_conflict、stale_preview。错误不包含 secrets。

### 10.2 策略与收益

| 方法 / 路径 | 行为 |
|---|---|
| GET /api/strategies?start=&end=&timezone=&cursor=&limit= | 包含运行 / 停止 / 历史身份和收益状态 |
| GET /api/strategies/{id} | 身份、profiles、runs、账户绑定、角色 |
| GET /api/strategies/{id}/performance?start=&end=&timezone= | 净收益组件、曲线、边界与对账 |
| GET /api/strategies/{id}/attribution?... | 方向 / 周期 / 执行解释，区分归因种类 |
| GET /api/strategies/{id}/executions?...&cursor=&limit= | 周期 / batch 分页 |
| GET /api/strategies/{id}/executions/{execution_id} | 关联订单、成交、时间线、来源、缺失 |
| GET /api/strategies/{id}/recommendations?... | 规则建议与证据，不执行 |
| GET /api/runs/{run_id}/logs?cursor=&limit= | 持久历史日志；不以当前 worker id 代历史 |
| GET /api/accounts | 按实际账户去重、最新时间、风险和相关策略 |

性能响应最小形状：

~~~json
{
  "schema_version": 1,
  "strategy_id": "str-example",
  "as_of": "2026-09-30T02:00:00Z",
  "period": {
    "start": "2026-09-24T16:00:00Z",
    "end": "2026-09-25T16:00:00Z",
    "timezone": "Asia/Shanghai"
  },
  "currency": "USD",
  "reconciliation_status": "incomplete",
  "sample_status": "insufficient",
  "net_pnl": null,
  "estimated_net": null,
  "components": {
    "gross_realized": null,
    "unrealized_start": null,
    "unrealized_end": null,
    "funding_net": null,
    "trading_fees": null,
    "other_costs": null
  },
  "evidence_metrics": [
    {
      "code": "matched_execution_edge_after_maker_fee",
      "amount": "-6.49",
      "currency": "USD",
      "status": "estimated",
      "sample_count": 50,
      "source_refs": ["fixture:zec-20260925"],
      "method_version": "historical-analysis-v1"
    }
  ],
  "coverage": {"complete_fills": null, "unmatched_records": null},
  "missing": [
    {"code": "funding_missing", "message": "资金费未归因", "source_refs": []},
    {"code": "inventory_exit_missing", "message": "库存退出未完整匹配", "source_refs": []}
  ],
  "reconciliation": {"id": null, "residual": null, "tolerance": null},
  "series": []
}
~~~

这是历史 fixture 的 API 示例；evidence 数值不能出现在 live fallback。生产 source_ref 是可定位的记录引用，不直接暴露任意绝对服务器路径。来源读取按已有授权和路径白名单，禁止客户端指定任意文件路径。

attribution 顶层明确 kind=pnl_components / execution_edge / execution_loss；执行损耗不能与已实现组成一起求和。缺分方向真实净收益时仅展示 estimated edge。

execution 包含 type=inventory_cycle / hedge_batch、matched / remaining quantity、fills / allocations、fees、start/end、pnl_kind、status、missing。fee 未知使用 null。失败率分母必须实际请求 / 成交事件数，不是日志行数。

### 10.3 配置与接入扩展

保留旧 profile 路径，增量返回 version；V2 保存携 expected_version，不一致 409。成功返回新 version、生效方式、受影响 runs，既有客户端省略参数维持兼容但仍服务端真实校验 / 写锁。

新增 GET /api/profiles/{name}/versions；版本包含无秘密快照、来源、diff 和应用记录。auto-band 写入也用相同版本路径，见第 13 章。

新增 GET /api/connections：masked 状态、实际解析的 credential source、每部署 / role / 市场诊断历史、credential_revision、关联策略。它不新建账户密码库，也不返回 secrets。

凭据修订是内部非秘密标识；不能把私钥 hash 暴露成账户 id。账户身份由实际适配器 / 诊断解析。

### 10.4 运维预检、并发与结果

新增接口：

- POST /api/operations/flatten-preview：body={wid}；只读，返回 preview_id、过期时间、目标账户 / 市场、每腿数量、相关实例、版本、允许执行 / 原因。
- POST /api/operations/flatten：body={preview_id,confirm,request_id}；202 返回 operation_id，后台执行并持久结果。
- GET /api/operations/{id}：queued / running / succeeded / partial / failed / unknown，结果逐腿。

preview 期限默认 30 秒；最终执行重新读头寸、账户身份、冲突与配置，不盲用缓存数量。行情未就绪不下单，preview 过期重新生成。

request_id 在相同请求内幂等，服务端按账户市场 / run 锁定启动、重启、平仓与 order-path 诊断。HTTP 超时不能证明交易没执行；状态 unknown 要先核对交易所。

兼容旧 /api/flatten 的同步返回结构，但调用相同操作服务；共享冲突时 409。旧 client 没有 preview_id 时服务端自行预检，不能因客户端旧而跳过账户 / 停机 / reduce-only 校验。

平仓是逐腿账户市场动作，不提供虚假的“仅关闭虚拟子账本数量”承诺。后续真正按策略退出需独立设计。

### 10.5 实验

- GET /api/experiments?strategy_id=&cursor=&limit=。
- POST /api/experiments：创建草稿。
- GET /api/experiments/{id}、PATCH /api/experiments/{id}：读取 / 修改草稿，带 expected_version。
- POST /api/experiments/{id}/apply：写目标配置，expected_profile_version、request_id；不自动重启。
- POST /api/experiments/{id}/rollback：同样版本校验，新增回退版本，不删除历史。
- GET /api/experiments/{id}/comparison：同口径区间比较与混杂因素。

以上完整生命周期仅阶段 D 实现。草稿接口可在之前单独交付；未交付时页面显示未提供，不能点击后假装保存。PATCH 需要在现有 api.js 内增量添加 patchJSON（沿用同一 auth / errors），不能另写无鉴权请求。

### 10.6 绝对研究区间

扩展 /api/analyze、/api/minutes 和 /api/backtest 支持 start/end/timezone；两者与 hours 同时提交拒绝歧义。保留旧 hours 请求兼容。

时间筛选在分析前进行，报告实际覆盖 / 缺口。分页 / 降采样不改变计算样本，只影响展示。分析先过滤再统计，不能只把全量输出图裁切成选择日期。

## 11. 建议规则与策略有效性的表达

第一版用可解释规则，不用 AI 任意生成策略判定。每条输出 rule_id / version、reason_code、period、触发事实、source_refs、missing、next_action。

优先级：

1. 真实未对冲 / EXPOSED / 需要处理残仓 → 查看风险处置；不自动平仓。
2. 对账缺口 → 补齐数据，指向具体缺失源。
3. 可重复执行故障 → 检查订单与市场，定义执行修复验证。
4. 可归因方向亏损 → 查看方向贡献，提出假设，不直接断定策略永远无效。
5. 账本完整且满足明确研究目标 → 保持规模复盘；放大仅为可评审研究方案。

缺安全相关数据不能默认风险低；收益、样本、风险三种状态分别显示。

可用 reason_code：unhedged_exposure、ledger_gap、execution_unreliable、direction_edge_negative、regime_change_observed、observe_at_current_scale、insufficient_evidence。

禁止规则：

- running=true → 策略有效。
- entry_edge>0 / maker fill 多 / 回测好 → 盈利或加仓。
- 一天亏损 → 策略数学上不成立。
- 43 个不能对账的日志行 → 43 次失败。
- 未配置 max_signal_edge_bps 等就猜为亏损唯一原因。

收益图说明条件，不写“保证赚钱”。策略是否成立需完整真实收益、持续样本、执行容量与风险证据；网站目前主要提供这些证据及明确缺口。

## 12. 调整与验证状态机

### 12.1 字段

experiment_id、strategy_id、profile、状态、version、问题 / 假设、source_refs、from_config_version、candidate_config、参数 diff、观察起止、样本目标、指标 / 停止条件、成本与请求量代价、创建 / 修改时间、实际生效版本、comparison。

保存配置快照不含 secrets，不存密钥尾号作为实验身份。更换账户绑定不是参数实验，按新的策略身份处理。

### 12.2 状态

~~~text
draft → ready → pending_activation → observing → review_due
review_due → retained
review_due → rollback_pending → rolled_back
任一未执行草稿 → cancelled
失败 / 版本冲突：保留当前状态及 error，不能伪装下一状态
~~~

ready 要通过真实配置校验，有可读取的证据和完整目标。apply 写文件后 pending_activation；观察开始以真实 config_applied 为准。需重启时由用户在运行管理操作，实验不自动下单。

rollback 生成新配置版本；恢复旧参数不恢复历史账户余额 / 仓位，也不保证恢复旧行情。auto-band 同时变化显示差异并冲突检查，不能覆盖最新内容。

### 12.3 比较

相同币种、口径、账户范围；对比期间长度、完整周期数、方向占比、市场波动、资金占用、执行失败、对冲耗时。每个差值展示覆盖。

变更后盈利不能自动归因于改参。净收益缺失则比较对应的执行指标，结论明确“不能比较净收益”。

## 13. 并发、版本、性能与运行约束

### 13.1 配置 / 自动校准

文件保存用临时文件 + 原子 replace，保留原字段与 sidecar 元数据。profile YAML 与 sidecar / 数据库版本记录的写入失败要有恢复日志，不能成功提示后实际版本缺失。

expected_version 根据当前内容与启动元数据比较，不只看 mtime；mtime 可分辨率不足。版本命名可 cfg-UUID + content_hash。

手动和 auto-band 使用共同 profile 写锁及版本记录。修改 entropy_arb/autoband.py 的文件 patch 路径，使自动变更也记录来源和 before/after；保留算法和定时行为。Linux / macOS 可用 fcntl 文件锁，所有协作写入路径都要遵守，不只 console 自己锁。

外部未受控写入在下一次扫描识别为 external，记录新版本；不能无声假定是用户编辑。运行实际采用的 band 独立记应用事件。

### 13.2 运维并发

同一运行实例启停重启互斥；同一实际账户市场 order-path / 平仓与相关运行操作互斥。跨两腿锁按稳定排序获取避免死锁。

后端重复 live-start、flatten、restart 请求需要 request_id / 操作记录；相同请求返回相同操作结果，不再发一次。现有操作 API 扩展参数可选兼容，但后端校验始终生效。

HTTP 断开后操作可能继续，UI 提供操作记录查询；不要自动重新 POST。操作服务重启有 running 记录但不能确定交易所结果时标 unknown，执行只读核对后再允许新动作。

### 13.3 数据刷新与性能

- 活跃 worker 状态可沿用约 3 秒刷新 / WS；账户约 5 秒显示已采集快照；交易所实际读频率遵守适配器限制、后台共享采集，不每组件重复请求。
- 历史收益响应从索引 / 缓存查询；导入只处理增量，重算与请求线程分离。
- 自定义窗口超范围拒绝或异步生成，不能卡死控制台的 Stop。
- 缓存键含 strategy_id、start/end、timezone、report_currency、账本 revision、方法版本。
- 最新实时数据与历史收益不同缓存，不互相覆盖。
- 数据库故障不能破坏旧运维入口；收益区域显示故障。采集故障不能改变下单决策。
- 性能验收用至少 10 万事件 / 多策略 fixture；常规列表 / 缓存性能请求目标本地 1 秒内，耗时任务后台处理并报告状态。这是开发目标，实测记录机器与数据量。

### 13.4 鉴权与数据展示

沿用现有 token 机制和鉴权路由；不写入文档 / commit / screenshot 真 token、.env 或 PEM。输入 / errors / logs 用 textContent 或等效转义，来自 profile、市场、错误信息的文本不可直接拼可执行 HTML。

新增导入 / 来源读取只允许后端管理的路径，检查真实路径与根目录关系，不允许通过 ../ 或 symlink 任意读取系统 / secret 文件。源配置仅返回无秘密 YAML；实际秘密始终由 SecretsManager 管理。

0 索引、0 费用、0 净收益都合法数值；不要用 truthiness 判未知。未知必须显式 null；非法数值拒绝并记录原因。

## 14. 测试 fixture 与确定的预期结果

### 14.1 2026-09-25 历史回归样本

样本来源是此前分析摘要，不是完整成交账本；仅测试页面与证据分类。不能作为 live 数据源，也不能凭这些摘要构造“完整可核对交易”。

当地区间：2026-09-25 00:00 至 2026-09-26 00:00，Asia/Shanghai。UTC：2026-09-24T16:00:00Z 至 2026-09-25T16:00:00Z。

| 策略 | 正式期间净收益 | 可展示证据 | 必须避免的错误 |
|---|---|---|---|
| ZEC · Katana / Lighter RH maker | null / incomplete | 50 匹配记录，毛价格边际约 -6.10，maker 费约 0.384，边际估算约 -6.49；21 卖 maker 方向约 -7.02，29 反向约 +0.53 | 不标真实净亏 -6.49；数值约数不要求伪造精确逐笔分解 |
| HYPE · 同组合 maker | null / incomplete | 54 行 hedge 日志，43 行不能用于当前对账 | 不标 43 次实际失败，不把 43/54 作失败率 |
| ANTH · Entropy / RH taker | null / incomplete | 10 尝试、9 双腿完成、入场 edge +3.43；24/25 日 basis 中位数约 -43 / -206bp | +3.43 不当完整利润，日中位数变化不当美元亏损 |
| SNDK · Entropy / RH taker | null / incomplete | 25 日 17 次尝试、0 双腿完成；17 日 32/37 | 不标零账户盈亏，历史暂停结论不当当前状态 |

ZEC 时间线 fixture：报价时 maker 卖 1599.49，RH 可买 1598.53；约 2.5 秒后 maker 成交，实际 RH 对冲买 1606.28。成交时 RH 盘口缺失。可显示价格边际约 -42.3bp，但两段损耗分摊必须 null。

这四条的当前 live 状态一律 unknown，不能用 25 日记录伪造 30 日 running / halted。

### 14.2 合成账本算例

下列为完全合成测试，不是历史业绩。使用同币种 USD、线性单位、独立账户、真实 fixture fee，补齐来源 / 估值 / 对账。

**算例 A：maker 已对冲但没退出。**

maker 买 1 @100，hedge 卖 1 @102，费用合计 0.2；期间期初无仓，期末分别按 maker 99、hedge 104 估值，资金费已确认 0。

- 毛已实现 0，期末两腿浮盈为 -1 和 -2，总计 -3。
- 期间净收益 -3.2。
- 已匹配毛成交 edge +2，扣交易费边际 +1.8。
- 页面同时显示真实净收益 -3.2 和边际 +1.8，不能用边际覆盖浮盈亏。

**算例 B：taker 跨日退出。**

前日 long base 1 @100、short hedge 1 @105；区间起点两腿 mark 101 / 104，期初浮盈 +2。期间 base 卖 1 @103、hedge 买 1 @102，毛 realized +6；期末无仓浮盈 0，期间费用 0.5，资金费 -0.2。

- 本期间净收益 6 + 0 - 2 - 0.2 - 0.5 = 3.3。
- 不能把前日的全部 +2 浮盈再算入本日收益。
- 期初开仓历史缺失时不得产出已核对 3.3。

**算例 C：rebate / 重复回报。**

gross_realized=10，浮盈变化=0，funding_net=-2，trading_fees=-1（真实返佣），other_costs=0 → net=9。同 fill id 重复导入两次，仍为 9。

**算例 D：部分平仓与未知费用。**

仓位 2 只平 1，剩余 1 进入估值；费用未获取时 net=null，不自动假定零。恢复实际费用后重算，旧 reconciliation revision 保留。

**算例 E：共享账户。**

两个 worker 同账户各报告 equity 1000，账户总额为 1000，不是 2000。实际市场仓位 1 不复制为每策略 1。相同 account_index 在不同 deployment 不合并；不同账户同交易所不取 max。

### 14.3 最少自动测试集合

1. 密钥空白省略保留、明确空串删除、批量一错全拒、未知字段拒绝、原值不返回 / 不日志。
2. Lighter 三组覆盖与回退、部分覆盖提示、0 account / key index 正常；主网 / RH 诊断缓存独立。
3. tradeXYZ 卡片映射到正确 venue/role/dex，不能误发 xyz venue。
4. profile 保存保留 base、市场别名、auto_band、高级合法字段；YAML 校验失败不保存。
5. expected_version 冲突，手动 vs auto-band 并发不丢更新；生效状态等待真实 snapshot。
6. stop 不自动 flatten；restart 新 run、strategy 不变；停止实例可处理残仓。
7. flatten confirm、record 拒绝、共享冲突、停机失败、两腿行情不就绪、逐腿 partial、超时 unknown、幂等重复。
8. order-path 默认 false，选中才可能下单，撤单错误不是成功；诊断服务被 stub。
9. console adopt 恢复 run，worker id 重用不串账；删除列表不删历史；profile 删除不丢策略账本。
10. 时间 UTC / 上海日界线及 DST 地区，end 排他、start=end 拒绝、非法 zone 拒绝。
11. 算例 A～E、FIFO 部分数量、方向反转、手续费已净扣的规范化、跨币缺 FX、人工 / 未归因。
12. 乱序、重复、部分 fill、一 maker 多 hedge、多 maker 一 hedge、相反 fills 净额、未知 fill id。
13. 文件半行、坏行、轮转、重导、header 变化；坏行可见，不能“解析失败=零成交”。
14. 净收益 null 时总览不求总和 / 不排成零；无数据曲线为空；断档不插值。
15. 旧请求迟到不覆盖新筛选；页面切换 destroy 后不新增轮询 / WS。
16. API 401 / 404 / 409 / 503、HTTP 200 ok:false、XSS 文本、路径越界、秘密不泄露。

数值测试用明确 Decimal 结果，不依赖截图颜色或复制实现逻辑。操作测试检查 stub 实际调用次数 / 参数，不能只检查按钮存在。

## 15. 浏览器验收脚本

在临时数据目录与 stub 后端执行，不使用真实生产凭据。

### 15.1 接入与运维回归

1. 打开 V2，无 token 的受保护 API 返回 401；有测试 token 正常，切换中文 / 英文可读。
2. 逐一打开 Entropy、tradeXYZ、Lighter、RH、Katana、Backpack；完整字段可达。
3. Lighter 展开 BASE / HEDGE 覆盖，观察共享来源；主网与 RH 不伪造独立保存。
4. 输入假测试密钥保存；再空白保存，服务器已有值不删除。明确删除后才删除对应项。
5. 格式错误对应字段，输入不被轮询打断；原值没有出现在 GET、toast、日志或 URL。
6. 诊断选市场 / role / dex；默认不发订单，勾选测试才有额外确认，分阶段结果可见。
7. 新建 profile，修改未在表单出现的合法高级字段，再改表单保存；高级字段仍在。
8. 从 Analyzer 填入阈值，编辑器显示草稿未保存；保存后阈值待热加载，其他参数需重启。
9. stub 启动 record，不要求真实密钥；启动 live 没输入 symbol 被拒，正确确认才提交。
10. 运行页显示 base 和 hedge，maker 实际角色正确；worker running / engine halted 可同时显示。
11. Stop 后仍能看残仓和日志；停止后 restart 生成新 run，旧 run 可复盘。
12. 平仓预检显示账户市场范围、两腿数量、冲突；冲突拒绝，部分失败保留 remaining。
13. 请求超时后查操作记录，不自动重发。重复点击最多执行一次。
14. 删除停止实例，历史策略 / 收益仍存在；运行中的 profile / worker 删除拒绝。
15. 切旧入口，上述旧能力仍可用。新版侧错误不会破坏旧页。

### 15.2 收益理解验收

1. 加载第 14.1 节 fixture，四条净收益都为待核对，ZEC 估算与 ANTH 入场 edge 各有正确标签。
2. 总览不给四条加总利润；点击 ZEC → 亏损方向 → 时间线；缺成交时盘口有缺失提示。
3. HYPE 43/54 解释为日志可用性，SNDK 0/17 解释为双腿完成率，不误导账户净收益。
4. 切合成 A，用户能理解正 edge +1.8 仍对应净亏 -3.2；组成为可核对来源。
5. 切 B 的上海自然日，只显示本日 3.3，跨日持仓完整，重启不重置收益。
6. 切日期 / 策略并故意让旧请求慢返回，结果仍属于当前选择。
7. 账户共享 fixture 不重复累计；停机策略残仓可见。
8. 从建议创建草稿，不自动改 profile / 发单；比较明确费用 / 样本 / 方向组成。
9. 320px 下操作、表单、证据、错误不裁切，键盘能操作；1280px 图表和列表可读。

验收者不打开日志、不编辑 YAML，能解释“净赚多少 / 哪些不能确认 / 主要原因 / 下一步”。需要日志时可从对应证据进入。

## 16. 开发任务顺序与完成条件

先完成一项并验证，再进入依赖它的下一项。每项单独可审查提交；不要以半成品大改覆盖所有页面。

| 编号 / 阶段 | 修改范围 | 明确输出与完成条件 | 依赖 |
|---|---|---|---|
| V2-001 / A | 新入口、路由、样式、i18n、api 复用、基础 storage / run UUID | /console-v2 可访问；/ 仍旧页；8 入口、公共状态、错误可见；运维持久存储和 migrations 就绪 | 无 |
| V2-002 / A | secrets 复用 / connections 展示 | 全部字段、保留 / 删除、部署 / role 诊断、影响 / 生效说明；第 15.1 的 2～6 通过 | 001 |
| V2-003 / A | runs、ops、操作锁 / 预检 / 记录 | 启停重启 / 日志 / stopped 残仓；平仓账户范围、确认、冲突、partial、unknown；stub 测试通过 | 001，账户解析预检 |
| V2-004 / A | profiles、配置版本与锁、auto-band 写入 | CRUD / YAML / schema 完整，冲突不覆盖，热加载 vs 重启明确，旧接口兼容 | 001 |
| V2-005 / A | research、绝对过滤、engine-view、旧能力 | 分析 / 回测 / 历史 / 阈值草稿 / 实时组件全迁移；旧表逐项勾选 | 001，004 |
| V2-006 / B | 扩展 SQLite migrations、完整策略 / run 映射 | 补齐阶段 A 的 provisional 关联；参数调整 / restart 稳定策略身份，adopt 不重算，停止 / 删除后历史保留 | 003，004 |
| V2-007 / B | 增量导入、source_refs、异常覆盖 | 历史 CSV / 轮转识别、幂等、半行 / 坏行、未归因显式 | 006 |
| V2-008 / B | 独立事件采集、适配器只读历史补齐 | 真实 fill、费用、资金费、订单关联 / 分配；unsupported 与缺口明确；不改决策 | 006 |
| V2-009 / B | 库存子账本、估值、账户对账 | 第 14.2 算例及边界 / 币种 / 部分数量全通过；数据不全不晋级 | 007，008 |
| V2-010 / B | strategies / performance / accounts API | 新合同、分页、鉴权、revision、null 完整，账户去重；10 万事件性能记录 | 009 |
| V2-011 / C | 总览 / 详情 / 图表 | 真实 API，缺失 / estimate 分开，停止策略可见，图表不造数 | 010 |
| V2-012 / C | attribution / executions / 历史日志 | 方向 / 周期 / 数量分配与时间线，单笔追溯，缺盘口不伪拆损耗 | 008，010，011 |
| V2-013 / C | rule recommendations | 可解释规则、来源、缺失；历史4样本防误导通过 | 012 |
| V2-014 / D | 草稿 / 状态机 / 启用回退 | 版本 / 生效 / 操作幂等完整，不自动 restart，回退留痕 | 004，009，013 |
| V2-015 / D | 比较、研究目标 | 同口径 / 样本 / 市场混杂说明，数据缺失时不推盈利结论 | 014 |
| V2-016 / 发布验收 | 全链路、迁移检查、文档 | 第 17 章全部检查；交付报告真实，默认入口切换单独安排 | 全部相关任务 |

若某交易所无法补完整历史，不阻塞运维迁移，但该策略收益继续 incomplete，交付记录来源限制；不能将 V2-009 全部账本覆盖写为完成。

建议各项 commit 描述直接使用编号和行为，避免“重构所有前端”等难审查提交。分支遵守仓库约定，无特殊约定时 codex/console-v2。

### 16.1 第一项的具体起步步骤

1. 查看 git status，保留用户现有 .pem / demo profiles 等未跟踪内容，不误提交。
2. 读取本文表中的当前入口和 tests/test_console_api.py、test_console.py。
3. 新增 console-v2.html 和路由 /console-v2，保留 / 与 /static/。
4. 建导航与 mount/destroy，不写假收益；从现有 API 接 profiles / workers / masked secrets，建立运维基础存储和持久 run UUID。
5. 显示 loading/error/no_data，测试 token 和旧入口。
6. 完成 V2-001 后再做接入与运维，不先在浏览器画假盈利曲线。

### 16.2 推荐新文件

~~~text
entropy_arb/webui/console-v2.html
entropy_arb/webui/v2/
  app.js          # 路由 / 页面生命周期
  store.js        # 全局非秘密选择
  components.js   # 状态、证据标签、金额、时间
  strategies.js   # 总览
  detail.js       # 复盘 / 实时
  connections.js  # API Key 与诊断
  runs.js         # 运行 / 操作记录
  profiles.js     # 完整编辑与版本
  accounts.js
  research.js
  experiments.js
  style.css
entropy_arb/console/
  strategies.py
  ledger.py
  performance.py
  experiments.py
  operations.py   # 与现有 ops.py 分开；生命周期、锁、幂等、预检
  storage.py      # schema migrations / 事务
tests/
  test_strategy_identity.py
  test_ledger.py
  test_performance_api.py
  test_operations.py
  test_config_versions.py
  test_experiments.py
  fixtures/console_v2/
~~~

文件名可为符合当前结构的小调整，但职责与验证不删减。不新增与现有 api.js 竞争的 token 实现。

## 17. 验证、部署和回退

### 17.1 验证方式

现有测试使用 pytest，安装于开发虚拟环境；requirements.txt 不包含 pytest，不能假定环境已安装。基础依赖满足离线运维 / 账本测试，真实 signer SDK 不是 stub 测试的必要条件。

首先跑相关门：

~~~sh
python3 -m pytest tests/test_console.py tests/test_console_api.py tests/test_config.py tests/test_state.py tests/test_venues.py
~~~

涉及 engine / maker 采集再跑 test_engine.py、test_maker_engine.py、test_katana_maker.py、test_backpack_maker.py、test_maker_ui.py，并跑新测试。发布前按 DEVOPS 要求完整：

~~~sh
python3 -m pytest tests/
~~~

adopt 测试依赖 Linux /proc；macOS 不具相同行为时不能为了绿灯删除断言。记录平台限制，在 Linux 隔离环境执行此门，不使用生产实盘作为测试进程。历史文档“179 passed”不是当前必须凑出的数量，报告实际结果。

浏览器验证使用假数据接口 / 临时应用，逐项执行第 15 章。提交阶段报告包含测试命令、结果、浏览器主要场景截图及具体未通过项。文档本身不是测试通过证据。

### 17.2 数据迁移

迁移前备份 profile / sidecar / 新数据库；不将含秘密备份放 Git。迁移提供 dry-run、版本号、发现策略数、未归因记录数、冲突说明。

新 SQLite schema 使用事务迁移；失败保留原数据 / 原入口。导入原始 CSV 不改写；方法升级保留旧 reconciliation 与来源，新增 revision。

旧控制台可继续读原配置；V2 增量字段不破坏 load_config / 旧 JS。发布切换前恢复验证备份，不只确认文件存在。

### 17.3 部署条件

本文不授权自动部署。准备可审查分支 / PR、迁移报告、回退步骤；部署由实际后续用户授权安排。

正式服务器已知为 taoli.coinfetcher.xyz，checkout /root/code/entropy；这些是基线信息，部署时重新核对，不能凭文档跳过当前 git status / 服务 / 实例检查。

先用 /console-v2 并行入口，只有原功能迁移全部验收后才考虑默认 / 切换；旧入口可保留 /console-legacy，别移除故障后备。

不改 KillMode=process。操作前记录 running PIDs / run IDs，按实际 service 停止机制确定影响。静态页面变化一般只需页面重新加载；后端变更可能需 console 重启，不能随之重启全部 worker 或自动恢复 HALT。

反向代理需允许已有 WS 和操作请求 / 状态查询；异步 operation 用轮询结果，不仅增加 proxy timeout。API 鉴权、静态缓存策略保持。

### 17.4 回退

- UI 回退：入口回旧版，数据库与账本保留。
- 后端回退：确认新增 sidecar 元数据旧代码可忽略；保留事件文件，停止新 importer 不删除来源。
- schema 已升级：不能直接以旧代码写新结构；按备份 / migration 支持恢复。不要在回退 UI 时删除历史账本。
- 配置实验回退：新版本写回旧参数，并检查实际生效；与代码部署回退是两件事。
- 回退后人工核对账户 / worker 状态，不因回退成功自动启动实盘。

## 18. 最终交付检查与交接格式

### 18.1 必须全部回答

- [ ] 六交易所接入、全部覆盖凭据、保存 / 删除 / 诊断正常。
- [ ] 启动 / Stop / restart / delete / 日志 / 实时视图保留，停止实例可查残仓。
- [ ] 平仓准确显示账户市场范围；冲突 / partial / timeout / 幂等测试通过。
- [ ] 全 schema / YAML / 市场别名 / base / 自动 band 参数不丢。
- [ ] 分析、回测、历史、阈值应用路径可用，模型范围清楚。
- [ ] 停止 / 重启 / adopt 不丢策略历史；账户去重正确。
- [ ] 正式净收益仅完整数据晋级，edge / estimate / MTM 分开。
- [ ] 资金费、费用、期初期末、汇率、人工 / 未归因均有处理。
- [ ] 两类策略分别复盘，部分 fill / 分配 / 周期与真实数量一致。
- [ ] 图表与建议可追溯，缺口 / 无数据 / 断线不伪造值。
- [ ] 实验完整记录版本、生效、目标、回退与比较，不自动交易。
- [ ] 鉴权 / 双语 / 窄屏 / 请求乱序 / 组件清理验证通过。
- [ ] 完整测试与迁移 / 回退资料提供，DEVOPS 更新。
- [ ] 未覆盖来源、未完成能力、平台限制明确列出。

有未勾选项就注明实际阶段，不能称“全功能开发完成”。

### 18.2 接手 Agent 的最终报告模板

~~~text
已完成：
- 任务编号、功能和对应文件

用户如何使用：
- 页面入口与最短操作路径

数据与收益覆盖：
- 哪些实际来源已接入
- 哪些策略 / 区间已核对，依据
- 哪些仍缺失；不能判断的原因

验证：
- 命令、通过 / 失败结果、平台
- 浏览器场景与截图
- 运维迁移清单状态

兼容与迁移：
- 旧入口、API、profile 保持情况
- 数据迁移及回退步骤

剩余工作：
- 任务编号、实际阻塞、下一步

生产操作：
- 本次是否部署 / 改凭据 / 改 profile / 重启 / 发单
- 如有，单独列实际授权与结果；没有则清楚说明
~~~

## 附录：可直接复制给开发 Agent 的任务提示

请阅读仓库根目录 CONSOLE-V2-DEVELOPMENT-SPEC.zh-CN.md，以该文档作为本次控制台 V2 的需求和实施依据。先核对当前代码与文档基线，保留用户现有未提交内容。从 V2-001 开始顺序完成；复用 aiohttp、现有 API 和原生 ES modules，不换技术栈。所有原有运维能力必须迁移，尤其各交易所 API Key、Lighter 交易腿覆盖、诊断、启停重启、日志、完整 YAML 与账户市场平仓。收益数据缺失用 null / 待核对，不能把 maker 对冲 edge、taker 入场 edge 或会话 MTM 当净收益。按本文账本、身份、时区、接口、任务和验收规则实现并验证，最后按第 18.2 节报告。仅完成前端框架不能宣称整项完成；原始数据无法支持真实盈利判断时明确来源限制并完成仍可推进的任务。开发与测试使用隔离目录 / stub，本提示不授权生产部署或实盘操作。

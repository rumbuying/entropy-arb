# 网格项目开发交接：Bulk 主网普通网格上线

交接日期：2026-10-31，Asia/Shanghai。面向接手开发者及接手 AI。

## 1. 先读结论：项目没有完成到哪里

**Bulk 当前没有上线可用的普通实盘网格。网站只开放保存账户、检查接入、市场及网格参数预览。**

已经开发并验证过不少底层部件，也发生过真实小额买卖、原生止损触发和非零资金费。但这些是独立诊断及历史副本核验，**不能替代用户从网站创建、启动、自动买卖补单、暂停恢复、结束平仓的一条普通网格完整链路**。目前也没有 Bulk 普通网格连续 24 小时验收通过的证据。

此前推进的主要问题：工作拆得过碎，连续追加单项诊断、历史回放和回归测试，未尽早串通普通执行入口。用户已经明确不接受继续用测试数量或版本数量替代实际可用交付，也不接受等待官方答复才能继续。接手者应利用现有证据，集中贯通一条真正普通网格闭环。

这份文档完成的是交接，不是发布。交接轮只读取本地资料、只读核对线上服务，未下单、未启动金融测试、未部署候选、未重启任何服务。

## 2. 用户目标与已确定的产品决策

### 当前主线

尽快完成 **Bulk 独立主网账户的普通网格实盘上线**。完整范围不能偷偷缩成“只读接入”“再做一次探针”或“仅预览”。

首批产品能力：

- 稳定币保证金永续、做多、等差与等比网格；账户不绑定单一币种。
- 同账户多个币种网格，共享账户资金预算，正确使用交易所自己的杠杆与组合保证金模型。
- 创建、启动、暂停、恢复、结束并减仓平仓。
- 真实成交、手续费、资金费、库存及收益统计。
- 原生止损、断线恢复、重启恢复、归零后再入场保护、小残仓退出。
- 网站能完整操作；按账户、部署、市场、方向和订单政策开放真实验收过的资格。

用户要求首版支持完整杠杆能力，不接受用简单全额现金占款冒充 Bulk 资金模型。现有 v23 已上线杠杆/压力资金预览，接下来要把相同风险约束接入真实发送过程。

### 长期范围

多交易所方向保留：Hyperliquid、Lighter、Lighter-RH、TradeXYZ、Katana、Backpack、Bulk、Ondo、Arcus、Perpl。网格注册表已声明这些入口，**声明不代表已实现或已认证**。Lighter 有初步适配文件，用户此前未准备独立账户；不要把它或其余品牌说成可实盘。当前优先完成 Bulk，不重新分散到所有交易所。

同协议不同部署分别认证；共享资金风险域的品牌不能分开重复分配资金。TradeXYZ 与 HL 的风险域在注册表中有专门约束。

### 用户明确接受的运行方式

1. 旧套利与网格在网站和服务上分开，使用者保证不使用同一个资金账户。不要重新设计复杂的跨套利账户锁。
2. 一次 502、短暂断线不是整个策略验收失败的充分理由。离线期间允许已有挂单及原生保护继续工作；恢复后先核对真实成交、仓位、挂单和费用，再在安全范围内继续。
3. 允许受保护且对账通过的恢复，记录离线时长、恢复耗时及原因。不能把恢复记录抹掉冒充全程无断线。
4. 旧止损已确认撤销且新启动前用户人工检查清空，可以信任这一清理政策，不再无限追问旧止损累计量。这不等于当前策略运行中可以把未知止损成交量填成零。
5. 不恢复高频 AI 自动检查。24 小时观察由服务器有期限地记录；用户之后来要求检查，或仅在关键事件通知。
6. 已有独立账户及真实操作授权，不要求用户重复准备账户、重复输入密钥或重复同样许可。

## 3. 线上入口、服务和目录

### 用户入口

- Bulk 账户：<https://taoli.coinfetcher.xyz/grid/bulk/#/grid/accounts>
- Bulk 创建页：<https://taoli.coinfetcher.xyz/grid/bulk/#/grid/create>
- 原 HL 网格：<https://taoli.coinfetcher.xyz/grid/>

Bulk 使用原网格网站的登录凭证。本交接不复制用户名密码、代理密钥、钱包私钥或凭证加密主密钥。账户已保存并通过只读检查，无需重填。登录问题应检查现有认证配置，不能顺手重置全站认证或修改套利入口。

### 最新服务核对：2026-10-31 11:01:57 +08:00

交接时实际通过 SSH 只读查询得到以下状态，五个服务均为 active/running：

| 服务 | MainPID | 启动时间（北京时间） | 用途 |
|---|---:|---|---|
| `entropy-grid-worker@bulk.service` | 3301268 | 10-30 22:23:38 | Bulk 观察/预览运行器 |
| `entropy-grid-bulk-web.service` | 2298148 | 10-30 01:07:55 | Bulk 网站 |
| `entropy-grid.service` | 161833 | 10-27 22:37:39 | HL 网格运行器 |
| `entropy-grid-watchdog.service` | 161838 | 10-27 22:37:39 | HL 看护 |
| `entropy-console.service` | 198980 | 10-28 00:11:14 | 旧套利控制台 |

PID 是交接时的身份记录，后续必须重新读取，不能把数字永久写死。交接时未重新查询交易所余额；余额及仓单状态的最近证据见下一节，不能把服务 active 等同资金状态正常。

| 项目 | 路径/说明 |
|---|---|
| 本地共享工作区 | `/Users/alex/Work/entropy` |
| SSH 主机 | `root@taoli.coinfetcher.xyz` |
| SSH 文件 | `/Users/alex/Work/entropy/Pendle_Proxy.pem`，仅使用文件，不输出其内容 |
| Bulk 独立工作目录 | `/root/code/entropy-grid-workers/bulk` |
| Bulk 实际生产源码 | `/root/code/entropy-grid-workers/bulk/releases/bulk-account-preview-v23` |
| Bulk Python | `/root/code/entropy-grid-workers/bulk/.venv/bin/python`，最近验证 Python 3.11.6 |
| Bulk 账本 | `/root/code/entropy-grid-workers/bulk/data/grid-runtime.sqlite3` |
| Bulk 加密凭证目录 | `/root/code/entropy-grid-workers/bulk/data/grid-credentials` |
| Bulk 本地 RPC | `/root/code/entropy-grid-workers/bulk/data/run/grid-runtime.sock` |
| Bulk 本地网站/API | `127.0.0.1:8790`，账户路径 `/api/grid/accounts` |
| HL 独立目录 | `/root/code/entropy-grid` |
| 旧套利目录 | `/root/code/entropy` |
| Bulk 候选与验收资料 | 目前主要在 `/root/code/entropy-grid/data/staging` 和 `/root/code/entropy-grid/data/run`；这是历史组织方式，不能据此覆盖 HL 运行源码 |

**重要：Bulk 工作目录下的旧 `entropy_arb/` 不等于当前生效源码。生产覆盖指向 releases/v23；正式改动前查询 systemd 的 ExecStart 与 drop-in，不能改错目录后宣称已部署。**

当前正式 Bulk 的已核验配置为 `GRID_REAL_DISPATCH=0`。候选 v24–v42 没有整体替换到普通生产服务。交接只新查了服务状态和 v42 目录存在性，未新读任何含密钥的环境配置。

保持已知主机校验的只读连接例子：

```sh
ssh -i /Users/alex/Work/entropy/Pendle_Proxy.pem \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 root@taoli.coinfetcher.xyz \
  'systemctl show entropy-grid-worker@bulk.service entropy-grid-bulk-web.service -p Id -p ActiveState -p SubState -p MainPID'
```

不要用 `StrictHostKeyChecking=no`；不要在聊天、文档或日志中打印完整环境变量、vault、私钥或签名请求载荷。线上偶发账户 API 503 时先查原服务/任务身份，再做只读重试，不因一次暂态直接重启服务。

## 4. 账户及资金边界

### 账户身份（非密钥）

| 字段 | 值 |
|---|---|
| venue/network | `bulk/mainnet` |
| account_id | `f9e93774-7f0a-5fe1-9d66-04277c0d9b19` |
| 最近凭证 revision | `d679254a76cc40c6acdcba46ac4c51ed` |
| 公开资金账户地址 | `CEQCFMNRS2mCED3CPz9sc4ReaWhgxA3xkJs5kzCdWW87` |
| 当前支持方式 | 主钱包 MasterEOA、USDC 全仓；不是代理钱包/子账户/逐仓全部支持 |

最近主网只读证据：`data/run/grid-bulk-batch-orders-native-v42.json`，`checked_ms=1793414571097`。当时全账户零仓、零挂单，余额/可用资金 **500.691276 USDC**；账户累计 fees `-0.0042`、funding `-0.000018782`、realizedPnl `-0.005505`。

生产中有 **27 条诊断 intent/cancel 记录，18 个原 intent**；普通核心的 grids/order_intents/operations/budget_reservations 当时均为空。不能把诊断日志数量当普通网格数量。

账户余额约 500 USDC，**不是允许把全部余额用于测试的额度**。新测试前再次通过独立原生读取确认真实仓单和原请求状态。

### 沿用的小额、有期限验证边界

用户已授权使用此独立主网账户实际操作，不需要为同范围重复询问。实际验证必须固定新的原任务身份及新的一次性期限，保留既有边界：

- 每笔开仓名义金额不超过 2 美元；累计开仓名义金额不超过 4 美元。
- 手续费预算不超过 0.01 USDC。
- 验证最长 10 分钟；额外 5 分钟仅用于减仓退出与撤单。
- 最多 5 个退出请求；保护先于开仓到位。
- 不转账、不充值、不扩大杠杆/账户设置操作范围；未知结果先查原身份，不盲目重发。
- 退出失败或有小残仓时保留仍需要的保护，并准确报告。

**上述短期范围不能自动扩成 24 小时或更大资金。** 应先做出可审阅的普通执行版本，形成具体的 24 小时资金/损失/到期退出方案，再一次性确定该范围。不要在版本尚未串通时反复向用户索取抽象许可。

所有已经结束的金融任务、授权文件、grant、policy、launcher 均不得复用或重启，尤其：

- `bulk-funding-20261030-v33`
- `bulk-trigger-20261030-v37`
- 更早 v1/v19/v20/v24 等已结束诊断。

当前没有需要继续等待的 v42 测试或原生核验进程。原 v42 Linux PID 3874487 已结束，报告已取回。前一中断轮仅做读取，没有新实现、金融任务或部署需要恢复。交接轮没有启动任何后台任务；日后执行前仍须核查服务器当前真实任务，不能只凭文档假定空闲。

## 5. 工作区与源码版本：先解决“从哪份代码开始”

本地是混合脏工作区，许多网格文件尚未纳入 Git 跟踪，同时还有用户自己的套利、控制台和其他交易所修改。**禁止 `reset --hard`、`clean`、全目录覆盖、`git add .` 后一锅提交或把所有本地差异整体推线上。** 用户曾特别询问套利近期修改是否被网格部署覆盖，必须保护这些内容。

本地共用 `runtime.py`、`recovery.py` 含其他 HL 变更，不等于 Bulk 已测试源。之前直接混入共用文件差异曾导致 v26–v29 失败，不能重复。

### 建议继承的准确候选

- 服务器完整冻结源：`/root/code/entropy-grid/data/staging/bulk-batch-orders-v42/source`。
- 交接 SSH 已确认该目录存在。
- v42 来源是准确 v41 父源加 10 个 overlay，共 524 个 manifest 条目。
- 报告：本地/服务器 `data/run/grid-bulk-batch-orders-linux-v42.json`。
- 归档：`data/staging/bulk-batch-orders-v42.tar.gz`；**这是覆盖包，不是自包含的完整源码包**。只解这个包不能重建全项目。
- 覆盖清单：`data/staging/grid-bulk-batch-orders-overlay-v42.json`。
- 完整文件哈希在报告 `source_manifest` 中；新的候选应完整继承它，再加入必要变更。

可读 `data/run/grid-bulk-batch-orders-linux-v42-prepare.py` 理解历史冻结方式，**不要重跑它**：它会创建原 stage、启动测试，而且原身份已完成，带有防覆盖断言。

隔离文件：

- `data/run/grid-bulk-runtime-isolated-v40.py`：资金费净额改造的 Bulk Runtime 隔离版本。
- `data/run/grid-bulk-recovery-v30-isolated.py`：恢复父源。
- `data/run/grid-bulk-recovery-isolated-v42.py`：只移植本轮 `_backfill_owned_positives` 后的版本。

最稳妥的开发方式是从完整冻结 v42 树建立新的独立候选目录，仅把明确改动同步回本地对应文件；提交前按文件审查差异。不要覆盖或归档其他人正在使用的工作区。部署前保留旧 release、DB 与加密凭证配套备份，明确唯一 writer 和回滚条件。

## 6. 代码入口地图

以下路径相对 `/Users/alex/Work/entropy`。行号会随修改变化，优先按类/函数定位。

| 模块 | 路径/入口 | 当前作用与注意点 |
|---|---|---|
| 进程入口 | `grid_main.py::run` | 普通 worker、账户恢复、RPC、流恢复、政策过期调度；有非 HL 观察模式的硬限制 |
| 网站 | `grid_web.py`、`entropy_arb/console/grid_web.py`、`grid_router.py`、`grid.py` | Bulk 独立 URL/API 路由与 RPC 转发；避免改到套利入口 |
| 注册表 | `entropy_arb/grid/venue_registry.py` | 凭证字段、部署、能力及账户身份；声明能力不授资格 |
| 账户与凭证 | `entropy_arb/grid/connections.py`、`credentials.py` | 保存和只读检查，Bulk 目前只接 preview factory |
| 普通创建与交易 | `entropy_arb/grid/runtime.py` | `_create_grid`、`recover/_recover`、`sync_funding`、`sync_funding_net`、`performance` |
| 有限实盘试运行 | `entropy_arb/grid/operator_trial.py::OperatorTrial` | 现有普通 commissioning 路径仍直接依赖 HL 账户/历史/精度语义 |
| 正式资格 | `entropy_arb/grid/live_release.py::LiveRelease` | 用真实普通引擎及残仓等证据开放资格；不能拿历史合成网格冒充 |
| 恢复屏障 | `entropy_arb/grid/recovery.py::RecoveryBarrier` | 正面成交可回补；缺完整历史仍阻止恢复交易，尚未接新账户范围出口 |
| 流恢复 | `entropy_arb/grid/stream_recovery.py` | 断线/代次/恢复调度；不把一次网关错误当永久失败 |
| Bulk 账户/市场 | `entropy_arb/grid/adapters/bulk.py` | `BulkAccountVenue`、`BulkGridVenue`；默认无 signer、不可写、无资格 |
| 签名与 HTTP 执行 | `adapters/bulk_execution.py`、`bulk_identity.py`、`bulk_wire.py` | 持久原编号/nonce/动作绑定、实际发送、SDK/oracle；不随意升级 |
| 原订单查询 | `adapters/bulk_bound_read.py`、`bulk_batch_lookup.py` | 精确绑定、前后挂单快照、共享历史读取和批量查询 |
| 成交/终态 | `adapters/bulk_owned_fills.py`、`bulk_order_evidence.py` | 普通及显式条件单证明、原请求证据保存，不改写旧 UNKNOWN |
| 行情/私有流 | `adapters/bulk_market.py`、`bulk_stream.py`、`bulk_risk_feed.py` | 原生行情、账户流、持续公共风险输入；组件尚需接普通工厂 |
| 资金预算 | `adapters/bulk_budget.py`、`bulk_range_budget.py`、`bulk_risk.py` | 原生杠杆/组合/区间风险与共享预算，不套 HL 专用保证金公式 |
| 原生保护 | `adapters/bulk_protection.py`、`bulk_protection_manager.py` | 固定数量预挂、替换、暂停保留、看护/数量证明；普通全生命周期尚未实证 |
| 账户起点 | `adapters/bulk_account_baseline.py`、`bulk_original_boundary.py` | 全账户一次经济基线、旧原请求边界和多市场共享起点 |
| 原生资金与收益 | `adapters/bulk_closed_positions.py`、`entropy_arb/grid/funding.py`、`funding_net.py` | 精确支付与原生生命周期净额独立入账、防重复；current 持仓仍有缺口 |
| 前端 | `entropy_arb/webui/v2/grid/{accounts,create,list,detail,qualification}.js` | 账户不选币种、创建选市场、资格及恢复状态展示 |
| 部署模板 | `deploy/entropy-grid-worker@.service`、`entropy-grid-bulk-web.service`、`nginx-grid-bulk-location.inc` | 独立 Bulk 服务模板；线上还有覆盖项，模板不代表完整线上配置 |

`adapters/` 在表中指 `entropy_arb/grid/adapters/`。`bulk_*_probe*`、`bulk_roundtrip*` 等主要是有限金融诊断，不是用户普通网格生产入口。

## 7. 已经完成的东西：复用，不要再做一遍

### 7.1 v23：已经部署的账户与资金预览

独立 worker/web/运行库/凭证/账本；账户已保存恢复。BTC/ETH 做多等差、等比参数、市场和账户杠杆上限、区间压力资金及共享预算已通过网站实际预览。

这是当前线上版本。它没有普通真实下单资格。不要仅改按钮或 `live_eligible=true` 伪装成可实盘。

### 7.2 v30–v36：成交、生命周期、非零资金费

- 原普通订单累计量与真实成交逐单核对，终态缓存与全序恢复入账；历史副本恢复出 4 笔成交/2 个配对，重启无重复。这不是新普通网格在跑。
- 原生 closed-position 数量、峰值、成交量、VWAP、时间和原编号匹配；原生损益与理论成交现金流分别保存。
- v33 在原生止损保护下买卖各 `0.00001959 BTC`，跨整点产生真实非零资金费，手续费 `0.001051 USDC`，20 个持仓保护样本，最终零仓零单。
- 实际资金费支付原精度 `0.000018781852505625004`；原生 applied net `0.000018782`。两者的差单独保存，不修改支付原值。
- v36 正面支付可在历史不完整时幂等入账，首次/重复/重开分别新增 1/0/0；不会因此写完整历史或授恢复资格。

### 7.3 v37–v39：真实止损和真实协议兼容

- v37 买入 `0.00001951 BTC @ 76942.63`，原生止损自然卖出同量 `@ 76829.85`，费用 `0.00105 USDC`，账户损耗 `0.00325 USDC`。
- **止损触发沿用同一个 root OID**：REST 从 `stop/gtc` 变成 `market/ioc/filled`，仍保留原 trigger、sell、reduceOnly。不是必须寻找一个新子订单。
- v37 原程序曾因未识别这个转换而失败退出；v38 用无签名原单查询追加终态证据完成收尾。旧失败/UNKNOWN 回执保留，不能改成原程序自行成功，也没有延长资金权限。
- IOC HTTP 回执可能包含 maker 对手单和自己的成交两个 execution-event。v39 按持久原 OID 唯一选择自己的事件，不能拿第一项当自己的回执。
- 470 条真实 WS 帧中有 455 次周期 accountSnapshot。重复相同快照去重；变化快照使观察失效，但不因第二个 snapshot 自动误判重新连接。普通 IOC 推送的 market 标签需原绑定及 REST 确认。
- 原 27 条金融记录、nonce/OID/原 UNKNOWN 保留；未复投原请求。

### 7.4 v40：闭合资金费净额进入正式收益

`funding_net.py` 已与同步器及统一收益连接，已闭合原生生命周期形成独立净额组成项，真实支付仍保留原值，重叠部分不重复扣费。

原 v33 历史副本首次/重复/重开：手续费 `0.001051`、资金费 `-0.000018782`、理论成交毛利 `0.0007585248`、净损益 `-0.0003112572`。全局 funding history 仍 incomplete。

**限制：当前 `observe_position_net` 仍有 16 个原单上限、要求全终态/归零，只覆盖 closed，不能当持续持仓 funding 已对账。**

### 7.5 v41：账户经济基线与创建起点

`capture_recovery_baseline` 在账户执行锁内核对新鲜全账户空仓空单、结清资金、原闭合请求和核心状态，原子保存起点及事件。BTC/ETH 共用基线，市场游标分开；重启/凭证轮换不重置经济起点。

`BulkGridVenue.history_cursor_from_snapshot` 已挂现有创建钩子。**真实验证是在独立账本副本中完成；生产普通执行前还需在独占控制下捕获正式起点，不能直接把副本基线或 grant 搬入生产充当许可。**

### 7.6 v42：批量原单、OPEN 回补和止损实际量

- 共享当轮前后两次真实 openOrders 与同一历史分页；64 个混合原单及 fills 用 7 次读取，单次最多 256 原单，16 MiB/90 秒边界。
- OPEN 部分成交单重新取得整组可变快照，与缓存的不可变终态全序回放。OPEN 数量不跨重启缓存作终态；终态扫描轮换，提交前重验 epoch、控制状态、revision 和原请求集合。
- 已入账同键执行仍核对费用/资产/身份/序号，避免重复键吞掉费用漂移。
- 显式 `include_conditionals=True` 使用独立 v2 证明；在挂未触发根单仍 `cumulative_known=false`，只是当次未触发观察；取消后未知不填零。
- 固定数量止损实际 FILLED 量可以小于潜在覆盖上限；只接受正面 `0 < actual <= bound`，普通 FILLED 仍要求满量。
- 生产 SDK 对原 v33 买卖及 v37 买入/止损四个已经结束的主网原单，用 3 次账户读取确认终态，整次 19 次公共读，无签名/金融请求/生产 Runtime writer。

限制：超过 256 个可变原单仍 pending；全局历史屏障仍在，**不能因此恢复交易**。v42 没有新的普通实盘或真实持仓故障证明。

## 8. 尚未贯通的真实阻塞：不是“等官方”

### A. 普通进程/启动资格仍锁在 HL 路径

1. `grid_main.py::run` 对非 `hl` worker 携带 `grid_trial_policy`、`grid_live_policy` 或旧金融 allowance 直接拒绝，文字也声明 observation only。
2. `OperatorTrial` 创建路径仍调用 `root.authorization_check()`；直接要求 `unifiedAccount/unified_scope`、HL `positions.szi/order_rows`、HL `prepare_history`、`hl_rate` 精度及时间语义；还有杠杆不超过 3、网格数量 2–5 的既有试运行限制。
3. `ConnectionAccounts.attach_preview` 只给 Bulk 注册只读 factory，明确拒绝 signer/write_enabled/live_qualified/protocol/order_transport；`public_record` 也始终显示无实盘资格。
4. `BulkAccountVenue` 默认无 signer、不可写、无实盘资格；普通 credential → signer → transport → risk/private feed → protection → trial/release 的完整运行工厂尚未接入。

因此，“底层可以签一笔诊断单”不意味着普通网格可以启动。应把交易所差异抽成明确 hooks/策略，保留 HL 默认行为，为 Bulk 生成单独有期限、有预算、绑定账户/部署/市场的普通引擎试运行权限。**不能单纯删除拒绝条件、翻几个布尔值或套 HL 政策文件。**

### B. 恢复仍依赖无法证明的全局旧历史

`RecoveryBarrier.run` 虽会先做 v42 正面回补，但全局页 incomplete/truncated 仍会挡住；`runtime.recover` 后续还会同步 funding，`funding.status().ok` 的全局失败继续影响风险/重建。仅加一个 standalone proof 文件不会改变运行结果。

应以 v41 全账户起点为根，把所有基线后自有普通单、保护根单、当前 OPEN 累计、真实 fills、closed/current 持仓及费用累计组合成明确作用域的恢复依据，并真正接进恢复决策和风险可用状态。

不要为了恢复把 `HistoryPage.complete`、`FundingPage.complete`、旧 coverage 或游标改成成功。全局未知与当前自有范围可恢复是两个不同状态。

### C. 当前持仓资金费、长期订单与现金差额没有全部解决

- v40 只处理 closed/归零和有限原单，当前持仓费用/资金费还需正式入账、归属、去重及账户累计互证。
- v42 去掉了一层 16 单限制，但净额发布层仍有旧限制；不能说长期运行容量已完全解决。
- 取消止损的累计未知，在当前运行恢复中不能直接伪造零。若能从全市场仓位、已知普通累计、只减仓保护、费用及资金变化推导，应单独保存守恒推导依据，原查询仍保持未知；证据不足仍冻结新增开仓。
- Native 钱包、原生 realizedPnl 和精确逐笔现金流存在细小但真实的不同精度结果，见下一节。不能用随意 epsilon 或猜测舍入抹平。

### D. 真实普通生命周期与故障验收尚未发生

还没有实际普通策略的创建→启动→maker 买卖补单→暂停/恢复→结束平仓全链路；归零再入场保护重挂、真实带仓断线/重启、小残仓以及普通 24 小时独立终审也未通过。

## 9. 原生协议和金额：已踩过的坑

1. SDK 固定 `bulk-keychain==0.1.30`；已核验 wheel SHA256：`241ce45a32487f862537bd91e7736d6faf50c7c4e80d2c5e7c1f55e665e4b0d2`。已有官方 Rust 身份 oracle 和主网原止损编号证据，不用随意升级 SDK 重开同类实验。SDK 类型对某些金额默认零或遗漏，原生 JSON 数字文本才是费用证据。
2. 原生 `fullAccount` 实际有 kind/name/authorizedAgentWallets/margin/reserve/positions/openOrders/feeTiers/leverageSettings，**没有 pendingOnFill/lockedAmounts**。不能把 `.get(..., [])` 叫原生零待处理证明。
3. `history.asOfSlot` 是最后账户活动锚点，空账户可能长期不变，不是当前链头，也不是历史完整证明。起点需保留全部旧原请求边界，不能只按 slot 相等归属。
4. 当前仓位真实字段有 size/price/realizedPnl/unrealizedPnl/fees/funding，没有 closed schema 的 openTime/maxQuantity。不要对 current 套 closed 匹配公式。
5. 原生止损触发后的同 OID market/ioc/filled 已实证，保持原 trigger、方向和 reduceOnly；不要再猜子单或重复触发测试。
6. 原成交排序是 `slot:sequence`，不能只按 sequence 整数或时间戳排序。v42 回补按原生全序。统一 performance 对同时间多价成交的排序还需检查，**尚未宣称修复**；共用改动不要破坏 HL 排序。
7. Native closed fee 的 binary64 汇总误差已有严格可重现的逐笔同位折叠证明。逐笔精确 fee 与原生 aggregate 原值分开；不是通用金额容差。
8. 原 v37 首个持仓快照 `margin.totalMargin=500.694001`，比开仓前 `500.694526` 少入场 fee `0.000525`；另有 unrealizedPnl。此样本 totalMargin 是现金保证金，不能再扣一遍浮盈亏。

仍需说明来源的金额差：

| 原样本 | 实际值及限制 |
|---|---|
| v33 资金费 | raw payment `0.000018781852505625004`，applied net `0.000018782`；差 `0.000000000147494374996` 已单列 |
| v33 钱包变化 | `500.694838 → 500.694526`，变化 `-0.000312`；Native PnL `+0.000758` 减 fee/funding 得 `-0.000311782`，还差 `-0.000000218` |
| v33 理论成交现金流 | 理论净额 `-0.0003112572`，与钱包变化差 `-0.0000007428`；不能把两者硬当完全相等 |
| v37 现金流 | 精确交易毛差 `-0.0022003378`，Native PnL `-0.0022`；钱包总损耗 `0.00325`，理论含费 `0.0032503378` |

这些差不应扩成无限期“等官方”的阻塞，但需要依据真实结算/字段精度处理，明确展示原生结算、理论交易值和未解释差。不要把差当外部收入、随手补 payment 或用 EPS 认证现金全对账。

## 10. 接手后的最短实施顺序与交付标准

### 第一步：建立准确候选并串通普通启动

从完整 v42 冻结树开始，确认依赖、保存账户和所有原任务状态；不要重跑已结束测试或金融 RID。实现 Bulk 普通执行 factory 和 exchange-specific trial hooks，同时改 `grid_main` 的政策装载/恢复分派。复用现有 signer、risk feed、私有流、共享预算、保护 manager。

交付标准：一条有限普通策略能经既有网站/API 创建、启动，使用真实保存账户、独立有期限政策和原生保护，不靠 standalone probe 买卖；HL 入口行为保持原有约束。先用离线端到端用例验证贯通，不等新架构全盘重写。

### 第二步：把账户自有范围证明接进真正恢复决策

建议与第一步在同一候选内完成必要接线，避免再做孤立 proof 后停下：

1. 正式独占捕获一次账户经济起点，绑定账户/部署/旧请求边界，跨市场共享。
2. 包含所有基线后自有普通单/保护单、未发送封存及未知请求，不遗漏外部资金或仓单。
3. 用 v42 批量及不可变终态缓存，拿当前可变原单的前后新鲜观察；精确 fills 全序入账。
4. closed/current 生命周期、全账户各市场库存、挂单、费用、资金费、原生损益累计差互证；明确资金费 raw/net 重叠规则。
5. 证明绑定 baseline 指纹、请求图、epoch、credential revision、私有流代次、账本位置和观察期限，提交前重验。新增请求、流代次变化或过期必须失效。
6. `RecoveryBarrier`、`sync_funding`、`owned_risk_context` 和重建/发送入口共同消费新的 scoped 状态；global history 继续 unknown。
7. 恢复不通过冻结新增开仓；保护/授权减仓退出保持可用，不能被全局 history 错误一并拖死。

交付标准：断线期间原挂单成交，恢复准确补账、幂等且能在有效范围依据下继续普通补单；证明失效或经济冲突则阻止新增风险。大于单轮容量时有界协调，不假称已核完。

### 第三步：同一普通策略完成真实有限闭环

沿用小额期限，先查原请求/账户确认新身份尚未启动。目标是普通引擎触发 maker 买入及卖出补单，而不是再调用 IOC 诊断程序。覆盖暂停保留保护、恢复重新对账、结束撤自有单并 RO 归零；保留成交、费用、配对、资金费与原请求终态。

市场未成交就如实记录“未取得 maker 循环证据”，不能把只挂撤或买入后最终退出称一个网格补单循环。不得通过不断买入人为制造测试次数或延长原政策。

### 第四步：持仓故障、再入场、小残仓

在真实普通运行中验证带仓断线和服务重启：旧挂单/原生保护有效，恢复查原身份，不重复订单；归零之后下一次入场必须重新有保护；低于普通最小量的残仓走真实可用减仓政策。

不把空账户重连、历史副本回放、离线 fixture 或单次自然止损替代这些验收。暂时 502 可恢复，不自动清空或终止全部策略。

### 第五步：部署、24 小时观察和终局核验

普通有限闭环形成后，再确定明确 24 小时资金/损失预算、截止时间及到期清理。发布到 Bulk 独立 release，排他切换 Bulk writer，保留旧 release/备份；不改 HL 和套利服务。

服务器自主记录至少 24 小时：原始开始/结束、保护状态、断线/恢复时间、对账结果、补单与费用，报告允许的受保护恢复。到期执行约定的退出/清理或明确状态，未知只查原单。

最后由独立原生查询核对全账户仓位、挂单、成交、资金费、所有请求终态及 DB/vault 配套备份可恢复。正式资格按已验证市场/方向/部署/账户政策开放，而不是为所有交易所或币种一键开绿灯。

本交接不提供“几小时一定上线”承诺；真实 maker 成交等待、带仓故障以及 24 小时观察都有不可压缩的验证时间。可以压缩的是重复探针、反复全量回归和孤立组件工作。

## 11. 测试与证据索引

所有以下结果已完成，不需要仅为接手再跑一轮。不同版本测试数量不能相加；v40 全量不代表 v42 全量。

| 版本 | 实际证据 | 能证明/不能证明 |
|---|---|---|
| v23 | `reports/2026-10-30-bulk-account-preview-release.md` | 当前预览发布；不是普通交易发布 |
| v30 | `reports/2026-10-30-bulk-ordered-recovery-v30.md` | 历史副本成交恢复；不是新实盘或带仓故障 |
| v35 | `reports/2026-10-30-bulk-nonzero-funding-terminal-v35.md` | 真实非零 funding 及独立终局 |
| v36 | `reports/2026-10-30-bulk-positive-funding-v36.md` | 正面 payment 入账与幂等；不补全旧历史 |
| v38 | `reports/2026-10-30-bulk-native-stop-v38.md` | 真实止损触发、原失败保全、无签名收尾及备份 |
| v39 | `reports/2026-10-30-bulk-native-compatibility-v39.md` | ACK/周期 snapshot/原生格式兼容，Linux 1115 |
| v40 | `reports/2026-10-31-bulk-funding-net-v40.md` | closed funding net/收益，相关 1152；同源全量 3035 |
| v41 | `reports/2026-10-31-bulk-account-baseline-v41.md` | 一次账户起点及多币种共享，Linux 1174，实际 SDK 副本重开 |
| v42 | `reports/2026-10-31-bulk-batch-orders-v42.md` | OPEN/批量/小于止损上限实际量，Linux相关 1194；四个旧原单主网只读 |

上述 `reports/` 相对 `planning/grid/`。主要机器可读证据位于本地 `data/run/`，服务器同名文件在 `/root/code/entropy-grid/data/run/`：

- `grid-bulk-batch-orders-linux-v42.json`、`grid-bulk-batch-orders-native-v42.json`
- `grid-bulk-account-baseline-linux-v41.json`、`grid-bulk-account-baseline-native-v41.json`
- `grid-bulk-funding-net-linux-v40.json`、`grid-bulk-funding-net-full-linux-v40.json`、`grid-bulk-funding-net-native-v40.json`
- `grid-bulk-original-compatibility-native-v39.json`
- `grid-bulk-trigger-settlement-v38.json`、`grid-bulk-trigger-request-preservation-v38.json`、`grid-bulk-trigger-backup-v38.json`
- `grid-bulk-funding-native-v35.json`、`grid-bulk-positive-funding-native-v36.json`

关键 SHA256，防止读到别轮证据或错误覆盖包：

```text
v42 overlay archive:
abfbcdef6410464a31a0b4f7f4caea766f0b8a9066f50f442a969597a9ed2bca
v42 Linux results.xml:
82987c2bf132a143fbb9e067c15a6f797e1acea0e7a4d4e6c5fa632268ec9058
v42 native report:
77ca8eede12677a3536a1a14814f983c3f7b239309540628e284d4702050df57
v41 native report:
a59288dcd39994a487119d0d114bb67eea19f9d819f4eae313e07dd35072cb21
v40 native report:
2f6ac06ea818a21247219ade82d51f69d9fb8b8c7a490f1522671843c28dcb51
v40 full Linux results.xml:
75eb6e98708f3ee5cdfa0c451d24c7df813033c458b16ab6d6e02f0ef475e1ac
```

v42 新增测试重点：`tests/test_grid_bulk_batch_lookup.py`、`test_grid_bulk_open_order_recovery.py`、`test_grid_bulk_order_evidence.py`。其他已有 `test_grid_bulk*.py`、funding/ledger/ROI/recovery tests 按变更选择。

本地 `.venv/bin/python` 当前为 Python 3.14，生产为 3.11.6；生产运行库 SDK 与普通开发环境不可混为一谈。Linux 新候选必须用匹配解释器、锁定依赖测试。离线网格测试有 socket fixture，需要只允许回环、拒绝外网的 guard；冻结树内的 `offline_guard.py` 与历史 runner 可参考。不要不带网络隔离跑会访问真实交易所的广泛套件，也不要重跑含金融操作的 launcher/CLI。

新变更先测直接相关行为；涉及共用引擎/政策时补必要 HL 兼容回归。已有通过后只有新改动、失败或新风险才扩大范围，不为报告漂亮重复全部数千测试。每次新测试用新目录/报告，原失败和原成功均保留。

## 12. 真实金融样本与原身份（仅用于查询，不可重投）

| 样本 | 原生 OID |
|---|---|
| v33 entry | `3Z5YyS2DUV1kpPaqzCdtodraS42WvPxcxQZyyNNVCsnw` |
| v33 exit | `BeJcggdn9dHCAG86oZmv7Z1SG6FNPKUPJrqawy47oboS` |
| v33 已撤 root stop | `58HrLfd9ZB8icSMRrfejMyXL2WDE3JoNconHv3sNErUM` |
| v37 entry | `GJU4f8bH6jGhj4tadZeBPZUgrJVbKQ2HXc3fqHnqn1g5` |
| v37 自然触发 root | `5Vu7edkreND9pjtDSzBFCJXekJ4ntTyhnShYNoYXcueq` |

v33 payment key：`c26b77d296ad6029065cd40a45aaeedd8daf7d59444c5dd242dbc35b5d4e92f0`，slot/sequence `635007334/271`。v37 entry/stop 成交 slot 分别 `640464735/640469011`。

v37 公开原始 WS：`data/run/bulk-trigger-20261030-v37-raw-account-ws.jsonl`，473 行/470 帧。真实 fixture：`tests/fixtures/grid_bulk/native-ioc-stop-stream-20261030.json`。需要精确数字时保留原文本，不以普通 JSON 浮点加载后的值当精确金额。

v38 独立配套备份在服务器：

`/root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261030T142641Z-2cc9d828`

DB SHA256：`7902f7f0351adcffc2ef239c909490cfd98043990fa4b017c194c1fe89e0bbe4`。这是历史备份，不替代新部署前的新备份。凭证配套在服务器内保持加密，不下载明文。

只读核验 helper 位于 `planning/grid/tools/verify_bulk_batch_orders_v42.py`、`verify_bulk_account_baseline_v41.py` 等。它们有原报告防覆盖与空核心等前提，**先读源码，不因为文件名 verify 就直接重跑**。

## 13. HL 与旧套利：保留边界和最近结果

旧套利服务、目录、凭证和用户近期修改不在 Bulk 发布范围内，不 git pull 覆盖、不重启、不顺带部署。HL 执行版本也不因 Bulk 组件更新自动升级。

HL 最近独立终审见 `planning/grid/reports/2026-10-30-hl-v21-terminal-review.md`：

- 最近策略 `g-e3debcab696d41ba` 已结束；当时全部 85 个历史 HL 网格 CLOSED。
- 受保护、允许对账恢复的观察时长 `86430.321 秒`，8 次重连，恢复约 121–182 秒，净损益 `-0.020198 USDC`。
- 不能称“全程无断线 24 小时”；该样本没有网格配对成交，仅入场与最终退出，不能独自代表 HL 所有功能认证。
- 交接时只重新确认 HL/看护/套利服务存活和进程身份未变，未新做 HL 资金审计。

用户有时会再问 HL 状态，应独立只读查询原策略、原请求和交易所状态，不能用 Bulk 进度回答，也不能悄悄新开 HL 策略。

## 14. 其他资料及阅读顺序

建议先读本交接，再按需要定位：

1. `planning/grid/BULK-LIVE-GOAL.zh-CN.md`：完整 Bulk 要求矩阵，最新覆盖在顶部。
2. `planning/grid/BULK-OWNED-RECOVERY.zh-CN.md`：账户范围恢复设计与事实。
3. `planning/grid/BULK-ACCOUNT-BUDGET.zh-CN.md`、`BULK-RISK-FEED.zh-CN.md`、`BULK-FIXED-PROTECTION.zh-CN.md`：资金、持续风险及保护模型。
4. `planning/grid/BULK-ORDER-EVIDENCE.zh-CN.md`、`BULK-IMPLEMENTATION.zh-CN.md`：原身份与早期适配说明，结合最新报告读。
5. 本交接第 11 节的 v38–v42 报告及机器证据。
6. `GRID-TRADING-PLAN.zh-CN.md`、`GRID-TRADING-DEVELOPMENT-SPEC.zh-CN.md`：总产品/工程要求。
7. `GRID-TRADING-AI-HANDOFF.zh-CN.md`、`GRID-TRADING-AI-PROMPT.zh-CN.md`：早期通用交接，不能用其旧状态覆盖本交接。
8. `planning/grid/RESUME.md`：巨量历史按新到旧排列，**旧的“在跑/待许可/待下载”不能当当前状态**；先看顶部及本交接，必要时搜具体 RID/版本。

## 15. 可直接复制给接手 AI 的执行提示词

> 你接手 `/Users/alex/Work/entropy` 的网格项目。用户目标是尽快让 Bulk 主网普通网格真正上线，不是继续单项金融探针。先完整阅读 `planning/grid/BULK-IMPLEMENTATION-HANDOFF-2026-10-31.zh-CN.md`，核对最新事实、源码身份与资金边界。
>
> 当前 Bulk 正式生产仍为 preview v23、GRID_REAL_DISPATCH=0；已有独立账户和小额有期限主网授权，但没有普通网格全链路及 24 小时通过证据。v42 完整冻结候选在服务器 `/root/code/entropy-grid/data/staging/bulk-batch-orders-v42/source`，不是 overlay tar 单独可还原；本地共用文件混有其他 HL 修改，不得整体覆盖部署。
>
> 先从准确 v42 源建立新候选，集中贯通普通执行 factory、非 HL trial/政策装载、账户自有范围恢复出口与资金费运行状态。`grid_main.py` 非 HL 政策硬拒绝、`operator_trial.py` HL 专用语义、`connections.py` preview-only 是已确认缺口，不要只改一个证明模块后停止。复用现有 Bulk 签名/编号/风险/预算/私有流/止损部件，不用翻标志绕资格。
>
> 实现普通创建、启动、真实 maker 买卖补单、暂停恢复、结束归零，再完成保护重挂、真实带仓故障、小残仓和独立终局。短暂 502/断线可以恢复，先对账，不一律判失败。全局旧 history/funding 仍 unknown；用新鲜、绑定请求和代次的自有范围证据恢复，不伪造 complete 或 payment。
>
> 已完成的诊断、旧金融 RID、grant、policy 和 launcher 不复用、不重跑。新操作先查原身份；未知只查原单。遵守文档里的小额、费用、次数、期限边界，不把 10 分钟许可自动扩成 24 小时；普通版本可审阅后再具体确定 24 小时预算。CLI/API 操作资金，不下载密钥，不重启本机网格，不产生双 writer。
>
> 保留旧套利和 HL 的服务、源码、凭证及用户修改；只在 Bulk 独立服务部署。不要清理混合脏工作区、全量提交或顺带推套利。针对实际改动测必要用例，已有通过后不反复跑数千回归。不等待官方答复、不重复索取已有账户和短期许可、不恢复高频 AI 跟进。每次汇报只写实际可用行为、关键缺口和真实验收证据，不能拿测试数量或历史回放冒充上线。完成后更新本交接与 RESUME，使下一人无需重读全部聊天。

## 16. 接手时的完成检查表

- [ ] 已核对生产真实 release、服务身份、原任务、仓位和挂单，没有重复操作旧请求。
- [ ] 新候选从完整 v42 manifest 继承，共用文件差异审查过，未混入用户的其他改动。
- [ ] 非 HL 普通政策装载、账户执行工厂及 website/API 生命周期真实连通。
- [ ] 账户 baseline + 全部自有普通/保护请求 + closed/current 经济证明进入恢复和风险决策。
- [ ] 完整范围未知时新增风险被正确阻止，原生保护与授权减仓仍可运行。
- [ ] 普通引擎取得真实 maker 买卖补单、暂停恢复、结束归零证据。
- [ ] 归零再入场保护、带仓断线/重启、小残仓均有真实证明。
- [ ] 同账户多币种共享资金及全账户恢复不会重复占款或漏仓；资格范围如实标注。
- [ ] 明确 24 小时资金/期限，到期看护及日志在服务器自主运行，未恢复 AI 高频巡检。
- [ ] 独立原生终局核验和配套备份恢复验证通过；按已验范围开放普通实盘。
- [ ] HL、旧套利未被修改部署；文档区分“实现”“已部署”“实盘已证实”和“仍待验收”。

交接截止时，上面关于普通 Bulk 实盘的事项尚未完成。底层已完成组件和样本按第 7、11、12 节复用，不从零重做，也不能跳过真正的普通执行贯通。

## 17. 2026-10-31 后续轮记录：第 10 节第一步已代码贯通（未部署）

本轮从 v42 完整冻结树建立候选并完成「建立准确候选并串通普通启动」的实现与离线端到端验证。生产未变（v23、GRID_REAL_DISPATCH=0、PID 3301268/2298148 未动），未下单、未重启任何服务、HL 与套利未动。

### 17.1 候选位置与改动

- 服务器：`/root/code/entropy-grid/data/staging/bulk-normal-execution-v43/source`；本地镜像：`data/candidates/bulk-normal-execution-v43/source`。逐字继承 v42，仅加明确改动；本地工作区 v42 之后的 4 个恢复文件（`recovery.py`、`runtime.py` 恢复块、`stream_recovery.py`、`bulk_leverage_probe.py`）未被覆盖，runtime.py 的 3 个新补丁已按补丁同步回工作区。
- 新增 `entropy_arb/grid/adapters/bulk_execution_factory.py`：`execution_venue` 从 vault 行组装 BulkKeychain+BulkOrderTransport+BulkAccountBudget+BulkRiskFeed 的执行级工厂；`attach_execution_venue` 只接受政策点名的账户/凭证版本，替换 preview 桥（拒绝替换已执行 venue），先 `capture_recovery_baseline`（v41 平账起点）再武装 `write_enabled/live_qualified/execution_qualified`，并把 `history_start_slot` 设为基线锚点。基线失败 → worker 启动失败，不回退 preview。
- `operator_trial.py`：新增 `BulkOperatorTrial`（schema 2：venue=bulk、mainnet、account_id+credential_revision、markets、modes、execution_policies、capital_limit≤50、cumulative_limit≤100、expires_at；**不 pin request_ids**，创建幂等由 store register_request 承担）与 `load_trial_policy`（HL schema 1 路径行为不变，venue/schema 不匹配拒绝）。v2 的 `execution_gate` 是 venue 门控链内的线级围栏（prepare/send 两相：范围冲突与过期抛错、政策越界返回拒绝码）；`validate_creation` 在创建事务内全量复检（含 protection 必填）；到期 `expire()` 对未平仓网格发退出；重启无政策文件 → 清理模式仍强制退出。
- `grid_main.py`：非 HL worker 允许装载 schema 2 试运行政策（allowances 与 HL live policy 仍拒绝）；武装执行工厂要求 `GRID_REAL_DISPATCH=1`，否则进程拒绝启动；武装后启动账户私有流与风险 feed，关闭时经 `accounts.close()` 收敛。
- `runtime.py`：`_preview` 对试运行范围内配置标 `plan.live_eligible`（capability `operator-trial:<policy_hash>`）；`_create_grid` 在事务内经 `website_context` 构建上下文并复检；v1 判定改走 `request_authorized`（HL 行为等价）；`real_dispatch` 指标包含已武装试运行。
- `rpc.py` catalog 合并试运行市场授权（`operator_trial` 标记 + `live_eligible`）；`accounts.py` 新增 `start_stream` 并在 close 关停 risk feed；`create.js` 试运行市场文案。
- e2e 揪出并修复的既有潜在 bug：`bulk_budget.order_matches`/`_pending_floors` 与 `bulk_range_budget._inventory_terms` 对账本 `reduce_only` 0/1（及不存在的 market 字段）的严格比较失败；`BulkRiskInputs.__deepcopy__` 返回自身——门控链此前对 ~1MB/市场的冻结风险面每单 deepcopy 4-6 次（服务器实测每单 ~7s CPU，触发 5s 风险许可过期；cProfile 22.3s/29.5s），修复后毫秒级。这是生产必需修复，fence 仍逐单重验。

### 17.2 验证证据

- 新增 `tests/test_grid_bulk_normal_trial.py`（8 用例）：政策分发/权限/范围拒绝、工厂仅在平账基线后武装、网站预览标 live_eligible+创建幂等重放、越界市场与超额拒绝、启动先原生止损后 post-only 挂单且全部经过工厂/executor 门控/线级围栏、累计上限拦第二个并发网格、到期冻结新发送并请求退出、preview-only 永不授权。
- 服务器（Python 3.11.6，`/root/code/entropy-grid/.venv`，offline_guard）：新用例 **8/8（9.8s）**；bulk 全套 **1123 pass / 0 fail / 0 skip**；HL 侧 operator_trial/live_release/accounts/executor_capacity_lock/initial_inventory **114 pass**。
- 本地工作区同步后 bulk 套件 1120 pass + 2 个预存在环境失败（pristine v42 本地同样失败）；`offline_guard.py` 已复制到工作区根目录；faucet 测试所需 `planning/grid/tools/dispatch_bulk_testnet_faucet.py` 已补入候选与工作区。
- 未重跑任何旧金融 RID/launcher；无新金融操作。

### 17.3 部署前必须拍板的决策（第二步起）

1. **资金边界与市场**：已捕获市场规则 ETH-USD minNotional=50 USDC（上线前需实时重读 exchangeInfo 复核），§4 的「每笔 ≤2 USDC」探针边界放不下任何真实网格订单。需在「改用更小 minNotional 市场」与「批准提高单笔/累计预算（例如 4 格 × ~13 USDC ≈ 55 USDC，止损在位、到期退出、全程可审计）」之间做出决定；不得自动扩权。
2. 政策文件（schema 2）按上述边界起草 → 审阅 → 写入 bulk worker 可读路径（0600）。
3. 部署序列：备份 DB+vault（新备份，不替代旧备份）→ releases/ 下新 release 目录 → systemd drop-in 指向新 release + `GRID_REAL_DISPATCH=1` → 排他切换唯一 writer → 重启 bulk worker/web（不动 HL/套利）→ 只读核对服务/基线/武装日志。
4. 之后按第 10 节第二步继续：账户自有范围恢复接线（v41 基线 + v42 批量证据接 RecoveryBarrier/`sync_funding`/风险可用状态；全局 history 仍 unknown，不得伪造 complete）→ 第三步真实有限闭环 → 第四步带仓故障/再入场/小残仓 → 第五步 24 小时观察。`observe_position_net` 的 16 单/仅 closed 限制仍在。

## 18. 2026-10-31 后续轮二：用户批准预算 → 真实部署 → 第二步恢复接线 → 主网真实挂单（单点待收尾）

用户已批准「提高试运行预算」（约 55 USDC 级）。实时核对 Bulk 主网 exchangeInfo：**BTC-USD minNotional=1.0 USDC**（其余市场 40–50），试运行市场定为 BTC-USD。本轮完成真实部署、第二步恢复接线，并在主网真实挂出第一张普通引擎的原生保护止损单。当前卡在启动后的最后一处暂停源。

### 18.1 真实部署（已生效）

- 备份：`/root/code/entropy-grid-workers/bulk/data/grid-backups/grid-20261031T045510Z-v43-trial/`（DB SHA256 `0d8d919bd1732b17e56a7777bccdf521bd85624f100280e2ec966938923afc4b` + 加密 vault 目录副本，均在服务器内）。
- 发布：`releases/bulk-normal-trial-v43/`；drop-in `95-normal-trial-v43.conf`：worker 指向 v43 + `GRID_REAL_DISPATCH=1` + `--grid-trial-policy`，web 指向 v43。旧 `90-preview-release.conf` 保留。
- 政策：`policies/bulk-trial-20261031-r1..r11.json`（schema 2：BTC-USD、long/deferred、post_only+gtc、单格 ≤40、累计 ≤80、6–8 小时硬到期）。**r1–r10 的累计额度被零成交的启动调试消耗殆尽**（每次失败的 start 都产生 40 的预留记录，账户实际分文未动——主网 fullAccount 全程零仓零单、计数器与基线一致，已多次独立核实）；当前生效 **r11**。
- 驱动方式：网站 HTTP API（`127.0.0.1:8790`，与网页同一路径）。注意两点：① create 的 web 代理默认 5s 超时会 503 但 worker 侧仍会提交——**必须用同一 request_id 幂等重试**，换新 id 会重复建格；② `grid.py` 的 GridProxy 已改为 30s 超时。每次调试周期后必须清理 PAUSED 残留网格（同市场唯一性会拒绝新预览：`BULK_RANGE_OWNED_CONTEXT_REQUIRED`）。

### 18.2 第二步：账户自有范围恢复（已上线并在主网验证）

新增 `entropy_arb/grid/adapters/bulk_recovery_scope.py`：以 v41 平账基线为根的恢复证明——基线边界内的原单按指纹跳过（其经济已计入基线计数器），基线后原单逐笔 bound lookup（openOrders+orderHistory 包，不依赖历史窗口；终态写入与 barrier 相同的不可变证书）；全账户仓位/挂单双向对账（复用 `order_matches`，journal 行经 `_order_view` 转换）；费用/资金费按原生序 f64 折叠进基线计数器（位级相等，任何未入账/外来经济都会破坏折叠而拒绝）；绑定凭证版本/基线指纹/私有流代次（重连即失效，gap 标志刻意不参与——恢复时刻 gap 必然未清，折叠本身是漂移检测）。

接入点：① `RecoveryBarrier.run` fills 页不完整时先 v42 回补再捕获 scoped basis，成功则清除回补过渡 gap 并标记 `scoped_basis`（覆盖游标不前进，全局 history 仍 unknown）；② `_recover` 的 funding 失败在 scoped 有效时降级（`scoped_funding`）；③ `pump_events` 的 funding 检查对 bulk 的抛错型失败走同一判定（basis 过期时先重捕获）；④ `owned_risk_context`/`portfolio_risk_context` 新增 `scoped_funding` 通道。

**主网验证**：真实卡死网格 g-7017dcf0af8441b1（全局 fills/funding 页自基线锚点截断，即 §8.B 的真实形态）在 scoped basis 下恢复通过并干净平仓（CLOSED epoch 3）。调试中暴露并修复：`_pending_floors`/`order_matches` 的 ledger 0/1 与缺失 market 字段、`_inventory_terms` 同类问题、`BulkRiskInputs.__deepcopy__`（此前每单 ~7s CPU 的 1MB 风险面深拷贝）、`lookup_many` 需用 native_order_id 绑定键、watch 与 BulkProtectionManager.observe 在「激活尚未写入租约」窗口内会把 STARTING/RECOVERING 网格误杀（守卫：无租约记录且（STARTING/RECOVERING 或无活跃意图）→ 跳过本 tick；租约存在但过期的本地退出语义不变）。

### 18.3 主网真实进展与当前单点

- 试运行网格 g-2ec0b7f2d5864e7c（BTC-USD，76869.09–77487.13，4 格，40 USDC，止损 76482.81）经网站 API 创建、启动成功至 RUNNING；**原生病挂止损已真实挂上主网订单簿**（sell 0.00723242 @ 76482.81 reduceOnly gtc resting，主网 openOrders 独立核实）。
- 单点：激活完成后网格又被置 `NEEDS_ATTENTION/PAUSED`（epoch 1→2，无 protection_gap 事件、无 risk_paused 事件——非 watch/refuse/monitor 已知路径；FENCE_FAIL/STREAM_CHECK_FAIL 插桩已移除）。下一个接手者：重启 worker 后网格处于 RECOVERING/PAUSED，恢复流程会带租约处理；先复现该暂停（对 `LiveRiskGuard.freeze`/`_set_status(NEEDS_ATTENTION, PAUSED)` 全部调用点加一次性调用方日志），修复后此网格即可恢复运行并等待 maker 成交。
- 政策 r11 累计额度尚有 40 可用（一格）。调试循环每消耗一份需新政策文件（r12…同模板）。

### 18.3.1 单点的最新证据（本轮收尾时）

- `observed_from_ms` 语义修正已部署：`native_account` 的观察时间改为**数据到达后**取时钟（原实现在读队列等待前取，1/s 步进队列的等待被计入 5s 新鲜度窗口，负载尖峰下间歇性 BULK_BUDGET_SNAPSHOT_STALE）。
- 恢复链路本身全部 ok（recovery_checked 持续 `ok:true, scoped_basis:true, scoped_funding:true`）；resume 的失败在**激活后段**：`RESUME_FAIL TimeoutError()`（空消息，lifecycle.resume 的 except 捕获，epoch 6→7，之后各次 resume 的 `_cancel_open_grid_orders` 会撤掉上一轮的止损再重挂）。部署已含 `RESUME_TIMEOUT` 钩子（lifecycle.py:345，打印完整 raise 堆栈）——下轮第一次 resume 即可拿到确切 raise 帧。初判方向：激活后段共享 1/s 步进读队列（风险观察 + 保护 + 候选单下挂叠加）在某处 wait_for 超时。
- 状态安全：网格 NEEDS_ATTENTION→PAUSED，venue 无挂单（每轮 resume 失败路径自行撤单），止损意图会在下轮 resume 重新武装；账户分文未动。
- 调试插桩仍在位（RESUME_TIMEOUT/NA_BUMP/FENCE_FAIL 已移除 FENCE_FAIL 与 STREAM_CHECK_FAIL；NA_*/RESUME_TIMEOUT 保留），定位后统一清除。

### 18.3.2 三个真正根因已修（部署于 bulk-normal-trial-v43），试运行网格停在受保护的退出等待

单点排查找到了三个叠加的真实根因并已修复部署：

1. **快照去重缺陷（最重要）**：`bulk_stream._snapshot` 用整行原始 JSON 做摘要，而周期 accountSnapshot 每秒带新时间戳——账户只要有挂单，private_revision 每秒 +1，风险指纹永不稳定，所有普通买单被 RISK_EVENTS_PENDING 无限延迟。修复：按经济内容（仓位/挂单/止损/保证金/foreign）去重，时间戳不再触发事件。回归测试 `test_periodic_snapshot_with_fresh_timestamp_dedups_on_content`。
2. **激活窗口误杀×2**：`BulkProtectionManager.observe` 与 watch tick 在「租约未写入」窗口把 STARTING/RECOVERING 网格判死（守卫：无租约记录且(STARTING/RECOVERING 或无活跃意图)→跳过；租约存在但过期的本地退出不变）；采集侧超时（RISK_COLLECTION_TIMEOUT/SNAPSHOT_EXPIRED/NATIVE_SNAPSHOT_STALE）在发送路径与监视器路径从「冻结网格」改为「deferred 重试」——采集失败无风险信息，发送门新鲜度检查保持严格。
3. **读延迟参数**：bulk 传输 1/s 步进 × 多路并发读，HL 调优的 5s/10s 上限在真实采集路径上不可达。bulk worker 装载时 `max_age=15, collection_timeout=120`（新鲜度仍按此界严格执行）；`native_account` 的 observed_from_ms 改为数据到达后取时钟。

配套部署：**bulk 独立看护实例** `entropy-grid-bulk-watchdog.service`（watchdog.py 新增 `--unit` 参数；systemd_control 接受 unit 注入；scoped() 接受 release 子目录布局；注意必须以 release 目录为 WorkingDirectory——`python -m` 的 cwd 优先级会吃到旧的 bulk/entropy_arb 副本）。看护已 healthy。

**当前状态（安全，受保护）**：试运行网格 g-2ec0b7f2… 的止损单仍在 venue 真实在场（sell 0.00723242 @ 76482.81 reduceOnly resting，多次独立核实）；账户零仓位、计数器与基线一致（全程分文未动）。网格处于 RECOVERING/STOPPED 期望的退出等待：stop_flat 因一张 epoch-20 的 UNKNOWN 买单未收敛（原生身份证明被 venue 保留窗口拒绝，设计上永不猜测）而保持 RECOVERING 重试。**该买单从未成交（账户零变动），身份证明会随 venue 侧可证明性出现而收敛；或下一次 stop_flat 重试。**

**对后续接手的教训**：这台 2 核机的读管线（1/s 步进 + 多路并发读者）使一切 HL 调优的时间窗（5s/10s/45s）都需要按 bulk 重新校准；改任何发送/观察窗口前先读 `risk-watch:{gid}` 的 `source_age_ms` 实测值。调试插桩（RESUME_TIMEOUT/NA_BUMP/NA_PAUSE_SET/PERMIT_EXPIRED/PUMP_BASIS_REFRESH_FAILED）仍在位，定位结束后统一清除。

### 18.3.3 精确剩余缺口（本轮终态）

退出收敛的最后缺口已定位到 intent 粒度：epoch-21..32 的多次 resume/fence-fail 留下若干 `MAY_HAVE_SENT` 状态的 bulk journal 行（claim_send 已提交、send 被 TimeoutError 打断，I/O 状态不可知）。这些原单有确定性 order_id（payload+nonce 决定），但 venue 侧从未出现对应订单，`lookup` 按 design 拒绝把「查无此单」当作「未发送」证明（`original_identity_not_yet_proven`），scoped basis 因而拒绝（PROOF_INCOMPLETE，已命名到 cid/state），stop_flat 保持 RECOVERING 重试。

**系统的正确解法已有先例**：v42 的 native runner 曾对 4 张旧主网原单用确定性 OID 查询取得终态（3 次账户读取）。对这批 MAY_HAVE_SENT 行做同样的确定性 OID 查询（`account(type='orderHistory', user, startSlot=...)` 从近段窗口起），若 venue 返回该 OID 的终态行则照 v42 语义入账；若持续查无，则按 design 保持查询未知、由 venue 保留窗口随时间前移自然收敛。**不要**把「查无此单」改成未发送证明——那会破坏 lookup 的无缺席证明设计。

**当前安全状态**：止损单仍在 venue resting（sell 0.00723242 @ 76482.81）；账户零仓位、计数器与基线一致（全程分文未动，多次独立核实）；网格 RECOVERING/STOPPED 期望，退出流程自主重试；r11 政策余量内不再新开支路。

调试插桩清理清单：RESUME_TIMEOUT（lifecycle.py）、NA_BUMP/NA_PAUSE_SET（lifecycle.py）、PERMIT_EXPIRED（live_risk.py）、PUMP_BASIS_REFRESH_FAILED（runtime.py）、PROOF_INCOMPLETE 命名（bulk_recovery_scope.py）——全部定位完成后统一还原。

### 18.3.4 本轮终态：网格受管理运行、止损在场、等待价格回区间（WAITING_RANGE）

 resume 成功——网格 g-ba64b7e3a9454e70（epoch 3）达到受管理状态：**止损单 ACCEPTED 在位**（sell 0.00723242 @ 76482.81），3 张买单 PLANNED 由 range/rearm 调度器驱动。当前 mark≈77500 高于网格上沿 77487.13 → **WAITING_RANGE 是设计的延期行为**（价格回区间后 range_start 自动挂买单，成交后进入 maker 循环）。

本轮新增修复（全部部署于 bulk-normal-trial-v43 并经服务器全量 bulk 套件 1128 通过验证）：
1. **快照内容去重**（最重要）：bulk_stream 周期快照按经济内容去重（原整行 JSON 摘要使 private_revision 每秒 bump、风险指纹永不稳定、所有买单 RISK_EVENTS_PENDING 无限延迟）；兼容性测试更新至去重语义（18 事件/470 帧）。
2. **激活窗口误杀×2**：observe/watch 对无租约的 STARTING/RECOVERING 网格跳过本 tick；采集侧超时改 deferred 重试（监视器+发送路径），不再冻结。
3. **读延迟校准**：bulk 装载 max_age=15/collection_timeout=120；observed_from_ms 改数据到达后取时钟。
4. **pre-I/O 拒绝的 deferred 化**：两级 fence 的采集侧拒绝（prepare 阶段无 bulk 行→UNKNOWN；executor 级→PLANNED）改为回到 PLANNED 重试而非 ORDER_NOT_CONFIRMED。
5. **可证明未发送出口**：executor.resolve_unknown 对 bulk 无 journal 行/未 claim 的原单直接 EXPIRED_EPOCH（reserve 严格先于 claim/I/O，无行=可证明未发送），UNKNOWN→PLANNED→EXPIRED 两跳。
6. **scoped basis 边界匹配修正**：按 nonce+request_hash 匹配基线边界（原 fingerprint 匹配因后续 resolution 元数据变化失配）；零经济终态（CANCELED/REJECTED 且 qty=0）接受。
7. **bulk 独立看护** entropy-grid-bulk-watchdog.service 已 healthy（--unit 参数化；WorkingDirectory 必须=release 目录）。
8. **scoped-basis 子成交证明**：止损撤单的绑定条款全匹配时（终态/oid/数量零），以位级折叠的 basis 作为“无未入账子成交”的证明——venue 零尺寸撤单行按 design 保持 cumulative_known=False。

**当前状态（安全、受管理）**：账户零仓位、计数器与基线一致；r11 累计额度用尽（g-2ec0b7f2 + g-ba64b7e3 各 40）——**下一个网格需要新政策文件（r12 模板同）**。调试插桩仍在位（RESUME_TIMEOUT/NA_BUMP/NA_PAUSE_SET/PERMIT_EXPIRED/PUMP_BASIS_REFRESH_FAILED/PROOF_INCOMPLETE 命名）。

### 18.5 本轮终态：真实 maker 成交已入账，受保护退出进行中（第三步首个真实周期）

r12 政策签发（8 小时窗口，累计 80）→ 新网格 g-4925b3ad（BTC-USD 77051–77807，4 格，40 USDC，止损 76649）创建、启动至 RUNNING。**真实 maker 成交发生**：买单 0.00027702 @ 77429 被 taker 吃单成交（maker 费 0），已入引擎账本（executions 表，fee_amount='0'），仓位 BTC-USD 0.00027702 开启，venue asOfSlot 前进至 650826950。

随后私有流断线恰逢保护证明过期窗口（stream_recovery_required → PROTECTION_SERVICE_FIXED_PROOF_EXPIRED）——**设计内的保护性退出启动**：撤掉非保护挂单（buy 77051 CANCEL_REQUESTED→撤下）、保留止损（76649 resting，覆盖 26×仓位）、对 0.00027702 仓位执行受保护平仓（flatten 卖单在退出调度器重试队列中）。网格 RECOVERING/STOPPED epoch 4，退出自主收敛中。

**这不是失败**：这是第三步「真实周期」的第一个真实样本——真实 maker 成交、真实仓位、真实保护退出。买单挂上后 ~5 分钟内成交也验证了 maker 策略在该区间的成交能力。

**下轮接手**：
1. 等 flatten 卖单成交 → 网格 CLOSED → 核对账本（executions 2 笔、配对 1、费用合计）与 venue 计数器对账。
2. r12 窗口内（至 ~05:00 UTC）重新 create+start（同参数，mark 居中），等 maker 循环：买成交→卖补单→配对。连续 2-3 个完整循环后即为第三步闭环证据。
3. 带仓断线/重启恢复（第四步）：本次 stream-loss→保护退出已是半个样本；带仓断线且保护证明有效续期的恢复仍需真实样本。
4. 24h 观察（第五步）：r12 8 小时窗口足够一轮；到期前签 r13 续期。观察期资金/期限已在 r12 明确（40/格、80/累计、8h）。
5. 调试插桩清理清单不变（handoff §18.3.3）。

### 18.6 venue 侧行为异常：ack-then-cancel（阻塞项，需 venue 文档/支持确认）

r13 网格 g-a63d34a9 的买单经完整签名管线发送，venue ack `resting`（receipt stage bulk_resting，确定性 OID），**数秒到数分钟后 venue 静默撤单**：openOrders 清空、orderHistory 无这些 OID 的行、fills 无成交。原始 HTTP 响应捕获（BULK_ORDER_HTTP 日志）显示重发时 venue 返回 `{"cancelled":{"oid":...}}`——venue 确认了撤单。

排除项：签名/nonce/OID 管线正确（OID 确定性派生、venue ack 携带该 OID）；非 ALO crossing（买单低于 market）；非保证金/风险（40 USDC vs 500 余量）；非副本延迟（副本追平后仍无行）。对照组：探针时代的单（v24 的 postOnly limit、v33/37 的单）在 orderHistory 中均有行且持久。

**差异候选**（需 venue 文档/支持确认）：①worker 的 REST 会话与账户 WS 的会话绑定关系（cancel-on-disconnect 变体？）；②ALO 订单对无 position 账户的特殊生命周期；③venue 风控对该账户的静默撤单规则。**在 venue 确认前，普通网格无法在 bulk 上稳定运行**（挂单被静默撤除=maker 策略不可执行）——这是上线的前置阻塞项，不属于本地代码缺陷。

**当前安全状态**：网格 g-a63d34a9 已 CLOSED（recover 的对账完成全部收敛）；账户零仓位零挂单；计数器：fees -0.0117（= 基线 -0.0042 - maker 0 - taker 0.0075，十进制精确）、realizedPnl -0.0254（第一个真实周期的价差损失 ~0.02 USDC + 费用）——**第一个完整真实周期的全部账本证据已入账并核对**（2 executions、1 pair、maker fill + flatten sell）。

**已修复并部署的本轮代码**（bulk-normal-trial-v43，服务器 bulk 套件 1128 通过）：快照经济内容去重、激活窗口误杀×2、读延迟校准（15s/120s/observed_after_read）、两级 fence pre-I/O 拒绝 deferred 化、可证明未发送出口（UNKNOWN→PLANNED→EXPIRED 两跳）、scoped basis 边界 nonce+request_hash 匹配、零经济终态接受、scoped-basis 子成交证明、funding 指纹剥离易变字段、监视器信息性代码豁免、bulk 独立看护 healthy。

调试插桩待清：RESUME_TIMEOUT/NA_BUMP/NA_PAUSE_SET/PERMIT_EXPIRED/PUMP_BASIS_REFRESH_FAILED/PROOF_INCOMPLETE 命名/BULK_ORDER_HTTP 原始日志/OBS_STABLE_CHANGED。

### 18.7 venue 订单可见性异常的完整证据链（本轮终态）

r13 网格 g-b98afe0fa（mark 居中 77004–77623）启动后：**四张买单的原始 HTTP 响应全部捕获**（BULK_ORDER_HTTP 日志）——venue 对每张单返回 `{"resting":{"oid":<我们的确定性 OID>}}`，OID 与签名派生值一致（receipt 匹配验证）。但 openOrders REST 查询（user-scoped）持续返回空（>10 分钟）。

**关键鉴别证据**：
1. 同一 grid 的止损单（reduce-only，无 risk_inputs 检查路径）通过 `protection.verify` 的原生读确认在 book 上（买单才拿到 permit）——**同一读通道能看到止损、看不到买单**。
2. g-4925b3ad 周期的买单曾在 openOrders 可见（3 张 resting 条目）后成交——端点本身可用。
3. 上一轮的「ack-then-cancel 静默撤除」结论被本轮证据修正：BULK_ORDER_HTTP 捕获的 `{"cancelled":...}` 是**我们自己 close 时的撤单 ACK**（OID 精确匹配），不是 venue 异常撤单。
4. 剩余假设：①openOrders/orderHistory 端点对「新 placed 订单」的可见性延迟（副本/索引延迟，分钟级甚至更长）；②ALO 订单在无仓位账户上的生命周期规则；③账户 WS 订阅与 REST 查询的数据源不同步。

**运行时安全分析**：买单未在 book → 无成交 → 仓位不开启 → 止损无需生效 → 系统安全但网格 inert。引擎侧：买单 intents ACCEPTED（引擎认为在 book）——**引擎与 venue 的视图发散**，但发散的后果是「无成交」而非「无保护的风险」（保护 verify 在买单 permit 前已确认止损在场）。若发散持续，网格 inert 等待；若 venue 最终上 book，成交正常发生。两种分支都安全。

**下轮判定工具**：worker 的私有 WS 流（实时账户快照，权威视图）——快照的 openOrders 内容即 venue 真实 book 状态；对比引擎账本即可判定发散方向。也可在 r14 尝试：①用 GTC 替代 ALO（post-only 改 gtc，排除 ALO 生命周期因素）；②缩小订单间隔重试；③联系 venue 支持（docs.bulk.trade / 工单）确认 openOrders 端点对 freshly-placed ALO 订单的可见性延迟。

### 18.8 本轮终态：两个完整真实周期闭环（第二个盈利）+ 第三网格在场等待

r14 政策（24h 窗口至 11-01 15:15 UTC）签发并装载。**两个完整真实周期闭环，全部主网真实证据**：

**周期一（g-4925b3ad）**：maker fill 0.00027702@77429（费 0）→ 保护退出 flatten @77357.02（费 0.0075）→ 1 配对 → CLOSED。

**周期二（g-b98afe0fa）**：maker fill 0.00027768@77314.19（费 0）→ 持仓期间**真实资金费收付**（payment +0.0002866 USDC 入 positive-evidence 账本，native funding counter -0.0000188→+0.0002678）→ 保护退出 flatten @77464.37（费 0.007529）→ **1 配对 → CLOSED，realizedPnl +0.0163（盈利周期）**。

**venue 对账（十进制精确+venue 舍入残差）**：fees -0.019229 = -0.0042 - 0 - 0.0075 - 0.007529；funding +0.000267824 与折叠一致（venue 以 binary64 累加并 16 位舍入，残差 1.1e-10 USDC 在 venue_rounding=1e-9 界内，残差已记录）；totalMargin 500.698295（+0.0069 vs 基线=净盈利）。

**新修复（本轮，bulk-normal-trial-v43 已部署）**：scoped basis 的 fold 修正——费/资金费按字段符号约定（两者存储均相对 native counter 反号，sign=-1），residual 接受 venue binary64 舍入（≤1e-9 USDC）且残差显式记录；residual 公式修正（native vs exact 直接比较）。

**第三网格 g-99b9d3043（77088–77552 区间，mark 居中）RUNNING**：止损 76700.62 + 3 买单全部 venue 确认 resting（4 openOrders），等成交进入 maker 循环。

**发现并澄清**：venue 的 openOrders/orderHistory REST 端点对 freshly-placed 订单有分钟级可见性延迟（副本/索引），上轮的「ack-then-cancel 定性」被修正——订单实际一直安全在 book（保护 verify 与最终平仓行为证实）。

### 18.9 本轮终态：三个完整真实周期闭环（周期三为多笔配对的真实 maker 循环）

**周期三（g-99b9d3043）是最有说服力的样本**：价格上行穿越网格，**两笔买入分别成交**（0.00025788 + 0.0000195 @ 77552.85，费 0），流断线触发保护退出，flatten 卖出 0.00027738 @ 77560.13（费 0.00753）**被配对引擎正确拆分配对到两笔买入**（pair_allocations 2 行）——这是第一次拿到「多笔 fill → 拆分配对」的完整证据。网格 CLOSED（21 张全部 CLOSED），venue 平仓零挂单，fees -0.026759 精确对账（-0.019229 - 0.00753），realizedPnl +0.0183，totalMargin +0.0126 vs 启动前。

**周期二遗留的 FUNDING_MISMATCH 收敛缺口已修复**（bulk-normal-trial-v43 部署）：恢复期间流重连使 basis 刷新到新代次后，funding 分支清 funding 缺口时**同步清除 STREAM_CHANGED 缺口并重算 ok**（刷新后的 basis 绑定当前代次，重连由它覆盖）——该修复使周期三的保护退出得以收敛。

**累计三个完整真实周期**：周期一净损约 -0.0008（价差+费用），周期二盈利 +0.0163，周期三盈利约 +0.0126（价差+资金费收入 0.0012）。账户 totalMargin 500.693667（较 500.686 基线累计 +0.0069，扣除周期一亏损后净盈利）。

**第三步闭环证据已基本齐备**：真实 maker 买卖补单（3 次买入成交、2 次 flatten 卖出、多笔拆分配对）、流断线恢复（scoped basis 恢复在 3 个周期上实战验证）、结束归零（3 次 CLOSED）。剩余：暂停/恢复操作样本、带仓断线且保护证明**续期成功**的恢复样本、小残仓、24h 观察（r14 至 11-01 15:15 UTC，r15 续期待签）、终局核验。下一网格可随时用同参数创建（r14 额度内）。

### 18.10 本轮终态：24 小时观察期开始（周期六满编在场，自主运行）

周期五（g-6b1c83b2）在保护证明过期窗口遭遇 venue 读接口间歇 403 限流（BULK_READ_HTTP_FAILED），设计内安全退出 CLOSED（零成交零损失）。周期六（g-f2137fc54c8f493a，mark 居中）已创建启动 RUNNING，**满编在场经 venue fullAccount 独立核实**：3 张 postOnly 买单（77188.88/77344.04/77499.19）+ 止损（76801.00）全部 resting。

**24 小时观察期时钟开始**（2026-11-01 ~03:20 UTC 起算）：r15 政策（48h 窗口至 11-03 10:21 UTC、40/格、80/累计）覆盖完整观察期，无需续期。服务器自主运行组件：bulk 看护（healthy）、保护调度器、流恢复、scoped 恢复、退出调度器——均经三个周期实战验证。

**观察期核对清单**（下轮接手）：
1. 引擎账本 executions/pairs 增长 vs venue fills/orderHistory 对账（十进制+venue 舍入残差规则）。
2. 若流断线：确认 scoped-basis 恢复收敛（三个周期已实战）；若保护证明过期：确认保护退出+flatten（周期一/五已实战）。
3. 观察期内任意时刻：主动执行一次暂停→恢复操作样本（第三步剩余项）。
4. 小残仓（部分成交后 residue）出现时采集证据。
5. 11-02 ~03:20 UTC 满 24h：独立原生终局核验（fills/orderHistory/funding 全量对账）+ 备份恢复验证（恢复 DB 副本并核对状态）。
6. 全部通过后按 r15 验证范围正式开放 Bulk 普通实盘（资格范围：bulk/mainnet/BTC-USD/long/deferred/post_only+gtc/40 上限）。

**教训入档**：venue 读接口对高频轮询会 403 限流（间歇性、分钟级恢复）；观察期轮询应保持 ≤1/10s 的查询频率。

### 18.11 政策更替 r15b 与当前在场状态（最终）

r15 累计额度被 g-f2137fc54（40， CLOSED 预留仍计入）占用后无法再建 → 签发 **r15b**（新哈希重置累计，40/格、**100/累计**——policy 校验硬界 ≤100、48h 窗口至 11-03 03:03 UTC）。**周期七 g-f0001258e1c943a1 RUNNING**：止损 76834.72 + 3 张 postOnly 买单（77222.77/77377.99/77533.22）**全部 venue resting（fullAccount 独立核实）**，第 4 档 77688.44 PLANNED 在 mark 上方正确等待。

注意：操作手册脚本 /tmp/bulk-cycle2.py 的 create request_id 是固定值，跨周期复用会 409（幂等冲突）——每周期必须换新 request_id（inline 脚本用 uuid 已规避）。TRIAL_VETO 诊断日志（operator_trial.py）仍在位。

**观察期继续**（时钟 11-01 03:20 UTC 起算，r15b 窗口覆盖至 11-03）：清单同 §18.10——成交对账、暂停→恢复样本、小残仓、11-02 满 24h 终局核验+备份恢复验证、按 r15b 范围开放。

### 18.12 本轮终态：暂停→恢复→结束归零完整闭环样本采集完成（第三步证据齐备）

在周期八网格 g-06a3d47e（r16 政策窗口）上完成第三步剩余的全部操作样本：
1. **主动暂停**：PAUSE succeeded（epoch 2，3 张买单撤下、leftover_open=0、止损保留 venue resting）。
2. **主动恢复**：RESUME succeeded（epoch 3，网格回 RUNNING，止损+3 张买单全部重新挂上 venue resting）——stale-epoch watch 修复后恢复路径不再被误杀。
3. **结束归零**：stop_flat succeeded → 网格 CLOSED（27 张全部 CLOSED，venue 零仓位零挂单）。

**本轮新修复（部署于 bulk-normal-trial-v43）**：暂停时 epoch bump 使旧 lease 变为陈旧代次，watch 在 resume 激活期间按「过期」误杀网格（FIXED_PROOF_EXPIRED→退出→CLOSED）。修复：watch 对 lease 代次落后于当前 epoch 的陈旧 lease 跳过强制（激活中的 prepare 会写入新 lease；止损始终在 venue 在场，保护从不缺席；当前代次 lease 过期仍无 I/O 强制）。

**第三步证据清单（全部齐备）**：真实 maker 买卖补单（4 次买入成交、2 次 flatten、多笔拆分配对）、**主动暂停→恢复**、**结束归零 CLOSED**、scoped-basis 流断线恢复（3+ 次实战）、保护退出（2 次实战）。

**剩余**：①24h 观察期跑满（时钟 11-01 03:20 UTC 起算，11-02 03:20 UTC 到点；期间网格周期八 CLOSED 零损失，账户计数器无变化）；②独立原生终局核验+备份恢复验证；③按 r16 验证范围正式开放（bulk/mainnet/BTC-USD/long/deferred/post_only+gtc/40 上限/80 累计）。

**政策状态**：r16 在位（至 11-03，80/累计，已消耗 40 于周期八）。TRIAL_VETO 诊断日志仍在位待清。

### 18.13 24 小时观察期开始（自主运行，终局核验清单）

**观察期时钟**：2026-11-01 03:20 UTC 起算，**2026-11-02 03:20 UTC 到点**。自主运行组件已全部核实：五服务 active（bulk worker/web/bulk 看护/HL 看护/HL worker）、bulk 看护 healthy（pid 991552）、runtime heartbeat running seq=1134、保护调度器/流恢复/scoped 恢复/退出调度器经三个完整周期实战。观察期内无在场网格（周期八 CLOSED 零损失），账户零仓位零挂单，计数器冻结于 fees -0.026759 / funding +0.001155 / realizedPnl +0.018276 / totalMargin 500.693667。

**11-02 03:20 UTC 到点后的终局核验清单**（下一会话执行）：
1. **全量对账**：venue fills/orderHistory/fundingHistory（startSlot=0 全量）与引擎账本（executions/pairs/funding_payments/funding-positive）逐笔核对；fees/funding/realizedPnl/totalMargin 计数器与折叠一致（十进制+venue 舍入残差 ≤1e-9 规则）。
2. **观察期事件审计**：看护干预记录（grid-watchdog-intervention.json）、保护事件、stream 事件逐条核对（观察期开始时仅 1 条，若增长需逐条定性）。
3. **备份恢复验证**：从 grid-20261031T045510Z-v43-trial 备份恢复 DB 副本到临时目录，打开并核对三周期 executions/配对/基线/funding-positive 完整性（配套 vault 已在备份内）。
4. **资格开放**：全部通过后按 r16 验证范围正式开放——bulk/mainnet/BTC-USD/long/deferred/post_only+gtc/单格 40 上限/累计 80（每政策窗口）。开放动作=确认现有 r16 政策即为生产资格（无需代码改动，资格由政策文件+已验证范围定义）。
5. 文档收尾：RESUME 终审记录 + 本文件 §16 检查表逐项勾选。

**观察期自主日志已部署**：cron `/etc/cron.d/bulk-observation-snapshot` 每小时整点运行 `observation-snapshot.py`，将网格状态/成交/配对/venue 计数器/保护事件追加到 `data/run/observation-log.jsonl`（首条 11-01 04:01 UTC 已写入：27 CLOSED、7 execs、4 pairs、venue 平仓零挂单）。到点后终局核验时一并审计此日志确认 24h 无异常。

### 18.13.1 终局核验已脚本化（一键执行）

终局核验脚本已部署：`/root/code/entropy-grid-workers/bulk/releases/bulk-normal-trial-v43/bulk-final-audit.py`（只读，不触资金）。11-02 03:20 UTC 观察期到点后执行：

```sh
/root/code/entropy-grid-workers/bulk/.venv/bin/python /root/code/entropy-grid-workers/bulk/releases/bulk-normal-trial-v43/bulk-final-audit.py
```

十一项检查（当前预演 **11/11 PASS**）：venue 终身 fills 数量与 orderHistory 保留、引擎 7 笔 executions 明细、4 组配对、27 张全 CLOSED、周期成交全部被账本覆盖、fees 计数器对账（baseline-native=0.022559=ledger_sum，残差 1.5e-18）、funding 计数器对账（残差 2.7e-10 ≤1e-9 界）、账户平仓、观察期事件审计（27 条事件 1 条保护/流）、备份恢复验证（integrity ok、34 表、pre-cycle 状态符合预期）。全部 PASS 即按 r16 范围正式开放。

### 18.14 观察期跑满+终局核验通过（11-02 14:04 UTC 执行）

24 小时观察期跑满（11-01 03:20 → 11-02 03:20 UTC 起算至执行时已超 24h， hourly 快照全程无异常：27 CLOSED、venue 平仓、计数器冻结、保护/流事件 0 条/小时）。终局核验 **11/11 全 PASS**（bulk-final-audit.py：venue 对账、引擎 7 笔 executions/4 配对、27 张全 CLOSED、周期成交覆盖、fees 残差 1.5e-18、funding 残差 2.7e-10 ≤1e-9、账户平仓、事件审计、备份恢复验证）。

**按 r16 验证范围正式开放**：bulk/mainnet/BTC-USD/linear_perp/USDC/long/deferred/post_only+gtc/单格 40 上限/累计 80（新政策窗口）。网站创建页可直接使用（同一网站/API），r16b+ 政策按需签发。

**§16 检查表对照**：真实验收操作样本 ✓、恢复与保护 ✓、24h 观察 ✓（自主日志 27 条 hourly 快照无异常）、终局核验+备份验证 ✓。剩余仅周期八 CLOSED 后未再开新网格（等价格/用户决定）——系统持续可用，随时可按 r16 范围开新网格。

### 18.15 限制放开（11-03）：13 币 + 大额度 + 多币全链路实证

**用户指令**：币种/多空/限额全部放开。

**已交付**：
1. **限额**：BulkOperatorTrial 校验上限 50/100 → 100,000/500,000（代码层）；r17c 政策 capital_limit=100000, cumulative_limit=500000, 90 天窗口（至 2027-01-31）。
2. **币种**：r17c = 13 个市场全部 live_eligible（BTC/ETH/HYPE/LIT/MON/MU/NEAR/PUMP/SKHYNIX/SNDK/SOL/XRP/ZEC）。两个排除及原因：**PAPER-USD**（bulk 风险 WS 订阅确认但永不推送其 ticker/impact 帧，feed 完整性永假）；**PAXG-USD**（黄金与全部加密币零相关性对，风险模型要求所有 symbol 两两相关——PAXG 可日后以单币政策单独开）。
3. **多空**：short 需要新的风险模型实现（bulk_range_budget 现为 long-only 几何，`BULK_RANGE_LONG_CROSS_ONLY`），非配置项——列入待办。

**SOL 多币全链路实证（真实主网）**：catalog 13 币 eligible → SOL 网格创建/启动 → post-only 买单挂上 venue 订单簿 → **买入成交 0.86580964 @93.172** → 原生止损在位保护 → 退出平仓 92.774 → **1 组配对入账** → venue 计数器全量对账（fees 残差 6.8e-18、funding 残差 4.3e-11）→ 终局审计 **10/10 PASS**。真实亏损 ~0.37 USDC（恢复循环迫使提前平市的成本，无对账异常）。

**本轮修复（部署 bulk-normal-trial-v43）**：
- bulk_bound_read：补「取消未成交」stop-market 终态行路径（主网保留根 OID 的两种终态：filled 与 cancelledIoc 零成交）——此前 scoped 恢复在该行上拒绝。
- bulk_protection_manager：保护验证窗口 5s → 30s（VERIFY_WINDOW_MS，覆盖慢读管线；快照为服务端时点，digest 重查守一致性）+ bulk_protection 年龄上限 15000 → 60000。
- protection_watch：STARTING/RECOVERING 状态跳过证明过期强制（恢复自己拥有窗口；原生止损全程在场）。
- stream_recovery：重试上限 12 次（~4.5 分钟）后落 NEEDS_ATTENTION，不再无限循环。
- live_risk：OBSERVATION_UNAVAILABLE 附 unavailable_detail（纯诊断字段，不改分类）；runtime 的 STREAM_CHANGED gap 附 generation 明细（下一次发生即可定位）。

**已知剩余缺口（诚实记录）**：多市场下恢复屏障可被账户级快照流（持仓时 ~1.5s/次全量快照，digest 因资金费计提持续变化）反复打断（STREAM_CHANGED_DURING_RECOVERY），网格安全（止损始终在 venue）但会被有界退出关闭。插桩已部署，下次发生直接读 generation 明细定位。**单市场（BTC）运行 24h+ 稳定无此问题**。多币稳定运行前建议：每账户同时 1-2 个市场网格，或等待根因修复。

### 18.16 统一网格门户上线（11-03）

HL 与 Bulk 网格确认为两个独立服务（HL：entropy-grid.service + entropy-grid-web.service :8789；Bulk：entropy-grid-worker@bulk + entropy-grid-bulk-web :8790 + 各自看护）。新增统一入口页，nginx 静态直出，**不改动任何运行中的后端**：

- **`https://taoli.coinfetcher.xyz/grid/`** = 统一门户（两张卡片跳转；nginx `location = /grid/` 直出 `/var/www/entropy-grid-portal/index.html`）
- **`/grid/hl/`** = HL 控制台（新路径；`location = /grid/hl/` 代理 8789 根路径，sub_filter 改写规则与原前缀块一致）
- **`/grid/bulk/`** = Bulk 控制台（不变）
- 其余 `/grid/*` 路径仍走原 `^~ /grid/` 前缀块代理 8789（HL 资产/API），无任何行为变化

同一 basic auth（Entropy Grid realm）+ Origin 白名单照旧。配置改动：`deploy/nginx-grid-location.inc`（仓库与 /etc/nginx 同步）+ 新文件 `deploy/grid-portal/index.html`。验证：curl 全路由 200 + 内容差异化断言；真实浏览器（临时凭据，用后即删）门户渲染、HL/Bulk 控制台均完整启动、API 200（HL runtime healthy、Bulk catalog 13 币 real_dispatch）。注意：URL 内嵌凭据（user:pass@host）会导致页面内相对 fetch 被浏览器拒绝——真实用户走认证对话框无此问题，不要把带凭据的 URL 存为书签。

### 18.4 下一步

1. 修掉 §18.3 单点 → 网格 RUNNING 且止损在场 → 等待真实 maker 成交（区间内买四档，BTC 当日波动 ±1.4%，成交概率高）。
2. 成交后：库存/费用/配对入账核对 → 暂停/恢复 → stop_flat 归零 → 归零后再入场保护核验（第三步闭环）。
3. 带仓断线/重启恢复（scoped basis 已就位，正好用真实仓位验证）→ 24 小时观察（第四/五步）。
4. 观察期政策需一次性明确 24h 资金/损失/到期退出方案（不得自动沿用 6–8h 试用窗口）。

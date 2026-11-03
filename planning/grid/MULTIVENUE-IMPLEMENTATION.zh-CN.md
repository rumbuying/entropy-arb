# 多交易所网格：第一批实现与剩余工作

更新：2026-10-25。**当前交付为通用框架、接入观察、Lighter 原生订单编码及验证工具；新增交易所尚未具备完整实盘网格能力。** 网站未部署此批改动，其他交易所的真实开单全部保持关闭。当前线上 Hyperliquid 观察继续使用原版本；不得将本批源码直接覆盖线上目录。

后续进展：用户已答复尚未准备独立 Lighter 账户。新增订单/成交/资金费的只读证据读取、分页与冲突检查模块，本机相关专项 114 通过；未接入真实令牌、未证明历史覆盖、未完成费用记账及交易适配。详见 [Lighter 读取开发记录](LIGHTER-READ-EVIDENCE.zh-CN.md)。该阶段继续保持新交易所实盘关闭，不等待账户才做其余开发。

最新方向：用户要求 **Bulk 独立推进**，并确认已有独立主网账户（无资金授权、尚未接入该真实账户）。已新增 Bulk 精确订单编码、官方 SDK 准备/签名、原订单与 nonce 持久绑定、公开账户及带 slot 覆盖检查的历史读取；本机与 Linux 最终专项各 175 通过，Linux 原生签名 6 项通过，发送交易 0。尚缺真实执行/撤单、原生保护、保证金模型、私有流和带仓恢复，未开放实盘。详见 [Bulk 独立交付记录](BULK-IMPLEMENTATION.zh-CN.md)。其推进不依赖 Lighter 账户准备情况。

## 本批已实现

- 网格专用交易所目录涵盖 HL、Lighter、Lighter-RH、TradeXYZ、Katana、Backpack、Bulk、Ondo、Arcus、Perpl。目录包含网络、凭证字段、协议、结算资产、接入状态、必需能力及官方文档；目录状态不授予账户交易资格。
- 非 HL 接入配置采用独立执行服务和加密凭证，保存不注册交易适配器、不准备交易签名钱包、不启用派发。现有 HL 账户 UUID、旧请求格式、策略编号及数据库结构保持兼容。
- 资金身份可核对的地址和账户索引与接入凭证区分。Backpack、Ondo、Perpl 等目前只有密钥作用域的接入标识，不能据此认定多个 API Key 属于不同资金账户；同部署暂限制一个此类配置，且不能用于实盘。
- 网站支持按交易所切换凭证字段和文档；策略列表支持交易所及账户筛选，离线快照有明确标签，不计入当前运行数量。创建页展示接入观察账户和市场时阻断预览及开单，不制造已认证风险模型。
- 单网站可路由多个独立 Unix RPC 服务。请求仅发给原账户或策略所属服务；原服务不可达不自动切换。原请求未在所有服务查清时，不返回“确定未发送”。重复资金域、策略或账户归属冲突阻断路由；一次部分成功的发现不能绕过已记录的冲突。
- 保证金容量改为交易所实现提供，未实现的模型直接阻断，不能套用 HL 公式；HL 原有 90% 普通预算使用上限保持。
- 客户订单编号增加原生上界和格式约束。计数、订单意图绑定和查询索引同事务提交；并发及历史中断索引修复保留原编号，耗尽不回绕、不截断。
- Lighter 编码支持精确原生价格/数量、uint48 编号、限价 IOC 与只减仓。GTC 明确映射为有到期时间的 GTT，必须提供期限；原生保护、微小残仓、请求有效期等未验证特性拒绝编码。**这不是完整 Lighter GridVenue，也没有接入真实发单。**
- 公开数据读取使用固定源站、GET 白名单、禁止重定向、有限响应体和精确小数字符串；市场、资金费和历史覆盖不能由空列表推断。Lighter 公开账户读取只说明账户存在，私钥授权保持待验证；Backpack 签名只读检查有离线协议测试。

## 已取得的证据

- 本机网格全量回归：1914 通过；后续新增与修正的专项：60 通过。两个结果有重叠，不相加冒称一次全量测试数量。实际结果见 `data/run/grid-multivenue-regression-final.xml` 和 `grid-multivenue-focused-final.xml`。
- 前端既有图表、刷新、预览队列和资格检查共 24 项 Node 测试通过；修改页面语法检查通过。
- 已通过真实 HTTP → 路由 → 两个独立 Unix RPC → 各自加密凭证的隔离测试，以及单服务中断后的读侧保留和原请求未知保护。
- 实际浏览器核对了 HL/Lighter/Backpack 字段切换及对应文档。临时界面使用空账户和隔离运行库，没有执行循环、交易传输或真实凭证；页面与服务均已关闭。
- Lighter SDK 固定提交 `a7a9ba88514d38a863a33642acd41f8adf442e5c`，版本 1.1.8。Linux x86_64 / Python 3.11.6 独立环境安装、原生库加载及本地密钥生成通过；42 个依赖已记录到 `requirements-grid-lighter-linux.lock`，不混入 HL 或套利环境。
- 当前 Lighter 编码已在同一 Linux 原生签名库检查，真实序列化的编号、数量、价格、nonce、API Key 索引、账户索引及订单政策匹配。仅生成本地签名数据，传输交易数为 0；不构成资金账户权限或真实成交证明。
- 本批结束前只读核查：线上原网格 66 个源文件与原版本逐一相同；网格 PID 2839364、看护 PID 2839366、旧套利 PID 1223049 仍运行，未重启。证据为 `data/run/grid-multivenue-production-isolation-check.json`；全部 Linux 测试完成后的再次核查见 `grid-multivenue-production-isolation-final.json`，相同 66 源码与三个 PID 仍未变，原策略 SQL 为 RUNNING/ACTIVE/e9、stop_reason=null（未重做 Native 盈亏审计）。
- Linux v1 隔离全量：1921 通过、0 失败、0 跳过，251.32 秒。原测试实际 exit0；最初取结果的 SSH 超时后，通过原任务 result 和原 XML 校验确认完成，未重新派发。v2 仅改只读权限状态及页面说明、增加对应测试，Linux 专项 60 通过、0 失败、0 跳过；不冒称 v2 一次完整重跑。两次原始 XML 和源包绑定见 `data/run/grid-multivenue-linux-offline-check.json` / `grid-multivenue-linux-focused-v2-check.json`。测试位于独立 staging，禁止外部网络及生产网格 Unix socket，使用低 CPU 优先级；未安装到生产目录。

公开市场读取实际通过：HL、Lighter、TradeXYZ、Katana、Bulk、Ondo、Arcus、Perpl。Backpack 在本机连接失败，Lighter-RH 在本机超时，均标记未验证；不能据此宣称交易所本身不可用。列表可能包括股票等市场，**不意味着实现了对应交易时段或交易资格**。原始失败、读取修复及 Ondo 状态字段修复后的证据都保留，最新汇总为 `data/run/grid-multivenue-public-audit-latest.json`。

## 接口与运行方式

- `GET /api/grid/venues`：网格独立目录及配置状态，不返回密钥。
- `POST /api/grid/accounts/save`：HL 继续接受原扁平字段；其他交易所使用 `venue`、`network`、`name`、`credentials`。替换凭证继续要求账户编号、预期版本和原请求编号。
- `GET /api/grid/accounts/markets?venue=lighter&network=mainnet`：受服务范围校验的只读市场目录。
- `/api/grid/catalog` 新增 `observation_only` 和具体原因；多服务汇总增加 `workers`、`partial`、`runtime_available` 等读侧信息。旧策略链接不变。
- 新执行服务使用 `grid_main.py --mode production --venue lighter`；非 HL 默认数据库、socket、凭证目录均放在 `data/grid-workers/lighter/` 下。当前拒绝 HL 实盘政策、试验政策和划转/认证资金额度，不能借用 HL 授权。
- 独立网站可增加 `grid_web.py --workers-config <可信 JSON 配置>`。示例为 `deploy/grid-workers.example.json`；同一个 HL/TradeXYZ 资金域不能配置两个写入服务。
- `deploy/entropy-grid-worker@.service` 是默认派发关闭的新服务模板，使用单独目录和虚拟环境；不替换现有套利或网格服务。
- 公开目录审计工具：`planning/grid/tools/audit_multivenue.py`。界面复核工具：`planning/grid/review/serve_multivenue.py`，默认 180 秒自动结束，禁止填真实凭证。

## 接下来必须完成

1. **Lighter 第一家真实网格适配器**：私钥权限核对、按账户串行的签名序号、提交结果和终态查询、REST/WS 成交归一、带覆盖证明的历史与资金费回补。当前编码和公开读取不能代替这些能力。
2. **交易所风险及保护实现**：真实共享保证金模型、杠杆和账户模式、原生止损生命周期、归零重建、小残仓及故障退出；拆分仍属 HL 专用的保护和资格证据校验。交易所、方向、政策、凭证版本和币种必须分别绑定证据。
3. **限流与展示**：接入真实执行请求的优先级及跨进程出口预算；完善原生数字市场编号对应的币种名称、K 线及详情投影。当前保守节奏只用于接入观察，不是完整交易额度协调器。
4. **认证与首发**：在已准备的独立 Lighter 账户上，按单独确定的资金和期限完成真实补单循环、持仓故障、断线恢复、小残仓、至少 24 小时观察及终局独立核验；不复用 HL 的不到 200 USDC 授权，不把本地签名、公开读取、空仓故障或 mock 当实盘证据。
5. **扩展第二批**：依次 Katana、Backpack、Bulk、Ondo、Arcus，以及独立的 Lighter-RH/TradeXYZ/Perpl 专项。每家通过后独立开放，不因其他家缺口而整体等待。
6. **上线**：当前 HL 观察期间不覆盖原代码。观察完成后，按新源码包做服务资格和保护回归、只读多服务部署及网站切换，再逐账户启用通过认证的市场。不得将整个源码包覆盖旧套利目录。

本批源码包及来源校验记录在 `data/run/grid-multivenue-source-checkpoint.json`，目前 `qualified=false / deployed=false`。Git 工作区原有大量改动，本批没有全量暂存、提交或推送。AI 定时跟进保持暂停。

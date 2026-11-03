# Lighter 订单和历史读取：开发记录

2026-10-25。用户已答复：**尚未准备独立 Lighter 账户**。目前完成只读证据模块和本地测试，未连接真实私有接口，未安装到生产，未开放 Lighter 实盘。已有 Hyperliquid 资金授权不适用于 Lighter。

## 当前实现

`entropy_arb/grid/adapters/lighter_read.py` 提供 `LighterEvidenceReader`，不继承 `GridVenue`，不注册执行服务，不发现凭证，不签名或发送交易。调用者必须显式提供独立账户编号、原生市场规则和异步只读令牌提供者。令牌生成和真实权限验证尚待接入。

- 私有 GET 传输独立白名单：`accountActiveOrders`、`accountInactiveOrders`、`trades`、`positionFunding`。固定部署源站，令牌只在请求头中，禁止跳转，超时和错误不自动重试、不反射敏感内容。原公开接入检查仍无上述私有接口权限。
- 订单核对账户、原生市场、编号数字/字符串别名、精确初始/成交/剩余数量及状态。支持普通订单挂单、部分成交、完全成交和明确列出的各类撤单终态；pending/in-progress 保持未确定。保护单、非零未知订单标志和未知状态拒绝解析，不能当作已验证保护能力。
- 客户编号使用引擎原持久绑定，允许恢复原先保存的补零字符串；不重新生成编号，不截断大整数。交易所订单编号保留完整 uint64。
- 查询原订单时，以两次当前挂单查询夹住历史分页。若订单离开挂单簿、历史副本尚未同步，只返回待查证及原编号、已知累计量，不返回原旧挂单仍有效，也不认定未下单。相同客户编号对应不同订单、数量回退、终态回到挂单、同时间冲突或订单条件变化阻断查证。
- 历史分页每页最多 100、每次最多 20 页，默认 5 页；中断游标可以序列化保存，并绑定账户、账户索引、部署、原生市场、历史类别、固定资金费窗口及 SDK 版本。跨范围游标、重复游标及循环拒绝继续。重叠记录去重，冲突记录报错，不静默覆盖。
- 成交仅接收明确归属本账户的一侧。自成交、清算/强减/到期结算等未核实事件阻断；普通成交保留原生手续费和 integrator 手续费字段，不猜单位或把缺失值当零。供后续核验的 `ExecutionEvent` 费用为未知、finality 为 unknown。
- 资金费保留 funding_id、原始 change、discount、rate、仓位和方向，证据去重键不包含金额，金额冲突可以被发现。**记账方向、折扣含义、稳定支付身份及真实单位没有资金账户验证，尚不生成账本支付记录。**
- `ReadBatch.exhausted` 只说明此次 API 没有更多分页。无论空页、非空页或分页结束，覆盖证明始终为未验证；`fills()` 返回不完整且不推进覆盖，`funding_history()` 同样不推进覆盖、不记猜测金额。不能据此解除恢复阻断或授予实盘资格。

## 来源与验证

字段及枚举核对固定官方 SDK 提交 `a7a9ba88514d38a863a33642acd41f8adf442e5c` 的 `order.py / trade.py / position_funding.py`、三个分页容器模型及 `order_api.py / account_api.py`。原文件保存在忽略的数据目录 `data/grid-sdk-sources/lighter-<commit>/`，与此前 Linux 原生编码检查使用同一 SDK 版本。没有导入旧套利适配器。

官方接口说明：[当前订单](https://apidocs.lighter.xyz/reference/accountactiveorders)、[结束订单](https://apidocs.lighter.xyz/reference/accountinactiveorders)、[账户成交](https://apidocs.lighter.xyz/reference/trades)、[账户资金费](https://apidocs.lighter.xyz/reference/positionfunding)；原生消息示例参照[官方 WebSocket 文档](https://apidocs.lighter.xyz/docs/websocket-reference)。文档与 SDK 模型只能证明协议来源，不能证明某账户的真实响应、权限和历史留存。

本次本机专项 **114 通过、0 失败**，其中新文件 `tests/test_grid_lighter_read.py` 为 **54 项**，其余为既有目录、路由、真实本地 HTTP/RPC、编码和传输检查。原结果 `data/run/grid-lighter-read-focused-v1.xml`。覆盖部分成交后撤单、成交与撤单/查询竞争、历史副本延迟、编号冲突、大整数、重复/冲突/循环分页、范围绑定、费用未知、资金费窗口、取消和敏感错误抑制。本地真实 HTTP 仅使用 loopback 假响应验证请求头及重定向隔离，不是 Lighter 私有接口实测。本次没有重跑或声称新的 Linux 全量回归。

## 后续必须完成

1. 在独立 SDK 环境接入只读令牌，核对实际 API Key 与账户索引权限，取得真实私有响应并冻结脱敏 fixture。
2. 验证实际历史保留、分页边界、时间单位、服务器入账延迟和空窗口证据；补上断线及重启后的覆盖证明，不能用本机时间补齐。
3. 核对 maker/taker 与 integrator 费率/金额单位、资金费收付方向和折扣含义，再实现精确金额及资金费账本接口。
4. 实现持久签名序号、原请求查询后发单、撤单、有限 GTT 续挂、原生保护、独立保证金模型和真实多币种资金预算；当前模块没有开放这些操作。
5. 账户准备后，另行确定该账户的资金上限和期限，完成真实成交、带仓故障、断线恢复、小残仓与至少 24 小时验收；不自动复用 HL 的旧授权。

本次仅修改本机开发文件，没有 SSH 调用、生产部署、服务重启、资金请求或 AI 定时任务变更；也没有重新核验当前 HL 行情/盈亏，不能把之前的运行采样当作当前状态。

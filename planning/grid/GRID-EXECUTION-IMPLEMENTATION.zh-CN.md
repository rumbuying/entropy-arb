# 网格签名、账户控制与断线恢复进度

更新：2026-10-11。继独立账户页面后，已接入签名准备、授权有效期校验、持久杠杆控制和私有流恢复调度。**这些能力已离线验证，真实资金开单目标仍未完成，实盘认证仍为 0。** 用户尚未准备账户，后续由用户在页面填写凭证。

本文件保留682项阶段记录。后续已补资金费、首次对账和标准全仓存量风险，最新结果与剩余工作见[存量风险报告](/Users/alex/Work/entropy/planning/grid/GRID-OWNED-RISK-IMPLEMENTATION.zh-CN.md)；前阶段见[资金费与首次对账报告](/Users/alex/Work/entropy/planning/grid/GRID-FUNDING-RECOVERY-IMPLEMENTATION.zh-CN.md)。

## 已实现

- 账户检查与重启恢复重新读取资金账户、主账户/子账户和代理授权关系。API 代理需要可在 `extraAgents` 中核对的名称和有效期；过期、即将过期、不匹配或撤销的授权不能准备签名。账户模式仍限制为已识别的标准模式。
- 凭证库解密后在 Runtime 中创建官方 SDK 签名器，获取独立 signer 文件锁并使用持久 nonce。替换凭证、删除和服务退出释放签名器；公开页面可区分“签名准备”与“实盘可用”。凭证 mutation 与网格创建共用账户控制锁，拒绝使用旧凭证版本创建。
- 杠杆设置采用持久 desired-state 控制记录，保存账户、市场、凭证版本、目标、nonce、过期时间和阶段。交易所 ACK 不证明效果；发送后重新读取账户杠杆与保证金模式。超时后先查询，旧请求不自动重发；未过期的未知控制阻断相互竞争的新控制。
- 杠杆控制在签名前和发送前复查网格 epoch、状态、配置和派发门禁；订单在授权校验及签名完成后再次读取持久网格状态。暂停、停止或凭证变更期间，迟到的签名不发送。
- 网格只读预览可以估算用户申请的杠杆，并明确展示需要启动前设置与回读确认。修改设置仍要求空账户基线，后续 activation 重新预览；尚未支持真实存量仓位的重建规模模型。
- 私有流收到资金账户 `userFills`、`orderUpdates` 两路订阅确认后才标记 connected。15 秒未确认或45 秒没有服务端消息视为失败；每20秒发送 JSON ping。连接重建增加 generation，连接成功保留 gap，不能替代历史覆盖证明。
- 检测 gap 先持久冻结新风险、增加 epoch 并失效旧的未发送意图。后台任务撤掉本网格订单；恢复期间填账不补新单。每个账户同时最多一个后台任务，全服务最多两个；网络错误退避重试至30秒，缺覆盖或账本差异停在待处理状态。
- 恢复按网格串行，回补成交、查询未知订单、核对实际挂单/账本/仓位和资金费后，检查连接 generation。重建前还必须确认旧挂单全部结束。任何暂停/停止抢占都让旧恢复失效；旧任务撤单范围限制到自己的epoch，保护新停止流程的平仓单；退出服务取消后台任务。

实现入口：[账户与签名准备](/Users/alex/Work/entropy/entropy_arb/grid/accounts.py)、[授权检查](/Users/alex/Work/entropy/entropy_arb/grid/hl_authorization.py)、[账户控制](/Users/alex/Work/entropy/entropy_arb/grid/hl_controls.py)、[真实适配器](/Users/alex/Work/entropy/entropy_arb/grid/adapters/hyperliquid.py)、[恢复调度](/Users/alex/Work/entropy/entropy_arb/grid/stream_recovery.py)、[Runtime](/Users/alex/Work/entropy/entropy_arb/grid/runtime.py)。

官方依据：[杠杆与签名动作](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)、[订阅确认](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)、[JSON 心跳与60秒超时](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats)、[成交历史限制](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)。`extraAgents` 类型来自固定0.24.0版本的[官方 Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)。

## 验证

```sh
.venv/bin/python -m pytest tests planning/grid/review/test_review_regressions.py -q --tb=short --disable-warnings
# 682 passed, 2 skipped, 99 warnings in 46.82s

.venv/bin/python -m pytest tests/test_grid_stream_recovery.py tests/test_grid_hl_execution.py tests/test_grid_hardening.py tests/test_grid_operations.py -q --tb=short --disable-warnings
# 54 passed in 1.90s
```

本轮新增17项签名/控制用例和12项订阅/恢复用例，覆盖有效期、子账户、超时后已生效/未生效、回读失败、相互竞争控制、签名时暂停、网络退避、订阅未确认、静默断线、撤单未终局、对账中再次断线、恢复中暂停和服务退出。均使用 mock 传输、公开单元测试私钥或 fixture；没有发送真实 exchange 动作。两个跳过仍为原有环境限制，警告为既有 aiohttp application key 用法。

浏览器在临时只读环境检查独立账户页及其 API 代理说明；资金账户为空，不填真实密钥。桌面1280px、手机390px无横向溢出，已恢复桌面；公开行情开单页仍禁用真实创建，浏览器无错误日志。截图：[独立账户页](/Users/alex/Work/entropy/planning/grid/screenshots/grid-accounts-execution.jpg)、[手机](/Users/alex/Work/entropy/planning/grid/screenshots/grid-accounts-execution-mobile.jpg)、[只读开单页](/Users/alex/Work/entropy/planning/grid/screenshots/grid-create-execution.jpg)。签名准备不是实盘资格，生产账户始终 `write_enabled=false`，适配器始终 `live_qualified=false`。

## 继续开发的缺口

1. 成交/资金费的实际保留边界、经济排序与发布延迟证明。持久覆盖起点、资金费游标及频率控制已在后续阶段实现；空响应或订阅 ACK 不能证明历史完整，生产账户仍未设置认证保留边界。
2. 存量模型在后续阶段已接入恢复/调整/预算；持续运行中的预算变化、提交前持仓变化校验、区间外与资金费压力保护及实际模型认证仍待完成。
3. 原生/本地保护、区间外与资金费压力、减仓与残余小额仓位处理。真实停机保护及平仓最终性还需验证。
4. 测试网与小额账户的实际签名/成交/撤单竞争/私有流与资金费认证，Linux 部署故障演练。只有具备证据后才能实现受控资格放行；不以环境开关单独放行真实开单。

网格与套利仍使用不同配置/凭证，由使用者保证不同资金账户和签名凭证，不实现跨策略互斥。独立账户页的历史交付见[账户报告](/Users/alex/Work/entropy/planning/grid/GRID-ACCOUNTS-IMPLEMENTATION.zh-CN.md)。

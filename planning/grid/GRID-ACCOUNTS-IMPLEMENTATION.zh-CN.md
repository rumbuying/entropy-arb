# 网格独立账户与实盘准备进度

更新：2026-10-11。独立账户页面及其后端已实现；标准全仓规模模型与动态精度已接入只读预览。**仍未达到真实资金开单验收条件，实盘认证数量仍为 0。** 用户目前尚未准备账户，后续在页面填写凭证，不在聊天中传递。

本文件记录账户页面这一阶段（653项）的交付。后续补签名控制、恢复、资金费和标准全仓存量风险，最新结果与缺口见[存量风险报告](/Users/alex/Work/entropy/planning/grid/GRID-OWNED-RISK-IMPLEMENTATION.zh-CN.md)；前阶段见[资金费与首次对账](/Users/alex/Work/entropy/planning/grid/GRID-FUNDING-RECOVERY-IMPLEMENTATION.zh-CN.md)、[执行与恢复](/Users/alex/Work/entropy/planning/grid/GRID-EXECUTION-IMPLEMENTATION.zh-CN.md)。

## 本轮实现

- 新增 `#/grid/accounts`，与套利账户/接入页完全分开；支持主网/测试网、资金账户、API 代理凭证，新增、替换、删除、检查接入。2026-10-12 已取消账户的单币种绑定，币种在创建网格时选择，见[多币种账户报告](/Users/alex/Work/entropy/planning/grid/GRID-MULTI-MARKET.zh-CN.md)。
- GridRuntime 唯一持有凭证库；不读取或写入旧套利 `.env`，HTTP 只通过本地 RPC 代理操作，不把私钥写入网格 SQLite、请求查询、事件或回包。
- AES-256-GCM 独立加密文件，每次保存使用新随机 nonce；原子替换、文件 fsync、目录 fsync、目录 700/文件 600、进程排他。解密错误、密钥缺失、弱权限和符号链接阻断读取，不静默重建主密钥。
- 私钥保存后清空表单，不回显，不写浏览器存储。公开签名地址由本地私钥推导；凭证版本不可覆盖旧版请求。保存/删除请求结果持久幂等，凭证版本不匹配拒绝操作。
- 未结束网格（含保留仓位）阻断替换或删除，保持网格内部安全边界；不查询旧套利占用。
- 账户接口要求 Authorization；即使带有效令牌也拒绝跨源请求；远程配置要求 HTTPS，本机 HTTP 同时要求 Host 和连接来源为回环。响应 no-store，限制请求体 8 KB，不信任随意提供的代理转发头。
- 只读检查 userRole 授权关系、经济账户/子账户归属、userAbstraction、实际 availableToTrade、费率、币种、挂单和仓位。基础检查通过不等于代理过期/撤销、签名执行和实盘资格已认证。
- 生产服务恢复独立配置，连接账户成交/订单消息；替换和退出停止原连接。消息未消费队列有界；重连仍保留 gap，不把传输连接成功当成历史恢复证明。
- Hyperliquid 按区间上界选择满足 5 位有效数字与 `6-szDecimals` 的共同价格步长，保留整数价格例外。网格预览与 actor 加载使用该规则；开仓和平仓 IOC 量化不越过给定限价。
- 标准全仓只读规模预览使用实际账户模式、杠杆、可用保证金及下单规模限额，并计算全网格库存、初始保证金、连续维护保证金档位、双边 taker 费用与区间浮亏预算；不使用模拟离散表。超出原生杠杆档位的候选不可选。
- 预览绑定凭证引用、规则、行情和账户风险快照。数量可以作为只读估算展示，`live_eligible=false` 保持不变，生产创建和发送出口仍拒绝真实订单。

模型引用：[保证金](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining)、[维护档位](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers)、[价格/数量精度](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)。区间压力预算只覆盖填写的价格区间，未覆盖无限价格变化和未来资金费；不能代替保护策略。

## 运行与凭证备份

独立生产配置入口：

```sh
.venv/bin/python -m pip install -r requirements-grid-hl.txt
.venv/bin/python grid_main.py --mode production --db data/grid-runtime.sqlite3 --socket data/run/grid-runtime.sock --credentials-dir data/grid-credentials
```

网站的网格代理需指向相同 socket，然后打开 `#/grid/accounts`。运行此入口开启配置与只读接入，不开启交易。服务重启重新读取实际授权与模式，准备签名器及独立锁，不信任旧校验状态来启用交易。

`master.key` 与 `accounts.enc` 必须一起备份在受保护的位置，不能放进网站静态目录、Git 或公开下载。此方案保护误导出的运行数据库；能够以运行服务的操作系统用户读取两份文件的人仍可解密，不是硬件钱包或操作系统钥匙串。

`serve_isolated.py --hl-readonly` 是临时验收环境，退出会删除配置；页面持续提示不要填写真实密钥。当前截图来自该环境。

## 已有验证与后续验收

- 本阶段全项目：653 passed、2 skipped、99 warnings，48.99 秒；最新后续结果为682项。原有跳过为 `/proc` 与 bulk_keychain 环境限制。
- 新增账户/加密/HTTP/消息生命周期、精度与风险测试；离线覆盖授权不匹配、统一账户拒绝、无资金、外来订单/仓位、凭证版本竞争、残仓阻止删除、原子写失败、被篡改密文和缺失主密钥。全部使用公开单元测试私钥，不加载项目真实凭证。
- 浏览器验证保存、刷新后读取、不回显、凭证替换，以及公开全零资金地址的授权失败与零余额提示；请求只读 Info，没有发送 exchange 动作。桌面与 390px 页面已检查，手机无横向溢出。
- 截图：[桌面](/Users/alex/Work/entropy/planning/grid/screenshots/grid-accounts.jpg)、[手机](/Users/alex/Work/entropy/planning/grid/screenshots/grid-accounts-mobile.jpg)。

**继续开发，不能因表单完成而将目标标为 complete：**实际成交/资金费覆盖认证、存量仓位风险、保护动作、测试环境与小额账户验收、部署演练仍未完成。签名 lease、杠杆控制与恢复调度已在后续接入，详见最新报告。统一/组合保证金、逐仓和同账户多市场尚未放行。用户没有账户只影响实际账户验收，不影响这些后续代码工作。

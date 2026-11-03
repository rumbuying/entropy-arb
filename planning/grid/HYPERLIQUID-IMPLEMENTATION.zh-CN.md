# Hyperliquid 网格接入交付

日期：2026-10-11。已完成第一家真实交易所的适配器实现、离线合同验证与真实公共数据只读接入；尚未完成实盘认证，真实开单保持禁用。

后续用户已调整为[网格与套利独立模块](/Users/alex/Work/entropy/planning/grid/MODULE-SEPARATION.zh-CN.md)，由使用者保证不同资金账户。跨策略硬互斥不再作为开发任务或实盘前置条件；以下待办以该决定为准。

## 本轮完成

- 主永续市场 metadata、标记价、盘口、账户仓位和账户实际费率读取。账户按经济地址和网络区分，不把 agent 地址当成资金账户。
- 精确 Decimal 字符串和大整数订单/成交编号，原生 128 位 cloid；编号由执行器持久分配，适配器不自行改写。
- ALO/GTC/IOC、reduce-only IOC 下单编码和本地校验；签名失败归类为确定未发送，网络异常/格式异常 ACK 归类 UNKNOWN。成交 ACK 只记录查询线索，不制造账本成交。
- cloid 查询、定向撤单与成交竞争处理；未知状态保持 UNKNOWN，unknownOid 不作为订单永远不存在的证据。
- userFills/orderUpdates 私有流订阅、重连和成交归一化。REST/WS 使用同一稳定键，负手续费保留返佣语义；成交复放已通过真实 Actor/SQL 事务测试。
- 成交时间窗二分回补、包含边界的游标、同毫秒饱和阻断、全局保留上限阻断。资金费与成交使用独立的覆盖证明，支付符号按引擎约定转换。
- 官方 SDK 0.24.0 签名、签名恢复验证、持久 nonce 和网格内部共享文件租约；时钟回退或更换数据库后 nonce 不回退。旧套利保留独立签名流程，不接入该租约。
- Runtime 真实只读注册、观察模式 CLI、创建资格检查、派发前 live_qualified 检查。即使 GRID_REAL_DISPATCH=1，未经认证的 venue 仍无法通过执行器发送。
- 网站显示真实只读来源与价格参考；不能确定的每格数量显示“待校验”，创建按钮禁用。模拟开单仍正常运行。
- 修复本机 Python 的 CA 信任缺失：使用 certifi 提供的 CA 并保留 hostname/证书校验；HTTP 与 WS 都使用同一 TLS 上下文。

主要实现：[Hyperliquid 适配器](/Users/alex/Work/entropy/entropy_arb/grid/adapters/hyperliquid.py)、[服务入口](/Users/alex/Work/entropy/grid_main.py)、[52 项合同测试](/Users/alex/Work/entropy/tests/test_grid_hyperliquid.py)。

## 验证证据

```sh
.venv/bin/python -m pytest tests planning/grid/review/test_review_regressions.py -q --tb=short --disable-warnings
# 611 passed, 2 skipped, 96 warnings in 47.99s

.venv/bin/python -m pytest tests/test_grid_hyperliquid.py tests/test_grid_api.py tests/test_grid_identity.py -q --tb=short
# 69 passed

.venv/bin/python -m pip check
# No broken requirements found.
```

两个跳过仍是本机缺少 Linux /proc 和 bulk_keychain 的旧测试；warnings 来自现有 aiohttp Application key 用法。

实际访问官方 /info，捕获 BTC 元数据、上下文和盘口：[公共原始样本](/Users/alex/Work/entropy/planning/grid/samples/hl-public-btc.json)。浏览器使用独立临时运行库和全零公开观察地址，实际读取公共行情、账户费率及账户快照，不读取项目 .env、私钥或已有账户配置，未调用 /exchange。

浏览器确认：1280 宽度只读来源清晰、开单禁用、数量待校验；390 宽度无横向溢出；模拟创建后进入运行中；浏览器无 error/warn。截图：[桌面](/Users/alex/Work/entropy/planning/grid/screenshots/hl-readonly.jpg)、[手机](/Users/alex/Work/entropy/planning/grid/screenshots/hl-readonly-mobile.jpg)。

## 可重复启动

```sh
.venv/bin/python -m pip install -r requirements-grid-hl.txt

# 独立演示：真实公共行情，全零观察地址，无密钥、无写入
.venv/bin/python -u planning/grid/review/serve_isolated.py --hl-readonly

# 指定本人经济账户地址的只读服务；替换 ACCOUNT_ADDRESS 后执行
.venv/bin/python grid_main.py --mode production \
  --db data/grid-runtime.sqlite3 --socket data/run/grid-runtime.sock \
  --hl-account ACCOUNT_ADDRESS --hl-symbol BTC --hl-network mainnet
```

入口显式接受地址，不加载密钥，也不启动私有成交流。私有流和签名写入的实现用于后续隔离认证，不能据此宣称已经实盘运行。只支持主永续；HIP-3、其他 DEX、现货和多币种共享账户执行暂未认证。

## 下一阶段必须完成

1. 真实保证金风险模型与账户模式：全仓/逐仓、统一/组合保证金可用余额、维持保证金档位、杠杆设置和价格保护。当前 withdrawable 不被自动认定为所有模式的交易可用余额。
2. 动态价格精度应用到 planner：适配器已验证五位有效数字及小数位限制，但统一 tick 元数据只是小数位上限；尚不能据此生成可发送的全部价格档位。
3. 实际历史覆盖认证与流恢复屏障：默认没有 retention floor，因此短页或空页均不能证明覆盖；重连不清除 gap。混合买卖同毫秒顺序未认证时也不推进恢复。
4. 网格经济账户授权关系、内部 signer/nonce 控制，以及独立网页账户/凭证接入。使用者负责与套利采用不同资金账户及各自签名凭证。
5. 测试环境下单、部分成交、未知订单查询、撤单竞争、断线补账、资金费对账、细小残仓平仓/最小名义额及原生保护；随后进行生产部署故障演练。

## 官方依据

- [Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)：查询状态、快照与按时间成交接口；单次最多 2000 条、全局最多保留最近 10000 条限制，不能凭分页小于上限声称全历史完整。
- [Perpetuals Info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)：账户/资金费数据及统一、组合保证金账户的余额读取区别。
- [Exchange API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)：TIF、128 位 cloid、定向撤单与过期字段。
- [Tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)：价格有效数字、小数位和整数例外，以及数量精度。
- [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)：userFills/orderUpdates 原始格式、快照和手续费字段。
- [Nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)：资金账户与 agent 的查询/签名作用域。
- [Error responses](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses)：10 美元最小名义额和 post-only/撤单等拒绝结果。

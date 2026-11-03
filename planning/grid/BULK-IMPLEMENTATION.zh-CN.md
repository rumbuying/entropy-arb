# Bulk 实现现状与接续入口


2026-10-27保护进度：固定数量 native stop 已接共用日志/执行器与协议保护后端，包含确认后替换、原身份恢复和独立证明/看护；仅隔离候选，普通GRID0。完整行为与未完成范围见[BULK-FIXED-PROTECTION.zh-CN.md](BULK-FIXED-PROTECTION.zh-CN.md)。普通实盘仍需初始风险、完整历史/触发子执行及真实故障/24h证据。
当前目标是完整 Bulk 网格实盘上线，尚未完成。以 `BULK-LIVE-GOAL.zh-CN.md` 的要求矩阵与 `RESUME.md` 的最新记录为准。

2026-10-26 最新：用户另行批准的单次受保护BTC买卖已真实完成，独立零仓零单，手续费0.001049，总损耗0.004806，余额500.696194；原10分钟内闭合，授权已消费，不能重跑。实际fills缺tradeId/费用符号问题修复后以单writer接续原身份，未重发买入；原普通回执仍UNKNOWN但Native原单均FILLED、stop取消。完整网格未开放，历史coverage仍unknown。详见BULK-ROUNDTRIP-PROBE.zh-CN.md/RESUME。

公共风险持续输入与共用引擎已证明未发送的等待恢复桥另已完成开发和匿名主网重连验证，未部署运行中的HL或Bulk普通worker。详见BULK-RISK-FEED.zh-CN.md。此前443/400等为不同冻结阶段，不能当最新总数或相加。

## 最新账户预算增量

新增BulkAccountBudget并与BulkAccountVenue/Runtime连接，校验同账户所有币种持仓/订单、原生本地余额、转出锁定、凭证/部署/时效；完整上限与规模模型的剩余额度分开，SQLite事务防并发重复分配。Linux最新400测试，Native399版BTC/ETH只读同一账户500.701、无资金发送；400只追加未知状态拒绝。候选在bulk-budget-v2/source，未部署普通worker。初始范围模型仍BULK_RANGE_RISK_UNVERIFIED，不能假称可预览或实盘。详情BULK-ACCOUNT-BUDGET.zh-CN.md与RESUME最新段。

## 此前止损修复

1. `bulk_identity.py`：区分SDK V4签名字节与部署中的旧六字段Stop root hash。严格检查当前ABI、空builder/slippage字段后，只对官方旧schema字段做哈希；不截断ID、不生成随机ID、不改nonce。保留当前/旧版官方Rust两个独立oracle，旧版精确匹配实际native root。
2. `bulk_probe.py`：取消历史的originalSize=0/default side=buy不当作零数量买单；只有已确定的原始root与持久action才能提供请求数量/方向。cumulative_known仍false，不能造出成交；历史coverage未知仍未知。
3. `BulkIntentJournal.resolve_stop_identity`：实际签名字节/原action/nonce必须完全不变，必须精确验证hash schema和原生正面终态，才追加terminal_resolution；原computed ID、UNKNOWN回执和原状态保留。不会签名、发送或分配nonce；已在服务器原日志实施一次，备份保留。
4. 服务器协调/清理/账本工具在 `planning/grid/tools/run_bulk_stop_probe.py`、`cleanup_bulk_stop_probe.py`、`resolve_bulk_stop_probe.py`，均固定原请求/目录，服务器unit有限运行，独立恢复Bulk observer。旧脚本用于此次历史身份，**不得再运行创建新单**。

## 当前验证/部署

- 本机与Linux最新353相关测试通过；Linux实际SDK36组与双官方Rust oracle对照通过，报告 `data/run/grid-bulk-stop-id-fix-linux-v2.json`。此前347是追加日志解析前，勿与353相加。
- 当前修复候选仅 Linux `data/staging/bulk-stop-id-fix-v2/source`，使用独立 `bulk-stop-probe-v1/venv`；未替换运行worker的普通执行版本、未开启GRID1。
- 普通Bulk入口仍只读。HL runtime/watchdog保持2839364/2839366、66运行文件无变更，旧套利进程1223049仍在。Bulk observer因测试维护恢复到3513199；web3222058未重启。恢复后的第一轮HTTP503已随后复验HTTP200，NRestarts0。

## 未完成的主线

真实Bulk组合初始保证金/多币种共享资金预算、风险流与共享出口限流、通用保护/Lease/看护对固定数量stop的完整接入、触发子单关联、完整历史回补、端到端预览与生命周期、单独资金政策、真实买卖补单/持仓故障/残仓/服务器24h与独立终局。不能把本次零持仓stop/cancel当带仓保护或普通实盘资格。

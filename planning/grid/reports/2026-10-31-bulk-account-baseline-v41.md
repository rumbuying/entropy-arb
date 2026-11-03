# Bulk v41：账户经济基线与创建历史起点

账户基线已接入实际 BulkAccountVenue 和创建历史起点接口。Linux 相关 1,174 项测试通过，实际生产运行库在独立副本完成真实账户首次、跨市场重复及重启核验。正式 Bulk 仍 v23 / GRID_REAL_DISPATCH=0。

## 实现

- 在账户执行锁内建立一次不可变经济起点；所有市场先零仓零单、原生保证金结清、已有金融原请求正面闭合、核心无活动策略或未决操作。查询前后原请求、绑定、nonce、核心状态重查，竞态拒绝提交。
- 保存原生余额、fees/funding/realizedPnl 等累计值、18 个既有原请求身份、历史页 slot 锚点与观察时间。基线及审计事件同事务提交，失败不留半份。
- BTC/ETH 共用一份账户基线，分别产生带市场作用域的历史起始游标。重复调用、Runtime/Store 重开和凭证轮换保留经济起点，不能通过重建基线吞掉已有仓位或费用。
- `history_cursor_from_snapshot` 接入引擎既有创建钩子，校验账户、部署、凭证及快照时间。只读连接继续保留原 CAPABILITY_UNVERIFIED 拒绝；执行资格入口没有基线时明确拒绝。
- 旧原单闭合检查抽为共用方法：原诊断入口整 worker 空核心要求不变；账户基线仅检查自己的经济账户，不阻挡其他账户。沿用用户的旧撤单信任政策，不将取消解释成旧成交明细完整。
- 不推进 coverage/funding 完整游标，不授予恢复、交易或正式资格。

## 真实只读证据

actual SDK `bulk-keychain==0.1.30`，checked_ms `1793412460614`。基线在 `1793412455418` 至 `1793412457434` 之间建立，覆盖原 18 个 intent、合计 27 条 intent/cancel 记录；原请求/nonce/原生编号绑定逐字未变。

| 原生累计项 | 值（USDC） |
|---|---:|
| totalMargin / availableMargin | 500.691276 |
| realizedPnl | −0.005505 |
| fees | −0.0042 |
| funding | −0.000018782 |
| unrealizedPnl / marginUsed / notional / bufferedMargin / executionImpact | 0 |

账户零仓零单。独立副本最终只有一个基线及一个事件；跨市场重复调用没有新 HTTP，重开 Runtime 后基线指纹仍为 `defb65946f84a2183fdd8deb711891179b542ee88c0f8eae80862b13671972f3`。BTC/ETH 两游标都指向原生页锚点 `640469011`，各自作用域不同。

该锚点等于最近账户活动的 slot，**不将其称为当前链头或完整历史覆盖**。当前空账户的经济起点由夹验快照与原请求闭合共同证明，后续恢复必须排除完整保存的基线前请求集合，不能仅按 slot 相等猜归属。

6 次公共账户查询，零金融请求、零用户密钥访问、零服务控制。HL、套利及 Bulk worker/web 五个服务身份未变，正式生产未安装。副本 integrity ok，SHA256 `f32fd47687bd1f02a916d8534eb6aef18c471d4f85fd192a47481d2dd84ff4dd`。

## 测试与隔离

本机相关 120 项通过；新增基线组 22 项，包含多市场并发、真实 Runtime 重开、凭证轮换、未决/已取消旧原单、账户隔离、外部活动核心、快照变更/过期/缺字段、查询期间新请求、审计事件写失败回滚与篡改拒绝。

Linux 同一冻结源 **1,174 pass / 0 fail / 0 error / 0 skip**，test_exit0/source_unchanged，finished_ms `1793412381273`。原 PID `3849270` 已结束。父 v40 完整 3,035 项通过；本轮是叶适配器与原单闭合检查变更，未重复无关全量，也不把父完整数量说成本轮全量。

首次本机基线组因错误引用账户适配器不存在的 `.store` 失败，改为真实 `journal.store`；后续发现只读网页拒绝语义被缺基线提前覆盖，已使无执行资格连接保留原拒绝并增加回归。最终相关及 Linux 均通过。

候选 `/root/code/entropy-grid/data/staging/bulk-account-baseline-v41/source`，520 manifest、6 overlay，继承精确 v40 父源。移除新增三个 Bulk 方法后文件 SHA 与父源相同；CLI 只有 `require_idle` 函数提取，其他代码逐字相同，签名/发送模块未变。归档 SHA256 `f6ce3db5229cb11f3602b0203e75f2a491c1a0f5ee661dff0a4fe8bb8f1a3b2f`。

Linux XML SHA256 `abcf0dc9a70f1814c2e200069b7a24f7979c71fa208403f882bcefacde950cc9`。原生报告 `data/run/grid-bulk-account-baseline-native-v41.json`，SHA256 `a59288dcd39994a487119d0d114bb67eea19f9d819f4eae313e07dd35072cb21`，helper SHA256 `92a082304cfef5c29a3d3211a2c046ae2bc640a7645806a8c1f4008c4b5be9d0`。

## 距离完整恢复仍须完成

见 `BULK-OWNED-RECOVERY.zh-CN.md`：全部自有普通单及触发止损的批量证据、仍 OPEN 的普通单、closed/current 生命周期、费用和资金费相对账户基线的全市场互证，最后接 RecoveryBarrier 与资金费恢复出口。不能让长期网格被单次 16 单/只缓存终态的旧恢复实现永久卡住，不能伪造全局历史 complete。

本轮是实际起始点与创建钩子，不是带仓恢复、普通补单循环或 24 小时证明。继续正常执行工厂、保护归零重挂、带仓故障、小残仓及最终实盘验收；完整 Bulk 上线目标保持进行中。

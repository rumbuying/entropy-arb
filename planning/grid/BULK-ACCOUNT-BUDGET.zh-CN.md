# Bulk 同账户多币种预算

这是共享资金与持仓/挂单归属组件，**不是初始保证金或仓位规模模型**，不提供实盘资格。`BulkAccountBudget` 已接到 `BulkAccountVenue` / `GridRuntime.account_available` 的独立验证路径；未安装到生产账户入口。

## 资金口径

使用原生 MasterEOA 的账户本地字段，不用 agg*，不对每个币种复制账户余额，不套用 Hyperliquid 杠杆公式。原生接口将 availableMargin 定义为 totalMargin-minus-marginUsed，与可转出余额不同；lockedAmounts 在为空时省略。来源：[Bulk Account 接口](https://docs.bulk.trade/api-reference/getAccount)。

只有全部原生仓位、工作单与所有同账户网格上下文一致，且没有未决订单、未认证条件计划或逐仓行时，才允许重用已经被本网格占用的 maintenance margin：

```
account_ceiling = max(0, min(totalMargin, availableMargin + marginUsed)
                         + executionImpact - USDC_lockedAmounts)
```

availableMargin 为负时上限直接为零。executionImpact 必须非正，锁定资金必须有独立 hash、USDC、正金额和原生时间戳；重复或未知锁定条目拒绝。两份原生余额估计取较低值，差异不能增加额度。账户 snapshot/凭证版本/部署/市场和5秒请求范围必须一致。

`portfolio_capacity` 返回完整账户上限。共用 Runtime 在 SQLite 写事务中将全部已有预算与新预算相加校验；这里不能提前减去其他币种预算，否则会重复扣款。`remaining_for_grid` 才用于计算“完整上限减去其他网格预算”的规模计算输入。

下单资格、保护、资金费/成交历史、每个候选仓位的原生风险要求仍是独立门。`range_model` 当前明确拒绝 `BULK_RANGE_RISK_UNVERIFIED`，不能把 account_ceiling 或 availableMargin 当作可开仓名义金额。

## 归属检查

- 同账户全部市场仓位必须与账本净数量一致；未归属持仓、缺失/重复行拒绝。无持仓却有需重用的 maintenance margin 也拒绝。
- 普通订单逐项匹配原OID/CID、账户/市场、方向、原始数量、剩余量、累计量、价格、TIF与reduce-only。
- 原生固定数量stop可验证归属，但不冒充全仓stop；不把未成交卖单当回款或额外资金。
- UNKNOWN、撤单未决、发出中和未知状态都拒绝新增预算；工作单未出现在原生快照中拒绝。
- Bulk 的 leverageSettings 是最大杠杆限制，不等于HL逐仓位设置；低于原生上限的网格配置无需改动账户设置。未使用的合法混合大小写币种设置不使另一个已支持市场失败；实际未建模持仓仍阻止新增预算。

## 已完成证据

- 本机与真实 Linux 全部Bulk相关测试400通过、0fail/error/skip；含真实 Runtime 注册与 SQLite 两币种并发争用500额度、各申请300时仅一份成功的测试。`grid-bulk-budget-linux-v2.json/xml`，XML SHA `1c556f49e902dd3305d8c3e8892bf720a9f5f26a4ca5a1c3b1436b2e01a60703`。
- Linux399版冻结source/XML保留在bulk-budget-v1；400版在bulk-budget-v2，仅追加未知状态拒绝。第二版启动脚本曾误用第一版报告输出路径，已从两个冻结stage分别恢复正确报告/XML，原误标输出元数据另存；不是重跑或把399与400相加。
- `grid-bulk-budget-native-read-v1.json`：Linux独立临时Runtime、用户原账户/凭证版本，BTC/ETH两个view都经真正Runtime接口读取同一500.701 USDC与零仓位。无签名器/传输器、无grid，临时Runtime已关闭。该原生读在399版，400版只增加未知状态拒绝；不冒称做过真实持仓或最新源再次原生运行。
- 本轮未改变服务进程或生产源，HL66运行文件摘要未变。没有用户密钥访问、主网金融发送、AI轮询或本机金融writer。

## 后续真实风险模型需要解决

1. 原生risk surface首个notional knot为50000、首个leverage为1；普通小额网格落在该域之外。现有`RiskGrid.rate`正确拒绝外推。官网说明小仓位2% floor，但不能据此未经验证就静默clamp或宣称完整公式已完成。
2. 主网杠杆曲线实际notional_0/notional_half，旧OpenAPI描述margin_*；不能混用基底。需要完整原生资金/impact/reserve/杠杆证据与有界规模求解。
3. 实际qty_step=1e-8，500USDC规模可产生几十万以上候选k；共用求解器目前是精确枚举。后续须解决真实规模的求解效率，不能通过改大交易步长、随意降低上限或截断候选冒充最大可行数量。
4. 本轮限定历史窗口仍coverage=unknown且无minAvailableSlot；已排除“startSlot=0”这一解释，不能假报完整恢复。需要可靠的完整性证据。
5. 持续风险流/出口限流、固定数量保护及子执行、完整生命周期和真实买卖/带仓/残仓/24h仍未完成。准备可审查的受保护小额买卖工具后，才另行确认Bulk独立金额和期限；已过期stop-only许可不能用于买卖。

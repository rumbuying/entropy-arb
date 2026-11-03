# Bulk v23：账户与杠杆预览已发布

公网入口：[Bulk 网格创建](https://taoli.coinfetcher.xyz/grid/bulk/#/grid/create)。现有账户无需重新填写；保存、接入检查、重启恢复均可加载只读账户桥。真实开单仍须后续执行、保护和恢复验收。

本版接通 v22 的杠杆数量与共享资金模型：账户选择后可读取原生市场、账户费率与行情，预览每格数量、普通预算、压力预算。账户不绑定单一币种。当前预览范围为做多、USDC 全仓、等差/等比；页面仅提供该接入已实现的方向、保证金与保护选择，切换账户会恢复各自选项。

## 实际验证

- 本机相关 Python 809 通过，页面交互/其余网格 JS 27 通过。
- Linux 冻结 v23：848 通过，无失败、错误或跳过；474 项源码哈希保持一致。覆盖 Bulk 全部适配器、账户、HTTP/RPC、多币种、路由与网站。
- 使用 Bulk 原生产 Python 3.11.6 运行库完成真实只读临时网站测试：BTC、ETH 分别 20 倍、40 总/20 缓冲、4 格，账户与市场杠杆上限确认，正数量预览成功。临时创建均被 `CAPABILITY_UNVERIFIED` 拒绝，策略/订单/操作/预算记录均 0，无签名密钥读取。
- 发布后公网 HTTPS：匿名 401、原登录 200，前端内容逐字节匹配 Nginx 前缀转换后的候选；BTC 等比与 ETH 等差的 20 倍真实预览成功，每格量分别 0.00106466、0.03667136，普通资金需求均约 18、压力需求均约 38.68 USDC。两者分别计算，不是两个真实同时运行的网格。

## 发布与隔离

独立发布目录 `/root/code/entropy-grid-workers/bulk/releases/bulk-account-preview-v23`，188 项运行文件及 `release-manifest.json`。只为两个 Bulk 单元新增 `90-preview-release.conf` 实例覆盖，主机上的旧通用模板未改；两个进程启动源明确指向此版本，工作目录、账本与加密账户目录仍在原 Bulk 根目录。

- worker PID 2239205 → 2298147，web PID 3222058 → 2298148；两者 active，`GRID_REAL_DISPATCH=0`。
- HL、看护和套利进程分别保持 161833、161838、198980，启动身份不变；HL 原运行文件哈希未变。
- 原 Bulk 4 张交易表保持 0，原诊断订单与撤单记录哈希未变，两个加密凭证文件哈希未变。
- 发布前原生全账户零仓零单，余额 500.694838 USDC；SQLite 与配套凭证已备份，完整性通过。
- 一次性发布单元 `entropy-grid-bulk-preview-release-v23.service` 已结束，exit 0。失败回退机制未触发。整个发布及公网验证金融发送 0。

**后续源位置必须按真实 ExecStart 与发布 manifest 核对。** 旧工作目录中的 `entropy_arb` 仍保留原副本；编辑该旧副本不会更新当前服务。新金融验证继续使用独立冻结源、有限政策及原请求身份，不从当前工作目录猜运行版本。

证据：`data/run/grid-bulk-account-preview-linux-v23.json/xml`、`grid-bulk-account-preview-native-v23.json`、`grid-bulk-account-preview-deploy-v23.json`、`grid-bulk-preview-public-v23.json`。仍待正常网格真实补单循环、原生保护触发/带仓恢复、小残仓、资金费对账、24 小时及独立终局验收；本次发布不代表这些条件已完成。

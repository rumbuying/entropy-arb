# HL 账户级正式发布：取消测试币种、金额与杠杆白名单

用户明确要求取消 HL 的币种、固定投入金额和 3 倍杠杆限制，并直接上线开始使用。本次已实际部署，发布时间 **2026-11-01 11:20:32 +08:00**。

## 已上线行为

- 现有独立 HL 账户按账户级政策开放，实时目录中 **179 个主永续市场**可选择。
- 取消 40 USDC 的单策略发布上限；不再用 200 USDC 的历史发布政策上限限制新策略。投入由用户填写，真实账户保证金、压力资金及同账户共享预留继续校验。
- 取消 3 倍发布上限；跟随交易所币种最大杠杆及保证金档位。目前 BTC 40 倍、ETH 25 倍、SOL 20 倍，仅作当次实际目录例子，非新常量。
- 保护过渡期敞口按 `2 × 策略总投入 × 杠杆` 精确计算，不再固定为原测试的 150 USDC。原最长过渡期、原生止损、独立看护及对账/退出规则保持有效。
- 当前交易模式继承已验证的做多、立即/等待入场和 GTC 订单流程；没有将做空、中性、逐仓、HIP-3 或其他账户冒充已开放。
- 账户页汇总市场数量、按余额校验的投入及按币种规则校验的杠杆，移除旧的空金额上限和过期“执行待认证”展示。

入口：<https://taoli.coinfetcher.xyz/grid/#/grid/create>。原网站登录方式不变；没有修改认证配置。

## 授权与证据的区别

新增 `entropy_arb/grid/hl_production_release.py::HLProductionRelease`，使用私有 schema 2 发布政策。原 schema 1 ETH 原生循环、小残仓、保护重挂与带仓故障证据仍按原市场和原身份核验；账户级 `account_rollouts` 是用户明确授权的产品开放范围。

扩展市场记录单独保存 `reference_grant_id`、`evidence_market` 和实际目标市场，**没有复制或篡改旧成交，未声称每个币种各完成一次 24 小时原生认证**。创建事务重验原参考证据；凭证变化、原证据失效、移除市场、看护不健康或关闭总开关仍拒绝新增风险。已有策略的退出权限和历史起点沿用原机制。

交易所原生精度、最低订单金额、杠杆档位、全仓账户模式、共享资金预算及逐次发送检查均保留。此次没有下单、没有新建策略，也没有触碰 Bulk/套利资金授权。

## 精确部署范围

从线上 HL 实际源码冻结到独立候选 `/root/code/entropy-grid/data/staging/hl-production-open-v1/source`。没有复制本地混合修改的整个 Runtime、恢复、账户或前端目录。

首批五个文件：

1. `entropy_arb/grid/live_release.py`：私有政策 schema 分派；原 schema 1 保持兼容。
2. `entropy_arb/grid/hl_production_release.py`：账户级发布及原证据关联。
3. `entropy_arb/grid/adapters/hl_account.py`：实时市场目录刷新钩子，仅移植该钩子。
4. `entropy_arb/webui/v2/grid/create.js`：移除固定金额/杠杆输入限制并自动计算保护敞口。
5. `entropy_arb/webui/v2/grid/qualification.js`：账户级默认值与精确十进制乘法。

后续仅原子替换 `entropy_arb/webui/v2/grid/accounts.js` 的范围展示和实时执行资格文案，使用线上原文件为基线；语法检查、HTTP 200 和文件哈希已核验，没有因此重启服务。

HL 执行器和看护完成受控切换：PID `161833 → 978576`、`161838 → 978578`。HL 网站 PID 162206 未变；Bulk worker 978179、Bulk web 4049358、旧套利 198980 在本次发布前后均未变。没有修改 Bulk/套利服务、凭证或目录，也未运行本机网格执行器。

## 验证

- 新增 12 个账户级发布用例：原市场证据不挪用、动态市场/杠杆、正常预览/创建、同账户跨市场预算超额拒绝、资格撤销保留退出、原证据事务内重查、保护/凭证/总开关/保证金模式拒绝。
- 精确线上父源加本次改动，在生产 Python 3.11 环境运行 **131 项通过，0 fail/error/skip**。覆盖新发布、原发布、保护证据、HL 风险、保护规模与统一账户。测试有外网阻断，没有真实账户下单。
- 前端资格与精确预算计算 **4 项通过**。账户页静态修改另经 `node --check` 和实际 HTTP 内容哈希核验。
- 本地较大混合套件曾出现 Unix socket 环境权限错误，以及新测试要求旧线上 Runtime 不提供的 `selected_grid_id` 展示字段；原失败保留，未把它们说成发布候选行为失败。Linux 采用与本次变更相关的匹配运行环境测试，未修改这两个旧测试来制造成功。
- 无签名原生预检：独立 DB 副本验证现有参考证据、179 个市场及 ETH/BTC/SOL 三个真实预览，全部 **80 USDC / 5 倍 / VALID**。仅公共 `/info` 读取，未加载实际 signer 或用户凭证，未开生产 writer，未创建策略。
- **上线后的真实运行器 RPC**：179 个可实盘市场、`capital_limit=null`，BTC `80 USDC / 5 倍` 预览 `VALID/live_eligible=true`，独立看护 healthy、剩余重启预算 3。
- 上线后普通账本：活动策略 0、queued/running 操作 0、UNKNOWN/DISPATCHING 订单 0。此次未自动创建任何网格。
- 实际 HL 网站提供新 create/qualification/accounts 静态文件，HTTP 200 且哈希与发布文件一致，缓存策略 no-store。内置浏览器访问公网仍返回既有 `ERR_INVALID_AUTH_CREDENTIALS`，未修改登录或索取新密码；公网登录后的画面未作目视验收，生产 RPC 和网站内容均已核验。

## 备份与审计资料

部署前配套备份：

`/root/code/entropy-grid/data/grid-backups/grid-20261101T031928Z-b6f7acdc`

SQLite integrity ok，事件位置 5718，含原政策与服务器内加密凭证配套。上线后的新 schema 2 政策配套备份也已完成：`/root/code/entropy-grid/data/grid-backups/grid-20261101T032602Z-8a5254fe`，同样 integrity ok/seq5718；原结果位于候选目录的 `post-release-backup.json`。不下载凭证或加密主密钥。

服务器候选目录保留 baseline manifest、准确冻结源、原文件及原政策 rollback、测试和发布结果。它是 HL 专用阶段目录，禁止与仍由另一位开发者推进的 Bulk 候选混用。

本地机器证据目录：`/Users/alex/Work/entropy/data/run/hl-production-open-v1/`：

| 文件 | SHA256 |
|---|---|
| `linux-tests.json` | `aa0558824a4a0dffd38e6f00657332b5b3a449acc7fef7aa8ba8b05f31c55087` |
| `native-preflight.json` | `a87bf8db3128c19662eb623811a889dfc21a50abec057c63c65a9023dfb3357b` |
| `deployment.json` | `5bb5cf5b55c660d7b58b6a3fca9ba8691c642dabbe65f6c753d1f65c19f37137` |
| `post-deploy.json` | `7b522c9206e0d5f729670c31ef9f179b52f2a3208d8f5536a33419f5641882c6` |

`account-ui-update.json` 记录额外静态更新，实际文件 SHA256 `969a1406a74b1a5e60042bb72b05f6de15f1fd94f2a5d5796fcda93cae5a09e3`。最终后端模块 SHA256 `bc422a326720272155da8291c29282d9db959e4cf770b4cc61af5b5c326e45ab`。

操作工具源码：`planning/grid/tools/hl_production_open_preflight.py`、`deploy_hl_production_open_v1.py`。这些是本次一次性过程记录，带防覆盖断言；**不要重跑原发布脚本或原预检身份**。后续以线上当前源码/政策和新请求身份为准，不因文档中的旧版本白名单记录再次把 HL 锁回 ETH/40 USDC/3 倍。

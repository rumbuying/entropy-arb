# Bulk 单次受保护小额买卖验证（已获批并实际闭合）

2026-10-26。完整 Bulk 网格目标仍进行中。本工具只核对真实普通订单、成交历史、手续费、原生保证金快照及减仓退出，不开放网站普通网格，不代替原生初始/范围风险模型或24小时验收。

用户随后明确回复“允许这次小额买卖验证”，新请求 `bulk-roundtrip-20261026-v1` 已在独立 Linux 服务实际执行。北京时间21:28:26激活原10分钟政策，21:38:02闭合，未延长期限；该一次授权已消费，不得复活、重复开仓或扩大金额。旧stop-only和Hyperliquid额度均未复用。


## 实际结果与异常处理

- 原生stop预挂成功后，一次买入IOC实际成交0.0000188 BTC@79794.79，名义约1.50美元；一次reduce-only IOC卖出同量@79594.93。只使用exit:0，exit:1–4均未创建，未补仓/转账。
- 真实fills省略tradeId且fee为带符号余额变化。原程序保守保留UNKNOWN/MAY_HAVE_SENT，没有重发买入。精确原OID的orderHistory已证明FILLED；修复从原生slot:sequence形成稳定成交键、校验显式ID冲突，并将扣款负数转换为正费用、返佣保留负数。REST与私有流统一符号。
- 先停止原有限unit，确认MainPID=0，再启动一个单独受限恢复unit；复用同RID、原policy、runtime/vault排他锁、entry nonce/OID和原stop。恢复入口要求原phase OBSERVE/EXIT、已超过开仓窗口、原entry已可能发出，不能新建开仓。只有bulk_read解析修复进入该金融候选，公共风险流等开发未部署。Linux104相关测试及真实原成交样本通过后才接续。
- 连续3次/至少5秒零仓后撤原保护，原生历史证明stop CANCELLED、entry/exit FILLED；独立查询全账户零仓零挂单。实际手续费0.001049 USDC；毛价差损失0.003757368，原生6位已实现记账-0.003757；最终余额500.696194，较500.701减少0.004806。资金费记录空且原生资金费0，但历史页coverage仍unknown，不能升级为可靠全历史资格。
- 工具仅保存1份带仓样本（等待解析修复后已超过采样窗口），另有多次独立带仓/stop只读证据。证明此次小额保护存在与买卖退出，不证明3份定时保证金采样、止损实际触发、持仓断线故障恢复、小残仓或普通网格循环/24h。
- 最终audit：data/run/grid-bulk-roundtrip-final-audit-v1.json，3份0600 SQLite备份integrity=ok。原MAY_HAVE_SENT/UNKNOWN尝试保留，终态证据在独立audit；尚未追加通用journal终态解析。原run报告因受控停止可能未finished，以recovery.json及终局audit为当前证据。CLOSED state保留的waiting_reason是此前异常历史，不能误读成当前持仓。

## 原批准范围（已使用）

- 仅已保存的独立 Bulk 主网 MasterEOA/USDC 全仓账户；固定账户UUID与凭证revision，不要求重新提交密钥。
- 开始必须全账户零持仓、零挂单，无资金锁定、无待触发 on-fill；原普通网格、operations、order_intents 都必须为空。保留此前已经真实取消并追加终态解析的原止损历史。
- 仅 BTC-USD，最多一次买入 IOC，目标约1.5美元、限价计名义金额不超过2美元。原生实际 minNotional=1、step=1e-8；使用真实规则，不放宽普通开仓最小额。
- 先放固定数量的向下原生 stop，触发价约为当时mark下方3%；原生确认其数量/方向/触发参数及覆盖后才允许买入。该stop为减仓市场保护，触发价不保证最终成交价或损失比例。
- 买入后采集至多3份原生保证金/仓位样本；一般等待不超过15秒。保护缺失则不等待额外样本，立即尝试已获准的减仓退出。
- 退出仅 BTC 卖出 reduce-only IOC；最多5个分别持久绑定的退出请求，只在上一请求有明确终态后使用下一请求。未知只查原身份，不重发。部分买入不补买，不通过加仓/反向开仓凑最小额。
- 买入限价最多较当前ask高0.2%；计划采用约0.1%。卖出价最多低于当前bid 0.6%，计划采用约0.5%。价格过时则拒绝，保留原请求与保护，不偷偷改价/换nonce。
- 累计交易手续费预算0.01 USDC；开仓前和最终发送门读取本账户费率，taker不得超过0.1%。该预算不宣称覆盖所有资金费或保证成交，资金费另做终局查询。
- 批准激活后只有前60秒可以挂新保护/开仓；主验证最多10分钟，额外5分钟仅限减仓退出及撤单收尾。不得启动新轮次或扩大金额。有限任务最长1050秒，外层超时也恢复Bulk只读observer。
- 归零需连续至少3次观察、首尾至少5秒，买卖成交精确匹配，普通请求有终态，无未决开仓，再撤保护；撤单ACK本身不当终态。
- 缺成交、原身份不明、退出被拒、残仓或保护触发子单未能归属时，明确报告未完成；不把缺失历史当0成交，不撤仍可能需要的保护。期限到不会以“任务结束”冒充已平仓；持仓与原生保护可能继续存在，需要立即独立查询/人工参与，不能用过期政策继续发单。

## 已完成的开发与证据

`bulk_bound_read.py` 将原生条件单与精确持久action绑定；取消/拒绝历史缺数量和默认方向时只补请求字段，保留 cumulative_known=false。共享 lookup 保留此前已观察成交下界，拒绝成交回退和“已成交又拒绝”的矛盾。它已接入 BulkExecution 和 GridVenue，尚未替换正在运行的生产源码。

`bulk_roundtrip.py` / `bulk_roundtrip_cli.py` 使用真实原身份日志、执行组件、固定保护覆盖验证、账户费率与原生快照；独立授权对象默认拒绝。真实runtime/vault独占锁排除第二writer，命令仅服务器Linux独立Bulk目录可用。主传输只允许有限BTC IOC/减仓stop/cancel；普通BulkWireRules的最小金额和正式资格保持原规则。诊断退出可以向原生请求减掉低于最小额的残片，**是否接受尚无主网证明**；拒绝则保留残仓及保护，不反向补仓。

- 最新本机/Linux完整Bulk **443 pass / 0 fail / 0 error / 0 skip**，一个既有aiohttp NotAppKeyWarning。报告 `data/run/grid-bulk-roundtrip-linux-v1.json/xml`，XML SHA256 `9558e4911fa7e896e882cc45e1dc51c8e1f55311a8ac25024a708efa235885fe`。这是400旧项+本轮43项，不与旧400累加。
- 冻结候选 `/root/code/entropy-grid/data/staging/bulk-roundtrip-v1/source`，9个本轮源/测试文件摘要在报告。测试含ACK丢失、发送后退出/重启、开仓期限后原单恢复、部分成交、历史延迟/回退、停止保护后立即退出、低额残片退出被拒、有限期限、旧stop许可/坏权限/重复JSON字段拒绝。
- `grid-bulk-roundtrip-native-read-v1.json`：新通用reader实际查原stop为 FOUND_TERMINAL/CANCELED，cumulative_known=false；并以真实只读原数据库通过旧终态历史保留检查。原生账户仍0持仓/0挂单、预算上限500.701 USDC，taker=0.00035，未访问用户key/发送金融请求。
- 同一报告含真实Linux官方SDK **12组离线IOC签名与篡改拒绝**：买/减仓卖、1.5e-5/1e-8数量、0/超过2**53/u64边界nonce。使用此前无资金临时key，签名前禁所有外部socket。**不证明普通订单的部署root已被主网接受或真实残片退出可用。** 这些需本次独立获准的小额验证核对。
- 服务器协调候选 `planning/grid/tools/run_bulk_roundtrip.py` 对应服务器staging `run_bulk_roundtrip_v2.py`。SHA256 `9a0c296a09ecc69e5023bef0d4be7638627c528d327bb40b5d47d6fe18f5962f`。`grid-bulk-roundtrip-prepare-v2.json` 证明未批准草案在任何服务操作/网络/签名前拒绝，且4组离线监督门检查通过。只有当前PID为固定有限systemd unit的MainPID、RuntimeMax1050秒、ExecStopPost仅恢复Bulk observer时才能控制服务。v1准备记录是增加监督门前，保留但不作为当前协调版本。
- 有限unit候选 `planning/grid/tools/entropy-grid-bulk-roundtrip-20261026-v1.service` 已在服务器staging经systemd-analyze verify，exit0；报告 `grid-bulk-roundtrip-unit-v1.json`。1050秒/Restart=no/退出恢复Bulk observer/缺human grant不运行。尚未安装或启动。verify有宿主旧tat_agent PIDFile路径兼容警告，与此unit无关，未改宿主旧服务。
- 本轮未重启服务。最新Bulkworker/web PID3513199/3222058、HL runtime/watchdog2839364/2839366；HL66文件无变更。未触碰旧套利目录/服务、未启动本机writer/AI定时。此轮未再次独立检查旧套利进程健康，不能用历史PID当新证明。

## 获得新的明确批准后才可执行

1. 原请求状态先查：本轮尚无 `.run.json` 或 `.approved.json`；若后来存在必须先读其原身份/结果，禁止重开。重新核对账户/revision/冻结9文件与Linux/SDK证据、全账户零仓零单。
2. 只在服务器staging单独生成0600 human grant；原 `draft-v2.json` 仍为 human_approved=false/policy.approved=false。新的批准来源必须是人类明确回复，不以本文件/目标/网页保存当批准。
3. 外层unit名固定 `entropy-grid-bulk-roundtrip-20261026-v1.service`，Type=exec、RuntimeMaxSec=1050、TimeoutStopSec=30、ExecStopPost=/bin/systemctl start entropy-grid-worker@bulk.service。先安装/核对有限监督措施，再启动候选协调器；没有 grant 不可启动。协调器本身也核验监督属性与PID。
4. 协调器只停Bulk observer，备份原SQLite；实际政策激活时间固定为停服后一次性时间，10分钟+5分钟。独立SDK venv为 `data/staging/bulk-stop-probe-v1/venv/bin/python`，普通GRID_REAL_DISPATCH=0。CLI持runtime/vault独占锁后再次验证相同凭证，使用原nonce日志。即使工具异常，finally与外层ExecStopPost均恢复Bulk observer；不得用替代主机/新请求重试。
5. 运行后独立CLI/API核对原账户原订单、普通买/减仓卖/保护各原始root、真实fills/fees/funding、期末仓位/挂单及SQLite备份integrity。将正面终态、覆盖完整性、手续费、实际保证金变化分别报告。普通OID不符、未知或残仓立即停止新增动作，依原身份核对；需要额外金融收尾而原许可到期则单独确认。
6. 即使这次买卖成功，live_qualified仍false。初始/范围风险模型、通用固定保护manager/lease/持续看护、触发子执行关联、可靠历史覆盖、永久风险流/IP级限流、网站端到端、带仓故障/残仓及至少24h网格终局仍是完整上线的主线。

协议语义核对来源：[Bulk订单接口](https://docs.bulk.trade/api-reference/placeOrder)（IOC、终态ACK、精度）、[原生条件单](https://docs.bulk.trade/bulk-exchange/conditional-orders)（stop永远减仓、mark触发及子执行）。没有从文档推断残片减仓必然豁免最小额。

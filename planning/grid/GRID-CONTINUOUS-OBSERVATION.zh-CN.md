# 实时账户观察、中性保护与自动退出

更新：2026-10-12。目标仍 active；普通网页实盘开单尚未放行。当前实际余额199.76517 USDC，仓位和挂单均0；本阶段没有新增入场、手续费或资金划转。

## 已取得的主网证据

真实账户已连接全部11个永续 DEX 的仓位、订单及共享现货资金观察。`account_observation.ready=true` 的依据是同一连接代次的全部订阅确认、全部初始快照、资金/资金费消息初始快照及5秒内的连接活动，不是从保存的接入结果推定。成交历史 gap 仍单独标注，不从快照或订阅确认制造历史认证。

新实盘两侧保护验收：

- `live-flat-position-sl-pair-20261012-v1` 同批提交相反方向止损，被 Native 明确拒绝：`Trigger order has unexpected side.`。原生 statuses 只有一个 error。官方说明这种预校验单条错误拒绝整个 payload；程序已据此解析原始 ACK，保留原拒绝证据并确认账户清零，最终 REJECTED。没有人工修改数据库制造已执行或通过。
- `live-flat-position-sl-separate-pair-20261012-v1` 分别提交两侧 reduce-only / Stop Market / `s=0` 的整仓保护，均返回 waitingForTrigger。sell：止损75153、限价73649、Native订单571588381423；buy：止损91856、限价93694、Native订单571588428984。
- 在撤单前独立查询到这两笔订单**同时存在**，均为 `isPositionTpsl=true`、`reduceOnly=true`、`origSz=sz=0`。随后逐笔撤销并按原生查单确认 canceled，最终实际账户清零。没有建仓或手续费。正式 `ProtectionManager` 本来就按侧分别提交，符合这次取得的 Native 证据。
- 第二次网站请求等待超过60秒，调用端返回503，但原运行进程保持运行，后台完成并持久化了 CONFIRMED。按同一请求的持久状态核对结果，未因观察超时重启或重复下单。

已新增只读状态接口 `GET /api/grid/accounts/commission_status?account_id=...&request_id=...`，实际返回第二次验收 `CONFIRMED/flat=true/coexistence_passed=true`。超时现在明确说明操作可能仍在进行，不再误报为运行时不可达；连接断开也不会回滚已执行动作或丢失持久结果。公开状态不返回密钥、签名或原生请求内容。

实际历史基线另已查询并保存在权限600、忽略版本控制的 `data/run/grid-history-native-baseline.json`：4笔成交，最早时间1791741105305、最晚1791743014426，资金费0，实际余额199.76517，仓位/挂单0。它是下一步历史范围核对的原始证据，**尚未自动认定为历史保留边界或正式交易资格**。

## 实现变化

`UnifiedObservation` 接入原账户 WebSocket，动态订阅 allDexsClearinghouseState、spotState、各 DEX openOrders、userNonFundingLedgerUpdates 和 userFundings，原 userFills/orderUpdates 保留。连接代次变化、断线、超过5秒没有活动、缺初始数据、缺确认、错误账户/DEX和异常数据都会使整套观察失效；未准备好时走原完整 REST 核对。

统一模式的其他 DEX 仓位/挂单和共享资金现在可以来自这套**持续有效**的观察，每次仍重新检查抽象模式、其他资产和保证金限制。await期间数量、订单或资金事件变化时拒绝旧观察；价格估值变动读取最新资金/冻结值，不在每个价格跳动时使签名前风险许可失效。资金、资金费、成交与订单变化继续推进风险指纹。

REST读取共享滚动上限由700调整为900权重/分钟，为原生写入和其他程序保留官方1200限额下的空间。预备检查在签名前等待配额，不在签名到期后发送；普通网格配额不足时保持 PLANNED并延期，不因尚未传输的请求制造 UNKNOWN或冻结已有库存。其他进程/IP流量仍可能导致 Native限流，不宣称单进程预算能控制它们。

新增持久 `ProtectionExitScheduler`：原生保护成交或标记价达到止损价时，先推进epoch/停止补单/撤普通风险单，再调用正常 stop_flat 退出剩余已归属库存并做保护撤单和最终释放。队列和保护成交同事务保存；自身退出的epoch推进也同事务更新队列。重启先核对历史与在途退出；UNKNOWN未证明终态前不再次减仓，明确拒绝保留待处理状态，新用户停止保留操作可取代尚未发送的自动退出。启动恢复保留 STOPPED意图，不错误转换为PAUSED。

最新全量 **919 passed、2 skipped、108 warnings（61.86秒）**。新增实时观察18项、配额预备/延期2项、同批/分别提交两侧保护6项、自动退出4项、超时/安全状态读取3项；既有账户、网格、资金费与恢复测试继续通过。

服务目前仍为独立本机8789，正常开单关闭，新增验收和划转额度均恢复0。加密凭证未修改，旧套利服务未重启。实盘证据继续在运行库及私有 `data/run/` 保存。

## 下一步必须验证正式网格引擎

不能继续把独立小额 IOC 验收当作完成目标。下一步应基于真实历史基线和已验证保护建立有账户/网络/市场/凭证/规则/额度范围的**受限原生网格验收授权**，用实际 GridRuntime、AccountExecutor、FIFO、共享资金预留、私有消息、补单与生命周期完成真实网格闭环；正式资格未证明前，不能伪造 `live_qualified` 或把一条独立验收直接放行给网站。

需要取得：真实 ALO 订单、实际网格成交和相邻补单；真实部分成交/库存变化与保护触发、尘仓退出；暂停/恢复/重启、资金费与所有历史订单累计核对；然后按真实范围生成资格记录，完成网页创建和管理。高级止盈/ROI/跟踪等仍未全部实现，目标没有收缩为单个 BTC IOC 或模拟演示。

接口依据：[实时订阅](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)、[整批预校验错误语义](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses)、[原生请求限额](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)。真实并存证据来自原生订单查询与持久记录。

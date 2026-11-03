# v9 实盘验收：未通过，当前空仓，新增实盘权限已关闭

核验日期：2026-10-22，北京时间。对象：g-fecf202d3d044c32 / ordinary-observe-linux-20261021-v9。本次没有开新策略、补发金融请求或部署修复代码。

## 结论

这一轮没有连续运行24小时。严格观察最长 **10206.669秒，即2小时50分6.669秒**。原生止损最终发挥作用，当前交易所11个永续DEX范围均无仓无单，但程序内部仍有未解决订单，不能称自动收尾成功或实盘认证通过。

## 实际时间线

- 10月21日23:42:38.935：进入RUNNING，启动请求5ed3a068-84bb-412c-af5d-8d5e9707dc96 succeeded。当时真实Native正仓、开仓单和原生止损核验已通过。
- 10月22日02:32:48：收到上游CLOSE/1000，seq457进入RECOVERING/e2。不是把服务在线时长当连续网格运行。上游关闭原因仍未唯一确定。
- 02:33:49和02:33:50：原恢复对账报告均ok，随后重建保护单阶段出现一笔UNKNOWN。没有成功恢复记录。
- 约02:34：原控制器通过唯一cleanup 66249bc8-e2b7-42e6-98e5-8d8f9360a9dd请求平仓，结果partial/cancel_orders，错误为reconciliation blocked flattening。未知保护单未解决，退出未完成。
- 02:37:14.242：控制器在有界等待后退出，策略仍待处理。不能把这个退出当实际平仓。
- **10:28:07.944：交易所原生止损成交，实际清掉0.0123 ETH。** 后续保护退出a114a72a-76ea-4f00-9281-2bbddfe1ab9b仍因对账阻塞返回partial。
- 本次直接查询交易所，11个永续DEX范围均positions0/orders0，账本净仓也为0。关闭权限后再次独立查询，结果仍为无仓无单。

## 资金核对

三笔成交与Native逐笔匹配，数量、价格、手续费一致；11笔ETH资金费记录合计与账本一致。

| 项目 | USDC |
| --- | ---: |
| 已实现盈亏 | -0.940950 |
| 手续费 | -0.027407 |
| 资金费 | -0.004208 |
| 净盈亏 | **-0.972565** |

没有完成普通网格配对盈利循环，grid_pair_qty为0。盈亏已核对，不代表订单对账状态已修复。

## 已定位问题与未确认部分

1. **原生止损成交回报解析不兼容。** Native确认原单581629066428为filled，成交后的对象isTrigger=false、triggerPx=0；适配器_validate_position_stop仍按在挂触发单要求isTrigger=true和triggerPx>0，返回HL_POSITION_STOP_MALFORMED。因此已实际平仓的原止损在程序中仍为PARTIAL。
2. **另一笔重建保护单仍为UNKNOWN。** 原CID直接查询为unknownOid，当前无任何在挂单；这不足以伪造“已撤销”或“已拒绝”的终态。为何出现UNKNOWN还需审查原提交阶段和完整历史证据。
3. **异常退出未闭环。** 自动平仓被未知订单阻塞，后续反复对账仍无法完成。Native止损提供了最终保护，但不能替代及时的保护性退出和异常监督。

本次不修改订单/账本状态、不强制将策略标为CLOSED，不用空仓证明替代历史恢复成功或24小时资格。

## 本次安全处置

fresh Native全范围空仓核对、私有在线备份完成后，清空普通grants、设置GRID_REAL_DISPATCH=0，停用仅原v9失败轮次到期timer，保留原不可变合同。只重启独立网格runtime/watchdog/web使权限生效。原始PARTIAL/UNKNOWN、失败请求身份和所有证据保留，原finalizer仍不是成功终局证明。

处置脚本的首次后置检查未确认完成；随后没有重放任何处置动作，仅重新核查实际GRID0、grants空、看护健康、原未知记录保留、合同SHA不变、原timer inactive后，将真实处置证明补齐。当前网格运行时1744074、watchdog1744076。旧套利1223049在本轮前后保持active，未操作旧套利目录或服务。AI自动跟进和goal仍paused。

主要证据：data/run/grid-v9-day-native-inspection-20261022.json、grid-v9-day-event-inspection-20261022.json、grid-v9-day-ledger-review-20261022.json、grid-v9-day-funding-review-20261022.json、grid-v9-flat-unresolved-containment-20261022.json、grid-v9-post-containment-native-flat-20261022.json。当前不能放开常规实盘，需先修复保护单终态识别、未知请求处理和失败退出，再以新身份验收。

# 修复已部署，小额实盘验证已重新启动

当前策略：[g-9b3b7681eded4dfa](https://taoli.coinfetcher.xyz/grid/#/grid/g-9b3b7681eded4dfa)。唯一运行身份为 `ordinary-observe-linux-20261023-v12`，原启动请求 `74ba9c1b-9397-419b-9b9a-8945c508999e` 已成功，没有重发。

实际进入RUNNING时间为 **2026年10月23日北京时间02:00:02.381**，独立Native启动核验于02:00:03.796通过。最早24小时观察点是10月24日02:00:02.381；建议02:10后检查连续观察与终局审计的真实结果。独立25小时退出期限为 **10月24日02:53:02.062**，服务器定时器已实际active/waiting。AI定时跟进保持暂停，服务器有限控制器、观察器和到期退出不依赖本机聊天持续运行。

## 实际启动证明

- RUNNING/ACTIVE，epoch1；原start succeeded，当前无PLANNED、UNKNOWN或未决撤单。
- 独立Native确认正仓 **0.0088 ETH**，3倍cross，账本与Native数量一致。
- **6笔真实挂单**，含开仓单与全仓原生止损，止损Native状态open。
- Native核验总敞口 **52.33536 USDC**，低于原150 USDC上限。配置仍是40 USDC总额（含20预留）、ETH long、5格、3倍cross；原30秒服务保护期限未延长。
- Native核验前后SQL、原请求身份和实际运行时相符。最后复核时严格观察最长136.18秒、issues为空；这只是启动验收，**尚未通过24小时**。
- 网站读取测试在初始化全程持续进行，原生启动证明通过后自动结束：详情70次200，最长响应0.701秒；行情2次200、3次503，4次延后。行情读取让出额度时可能暂时不可更新，详情与控制读取不依赖行情完成；测试没有资金POST。不能将这些行情503描述为全部HTTP请求成功。

## 本次修复

1. **原生止损终态。** 区分在挂触发单和触发后的同CID/OID市价子单。终态仍校验币种、方向、reduce-only、全仓标识及数量上界；经济累计只与真实成交历史比较，不生成虚构成交。在挂保护的严格校验保持。
2. **发送前超时。** 授权、签名及最终发送前准备有独立8秒限时，明确未发送的请求不再变成永久UNKNOWN；已进入网络发送的异常仍查询原身份，不补单。
3. **保护容量复用。** 常规库存不再重复计入保护容量，恢复时可以复用已独立确认有效、容量足够的原生止损。
4. **网页读取与交易调度。** 普通交易准备在原public额度内优先于新页面/背景读取；已进行的读取可收尾，避免持有传输锁互等。原900总额度、550普通额度及350保护预留保持，止损和紧急平仓保留原优先路径。
5. **首次成交同步。** 初始IOC已获确认后，最多10秒进行同一原订单的只读完整对账。仅其订单索引/累计成交和仓位的短暂发布差异可重查；必须历史、订单和实际仓位完全一致才继续挂网格单。期间不创建第二笔IOC；外部订单、历史缺失、用户暂停、epoch或连接代际变化均阻断继续开仓。

旧代码已实际复现止损终态、恢复时容量重复、页面读取饥饿和首次成交发布延迟的失败。修正均有回归覆盖；所有资金请求均沿用原身份查询，没有以重复发单解决不确定响应。

## 测试、版本与部署隔离

新干净归档为 `grid-code-20261023-initial-reconciliation-v1.tar.gz`，324文件，SHA256 **7cf83c59d4c35fc82e7bee8f4c1bae86273fe6a5e9a8a34a097d46bd897d4cee**。从已核验归档逐次只替换相关代码与回归文件，未混入工作区其他套利改动。

Mac完整回归1623通过、2项平台跳过；实际Linux **1625通过、0跳过**，XML SHA256 **dca7cea280fac75695e95cee61165edbb01d793c123dffebe8929a948539fbcc**。另实际Linux控制/监督回归135项、工作流34项、网页HTTP6项、诊断守卫7项、systemd校验及真实AccountExecutor离线调度检查均通过。实际66项源文件摘要、原v35以来8项运行时差异、安装证明和新报告逐一绑定；版本正式资格仍为false。

部署目录仅 `/root/code/entropy-grid`，只操作独立网格服务。旧套利目录 `/root/code/entropy` 未同步或修改，`entropy-console.service` 未重启；旧套利进程前后一直是1223049。当前网格runtime1802603、watchdog1802605，同实例且健康。

公网登录页面和认证metrics已实测可访问；原Basic登录保留，登录信息未更改。

## 历史问题的真实收尾

- 原v9 `g-fecf202d3d044c32` 已凭Native真实成交和强过期缺席证据解决旧PARTIAL/UNKNOWN，随后正常退出流程CLOSED，原Native终局审计与备份完成。没有修改任何成交、库存或资金费，没有强制CLOSED。原净亏损0.972565 USDC、24小时失败的结论保留。
- v10 `g-912a518184e846b5` 暴露网页读取饥饿，原cleanup成功、CLOSED、Native空仓与备份完成，净-0.246006 USDC。
- v11 `g-5de1790be67d4652` 暴露初始IOC发布时序，原cleanup成功、CLOSED、Native空仓与备份完成，净+0.029154 USDC。

v10、v11均另经fresh Native空仓核验和备份后停用各自期限、清空grant、恢复GRID0；之后使用新身份部署和重启。未复用失败资金请求，未拼接失败观察窗口。

上游CLOSE/1000的唯一原因仍未确认。本轮还须用真实证据继续检查断线恢复、连续24小时与终局Native/备份；不能以网站在线、空仓测试或本次启动成功代替。

主要证据均位于私有 `data/run/`：`grid-initial-reconciliation-install-proof.json`、`grid-initial-reconciliation-version-proof.json`、`grid-initial-reconciliation-linux-complete.xml`、`grid-v12-final-start-review.json`、`grid-ordinary-live-linux-v12-native-running-proof.json`、`grid-ordinary-live-linux-v12-soak.json`、`grid-ordinary-linux-deadline-v12-contract.json`、`grid-v12-ui-read-check.json`。

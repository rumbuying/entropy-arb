最新覆盖：第35 RUNNING/e5，3笔双向maker refill，自然归零/保护重建3197ms/再入场及真实TCP断线后的带仓恢复均Native验证通过（约4分27秒，原runtime未重启）。仍待>=4refill及终局审计/独立Linux持仓进程故障/小残仓/普通24h，grants=[]。详情见RESUME。

当前覆盖（UTC2026-10-18 03:32）：第35 RUNNING/e3，原start/pause/resume全部成功，独立Native带仓管理proof通过。当前.0176ETH、6所属挂单、含开仓单敞口53.23780USDC，guardian正常。实际refill成交仍0，闭环/恢复/持仓故障/小残仓/普通24h未完成。普通grants=[]；详见RESUME。

最新覆盖：对账诊断已部署，Linux单次1488通过；第35 g-10ced5cc7fbb4c35正在STARTING/e1，原start处理中，不重复。策略风险门槛未改；普通grants=[]。公网独立网格站已发布。实时记录见RESUME。

最新覆盖：第34已因初始建仓后对账不通过而正常清仓，CLOSED/e2，原cleanup成功，Native11DEX零仓零单、198.709458USDC，审计/备份exit0。普通grants仍空；暂无活动实盘，下一步定位具体对账缺口。修复调度和独立公网入口已上线；细节见RESUME。

最新（2026-10-18，第34）：调度清理阻塞修复已部署独立Linux目录，1486项分批通过，66个运行文件hash绑定；新空仓SIGSTOP替换11.411820s。独立公网https://taoli.coinfetcher.xyz/grid/及认证/42资源验证通过，旧套利未改。第33已审计关闭，余额198.718377；第34 g-ddbfe3ca7fed47a6 STARTING/ACTIVE/e1，原start待终态，不重复。普通grants=[]，尚未完成同轮loop/Linux持仓故障/小残仓/断线恢复/普通24h。准确状态与证据见RESUME.md；以下均历史。

# Linux 独立实盘主机迁移检查

状态（2026-10-18 北京时间凌晨）：已迁入独立/root/code/entropy-grid，旧套利不变；Linux分批1486用例全通过，真实空账户卡死恢复11.384625秒通过；本机执行已停用。第32私有40USDC/3cross/150-30验收 g-b84d8d60d70e48f7 已创建、启动仍在原请求处理中，准确实时句柄见RESUME。普通grants=[]，未取得完整Linux实盘资格或普通24h证明。网格暂经SSH转发127.0.0.1:8789访问，公网nginx未修改。

## 已准备的代码

- `entropy_arb/grid/systemd_control.py` 只控制固定 `entropy-grid.service`；检查 systemd unit、实例InvocationID、当前OS用户、进程目录、Python入口、数据库和心跳路径。发现配置待reload或身份不符则不控制。
- 使用Linux pidfd固定所观察到的进程，二次核对身份后才发送SIGKILL，防止PID复用或新实例误杀；随后仅请求启动固定unit，已自动启动的实例不会再次重启。Python缺pidfd接口时不报告有效控制范围。
- `watchdog.py` 默认macOS仍使用launchd，Linux使用systemd；可显式选systemd-user。原两次卡死采样、每小时三次介入、冷却、私有恢复标记和新实例保护退出逻辑保留。
- `deploy/entropy-grid.service` 加入独立心跳并设Restart=always；新 `deploy/entropy-grid-watchdog.service` 独立运行。模板目录仍为/root/code/entropy，必须按用户确认目录替换，不代表已选定安装位置。模板默认GRID_REAL_DISPATCH=0。

## 连接与安装前

1. 确认用户指定的SSH目标、部署目录、服务OS用户及是否已有旧套利服务；先只读检查。不能停止或覆盖旧套利服务、数据库、端口或密钥。
2. 验证Python版本、依赖、systemd可用、服务控制权限、`os.pidfd_open`及`signal.pidfd_send_signal`可用；在目标内核上用本进程pidfd做只读打开/关闭验证，不能只看API存在。
3. 选择system服务或user服务。system模板的runtime与watchdog须使用相同OS用户，且watchdog有固定服务控制权；user服务必须使用--manager systemd-user并核对用户服务管理器能跨退出保持运行。不能把无法控制服务的watchdog当healthy。
4. 保留本机终局CLOSED备份；复核源文件hash及版本后，将独立网格所需文件和私有加密凭证迁移到已确认目标，保持目录/文件私有权限。不要复制本机socket、PID、旧心跳或旧看护状态作为新主机当前状态。若迁移恢复记录有旧实例标记，必须在原恢复规则下核对，不能手删跳过。

## 实机验证与资金切换

1. 新主机先GRID_REAL_DISPATCH=0、正式空授权启动；检查新进程/心跳/外部看护的同实例新鲜度、网站鉴权、只读账户/真实市场/历史和数据库完整性。
2. 必须先做目标Linux的真实服务故障验证：心跳停滞后只替换同一固定网格进程，新实例读取介入标记并执行既有恢复/退出。不得把本地替身测试或Mac历史故障秒数当Linux证据。
3. 新主机开始资金动作前，确认本机无活动网格、所有请求终态，独立Native零仓零单；停用本机网格执行/看护，防止两个主机同时使用同一资金账户。网站可保留只读入口，但不能仍允许本机创建验收。
4. 任何新私有验收政策均有独立请求ID、明确额度和期限；不得修改已失败31的截止或复用其loop证据。新主机验收需要真实成交、双向补单、暂停恢复、原生保护自然重建、真实断线恢复及持仓故障/小残仓退出，按真实平台与范围审核已有证据是否可复用。
5. 全部正式证据审核通过才安装精确范围的普通授权；通过网站正常流程创建新普通网格，实际连续观察24小时，最后平仓/独立Native审计/备份。私有验收时间不能计入普通24小时。

本地隔离测试只验证命令范围、进程身份竞争及原Mac路径兼容性，不证明Linux实机运行成功；全量结果见RESUME中最新记录。本机当前无活动实盘网格，不因等待主机信息开启新轮次。

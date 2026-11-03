旧第27已因保护版本交接缺陷退出，不能用于上线。后续见 ORDINARY-CUTOVER-AFTER-V28.md。

# 第27轮通过后的受限普通入口切换

当前未开放。用户已授权小额CLI/API实盘验证，不再要求用户参与；不能通过浏览器自动化执行交易。原目标范围和真实24小时观察保留。

准备好的运行配置：`data/run/grid-runtime-ordinary-after-v27.plist`。它使用正式实盘政策，移除了私有trial政策；尚未加载。正式政策仍为`grants=[]`。

只读发布预检：`PYTHONPATH=. .venv/bin/python data/run/grid-ordinary-v1-preflight.py`。它不安装政策、不加载密钥、不重启或交易。当前实际拒绝`REVIEWED_CANDIDATE_NOT_READY`，记录为`grid-ordinary-v1-current-preflight.json`。14项隔离场景通过；隔离正例以桩替代正式证据审核，不构成真实资格，见`grid-ordinary-v1-preflight-offline-proof.json`。

## 切换次序

1. 延续第27轮原helper31853，不能重复start或修改配置。原观察截止UTC2026-10-17 14:34:25.217。第27已通过真实带仓pause/resume，但仍缺至少4双向refill、本轮归零重建与后续入场。
2. 实际CLOSED后自动收尾91490做Native最终审计、正式组合审核和备份；只有`grid-service-v27-reviewed-candidate.json`真实生成且只读预检再次全通过，才继续。复用small20/fault23原证据，不能降低或跳过当前完整审核。若Native最终审计超过5分钟，先重新做Native只读核对并重新审核其新哈希引用，不把旧文件时间改新。
3. 确认全库无活动网格/未决意图，保留正式政策空授权和已关闭备份。正常bootout执行服务，原子安装已审核候选和已准备ordinary配置，再bootstrap；网站/看护不动。失败时恢复原空授权和GRID0配置，不在有仓位时回滚代码。不能手工重复启动另一执行进程。
4. 启动时仍须真实凭证/代理授权、历史小额完整退出/成交保留范围及外部看护校验。核对catalog：目标账户ETH精确市场live_eligible=true，范围仅long immediate/deferred、GTC、资本<=40、杠杆<=3、150/30保护。当前普通入口未开启，不能只看环境变量GRID_REAL_DISPATCH。
5. 从普通RPC流程创建新网格：fresh market→正常preview→确认VALID/live_eligible/preview_id→create（固定request_id与preview，confirm_market精确市场）→start（固定request_id）。使用40总额/20reserve/3cross及已审核保护参数；新鲜价格区间与费用仍按正式预览。不得调用operator_trial_create、复用旧RID或把旧试验上下文当普通上下文。超时先查同request_id；不创建新的初始IOC。
6. 实际RUNNING后启动`entropy_arb.grid.soak_observer --scope ordinary`，必须绑定新grid的live-grid记录、grant_id、当前执行实例与保护证据，实际持续86400秒；不计私有验收时间、不改时钟、不按样本数代替时长。暂停/重启/保护异常等原样记录；不能把部分时段或重启前后拼成通过。
7. 连续观察通过后正常停止、最终Native对账及新备份，核对范围后报告真实可用能力与限制。用户若主动接管或改变策略，先尊重新指令。当前不宣称已完成普通24h或整个通用产品范围。

当前source/test与部署依据：1427passed/2skip/110warnings；快照`grid-code-20261017T0829Z-pause-resume.tar.gz`，299文件逐项hash/size核对。预检和准备配置是后加的独立私有运维工具，14场景单独验证，不声称包含在1427那次全量里。

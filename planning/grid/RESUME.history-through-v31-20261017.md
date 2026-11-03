# 最新：第31已启动，保护行情读取修复已部署（2026-10-17）

用户授权CLI/API不到200USDC独立账户验证，自主推进、加快实盘上线；无CUA金融操作、不动旧套利、不引入互斥。目标active，普通grants=[]。

## 实时状态与故障
- 第30 g-25c06adff0dd434f 已 CLOSED/STOPPED/e5。start/pause/resume全部成功，实际2笔maker买成交分别.0044@2436.6/.0044@2434，2笔因果refill卖已Native OPEN，但没有refill成交。UTC11:39:25.752风险检查得到PROTECTION_QUOTE_STALE，按原保护门槛触发PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE/e4退出，helper同请求stop_flat成功，不能当合格loop。
- Native终局4fills/2stops/11DEX全零，net -.053777 USDC（gross-.03124，fee.022496，funding-.000041），账户198.802119。原审计 grid-service-native-v30-proof.json；finalizer4569 finished（Native0/review1/backup0，无候选授权）。最后CLOSED备份 data/grid-backups/grid-20261017T114059Z-af87ef95 seq280/integrityok；11:29活动快照4f4d32ae/seq278是3fill/持仓.0132的活动副本，不能当终局。
- 当前Runtime5573/web89365/watchdog13846健康；第30主helper72683已terminal0，finalizer也完成，不能重跑旧30。第31流程7588已创建g-a14ab46c2c3042dc，当前RUNNING/e3；start a23ff75b-aa5e-455f-9b07-c0703a31e275成功5placed，pause d2e4fc7c-7d5e-45ca-9cb5-c5e7b34f914b成功5撤净，resume a06165e3-8d19-4848-8346-bf60cc740cff成功5placed/0pending；真实grid/status看source及DB，不因RPC等待重复创建。

## 新修复（已部署）
- protection._verify原先用venue.market_snapshot（普通HTTP），而prepare及服务重建用critical_market_snapshot（新鲜WS请求、原HTTP fallback）。实际该HTTP行情未通过原时效门槛；观察日志只保存错误码，没有保留当次原始HTTP响应，不声称拿到原始网络延迟证据。
- 现仅把_verify行情调用改成critical_market_snapshot；保留原5秒时效/未来时间/标的/价格触发/Native stop/资金仓位门槛及退出行为，未放宽门禁、未缓存价格、不把stale改成有效permit。
- 新 tests/test_grid_protection_quote_transport.py 5case旧版均失败，修复后相关86通过2.66s，涵盖fresh WS替代stale HTTP、stale WS仍拒绝、错标的仍拒绝、触价仍退出、WS不可用fallback HTTP仍受原时效门槛。没有签名动作。
- 完整全量1464passed/2skipped/110warnings/81.81秒，log data/run/grid-protection-quote-transport-final-tests.log。fresh Native11DEX空仓后已部署5573；bootout0/首次bootstrap5、确认未加载后重试0，外部看护fresh匹配。版本grid-service-native-v31-version-proof.json记录67源码hash。303文件归档data/grid-code-checkpoints/grid-code-20261017T1144Z-protection-quote.tar.gz sha eaae0e42c21617178b39d8e9ddb896328198f865b96eebd7aa575532d823df40；manifest实际1464结果，包版本/逐项hash复核通过。回滚源 data/run/grid-protection-quote-rollback/protection.py。

## 后续
1. 延续同一31主helper7588；finalizer69014/health54309/Native20421/stream取证4082/caffeinate70517（绑定5573最多24000秒），processes已保存。仍须实际start/pause/resume及后续成交/保护自然重建。
2. 第31政策沿用18:26:32UTC到期时间，没有延长旧30期限。40/20/3cross/GTC/5格/2.6正费用间距/150-30，新的原6h deadline会在本轮start返回后持久化；不能修改已开始的截止/配置，不重复small20/fault23。
3. Ordinary准备已绑31，ORDINARY-CUTOVER-AFTER-V31.md；14只读预检+9控制器+6本地HTTP场景独立通过，不是实盘或1464。controller未启动，其state不存在，grants=[]。候选必须正式audit_release/fresh Native/全库空仓/源码/guardian检查；真实断线恢复仍须独立证明。然后一次普通网站HTTP控制器实际24h，最后Native审计/备份。
4. 第20小残仓、第23持仓故障原证据可按范围复用；第24真实rearm部分证据不是完整loop。其他方向/交易所/Linux/高级功能目标保留，不标complete。历史与精确30过程见 RESUME.history-through-v30-20261017.md。

第31带仓management已成功，独立Native证据 grid-service-native-v31-held-management-proof.json（尚不证明断线恢复/完整loop）。只读trial soak session87970已经启动，scope operator-trial，不是ordinary24h。原deadline_ms=1792259433093，UTC 2026-10-17T17:50:33.093000+00:00，不能延长。延续同一7588等待真实4双向refill+自然rearm/再入场；所有其他当前handles见processes。

第31首笔maker买.0044@2431.8（UTC11:59:19.506，OID577503313526/TID836548256004361）已实成交；因果refill卖.0044@2434.4，IID190a165a-f23b-44f5-b0f6-6261b473d740/CID...01a7，已Native OPEN/Gtc/reduceOnly独立确认，源Native成交OID/TID/CID/量价费/maker均匹配。proof grid-service-native-v31-first-refill-proof.json。Native接受距源24976ms、独立取证距源35775ms；refill尚未成交，不能计入4双向门槛。

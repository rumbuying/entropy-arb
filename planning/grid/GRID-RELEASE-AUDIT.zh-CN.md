# 本地上线证据只读自检

运行：

```sh
.venv/bin/python -m entropy_arb.grid.release_audit --db data/grid-runtime.sqlite3 --policy data/run/grid-live-release-policy.json
```

也可将--policy指向尚未安装的私有候选政策，提前查看每个grant的本地证据结果。数据库使用mode=ro及query_only，单一一致读取事务；不会迁移或新建数据库，不读取凭证库，不实例化运行引擎或交易所客户端，不修改配置或打开派发。候选文件须本人所有、普通文件、无符号链接且权限600，最多1MiB；非有限JSON或坏输入只输出符号错误，不回显内容。

工具直接复用LiveRelease.evidence及其service_release验证组件，不维护另一套放宽的规则。输出每个grant的local_evidence_passed、失败代码或proof_hash。all_local_evidence_passed在没有grant时为False；即使本地通过，live_dispatch_enabled_by_audit始终False。实际加载仍须原LiveRelease.restore完成当前密钥/授权、原生小额全额退出/历史以及看护与运行政策核对。

退出码0：存在grant且所有本地证据通过；1：未配置grant或有证据不通过；2：输入不可用。数据只是当前数据库与固定文件的读取快照，不替代持续运行验证。

专项69通过，覆盖实际因果循环/小额证据成功及缺失拒绝、主动写入尝试被只读数据库拒绝、空政策不算成功、缺失库不得生成、文件权限/符号链接/NaN和坏数字输入不回显。日志grid-release-audit-final-tests.log。本机真实政策自检结果grid-release-readonly-current-proof.json：0 grant、未通过、没有打开交易入口，符合当前状态。该工具为独立CLI，未接入运行中第22轮；交易核心仍为1398全量通过版本。

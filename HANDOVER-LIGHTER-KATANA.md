# Lighter ↔ Katana 线交接文档

> 写作时间：2026-09-25 · 对应提交 `256ec8b` 及其之前一串
> 相关文档：[MAKER-DESIGN.md](MAKER-DESIGN.md)（maker 模式设计）、
> [BASIS-EXPLORE.md](BASIS-EXPLORE.md)（三所基差探索）、
> [KATANA-2D-REVIEW.md](KATANA-2D-REVIEW.md)、[HANDOVER.md](HANDOVER.md)（项目总交接）

---

## 0. 一句话现状

引擎已能表达 **Lighter 为基准腿** 的配对（`--base`），Lighter↔Katana 线已采集
主网 44.5h×6 标的、rh 9.5h×5 标的，并完成 **rh 的 HYPE/ZEC maker 试点首跑**：
机制全部跑通（报价→成交→批量对冲→失败熔断→兜底平仓），但**被 Lighter nonce 冲突阻塞**。
**当前状态：两边仓位已平、无挂单、maker worker 已全部停止，无资金敞口。**

---

## 1. 这条线是什么

| | |
|---|---|
| 目标结构 | **Katana 挂单 maker + Lighter 吃单对冲**（往返成本 ≈ 0.95bp + Lighter 点差） |
| 引擎表达 | `--base lighter-rh --hedge katana`（maker 角色 = **对冲腿** Katana；taker 对冲 = **基准腿** Lighter） |
| premium 口径 | `premium = Lighter / Katana − 1`，与 `tools/basis_probe.py` 完全一致 → probe 采的 band 可直接迁移 |
| 为什么可能不用主网 | rh（Robinhood 链）账户已有且已入金，省掉主网开户；代价是 **rh 点差宽 2–3 倍**（见 §2） |
| 不可行结构 | taker-taker（成本 3.8bp + 两所点差）：sd/成本 只有 0.31–0.33，**必须走 maker** |

---

## 2. 选标的的实测依据

同窗口（9.4h 逐分钟对齐）对比，maker 成本 = 0.95bp 手续费 + Lighter 侧点差：

| 标的 | 路线 | median | sd | Lighter 点差 | maker 成本 | sd/成本 | bp/天 | 现实 clip | $/天 |
|---|---|---|---|---|---|---|---|---|---|
| **ETH** | 主网 | +2.34 | 0.98 | **0.52** | 1.47 | 0.67 | 25.4 | $15k | **$38.1** |
| ETH | rh | +6.29 | 0.97 | 1.53 | 2.48 | 0.39 | 1.1 | $15k | $1.7 |
| **HYPE** | 主网 | +1.53 | 2.83 | 0.85 | 1.80 | 1.57 | 48.1 | $2k | $9.6 |
| **HYPE** | **rh** | +10.26 | 2.77 | 1.75 | 2.70 | **1.03** | **52.5** | $2k | **$10.5** |
| **ZEC** | 主网 | +8.67 | 4.04 | 1.20 | 2.15 | 1.88 | 139.7 | $1k | $14.0 |
| **ZEC** | **rh** | +7.80 | 3.04 | 2.65 | 3.60 | **0.84** | **132.1** | $1k | **$13.2** |
| BTC | 主网 | +2.98 | 0.73 | 0.02 | 0.97 | 0.75 | 9.3 | $10k | $9.3 |
| BTC | rh | +2.27 | 0.76 | 0.97 | 1.92 | 0.39 | 0.2 | $10k | $0.2 |
| SOL | 主网 | +3.03 | 1.13 | 0.51 | 1.46 | 0.77 | 43.8 | $1k | $4.4 |
| SOL | rh | +1.75 | 1.11 | 2.03 | 2.98 | 0.37 | 0.0 | $1k | $0.0 |

**结论**
- **ETH 只能在主网跑**：rh 的点差（1.53 vs 0.52）把 sd 只有 ~1bp 的标的直接压死（$38 → $1.7/天）
- **rh 路线只有 HYPE + ZEC 划算**（它们 sd 大到能吞掉多出来的 ~1bp 成本）
- **容量瓶颈永远在 Katana**（OI：ETH $473k、BTC $148k、HYPE $37k、ZEC $17k、SOL $12k）；Lighter 侧都很深（rh：BTC/ETH $26M、HYPE $5.3M）
- 已排除：BTC（溢价太紧 + 回归最慢 ~120min）、SOL（OI 仅 $12k）、DOGE（成本 6bp > 边际、rh 无此市场）

**数据文件**：`logs/minutes-<SYM>-lighter-vs-katana.csv`（主网）、
`logs/minutes-<SYM>-lighter-rh-vs-katana.csv`（rh）。bad-row 过滤：`|premium| < 150bps`
（ZEC 薄盘出现过 1687/2030bps 假行）。

---

## 3. 现在跑着什么

| 进程 | 说明 |
|---|---|
| `ANTH` live（w1） | HL io dex ↔ lighter-rh，taker band，**未受本次事件影响** |
| `SNDK` live（w2） | HL ↔ lighter-rh，同上 |
| record-only：`katana-btc` / `lighter-eth-katana` / `lighter-hype-katana` | 主网对照采集 |
| `entropy-probe`（systemd） | 主网 Lighter↔Katana，6 标的 |
| `entropy-probe-rh`（systemd） | **rh** Lighter↔Katana，5 标的（rh 无 DOGE） |
| **已停止** | `lighter-rh-hype-katana`、`lighter-rh-zec-katana`（maker 试点，因 nonce 阻塞） |

---

## 4. 🔴 当前阻塞：Lighter nonce 冲突（最高优先级）

**机制**：Lighter 的 nonce 按 `(account_index, api_key_index)` 计数，SDK
（`lighter.nonce_manager`）本地缓存后递增，必要时向 `/api/v1/nextNonce` 取。

**现象**：`HTTP ... code=21104 message='invalid nonce'`。maker 对冲连续失败 3 次
→ `max_hedge_failures` 触发 **EXPOSED 熔断**（撤单 + 告警 + 兜底平仓）。

**根因**：rh 账户（27904）上**同时有 ANTH、SNDK、HYPE、ZEC 四个进程共用同一个 api key**。
昨夜 ANTH 00:30 那次 `invalid nonce` 是同一根因，当时误判为偶发。

**三个修法（推荐 1+2 组合）**
1. **每个 worker 一个 Lighter API key**（最正确）：同一账户可建多个 key，nonce 按 key 隔离。
   需要给 profile 加"凭证前缀"支持（如 `credentials: LIGHTER_HYPE`），然后在页面建 2–3 个 key
2. **撞 nonce 自动刷新重试**（轻量缓解）：捕获 21104 → `signer.nonce_manager.hard_refresh_nonce()` → 重试一次
3. **同时只跑一个 rh worker**（零改动，但 ANTH/SNDK 仍共用 key，偶发冲突仍在）

---

## 5. 2026-09-25 首跑事故记录（含失误）

**时间线**
1. 改 cap 至试点档（HYPE $600 / ZEC $400）、启用 maker、启动 HYPE live
2. 暴露三个**从未实盘验证过**的缺陷，逐个修复：
   - `venue_katana.py` 用了 `ws_connect` 却未 import → 私有订单流起不来 → 引擎拒绝报价（**安全梯正确生效**）
   - 报价被 GTX 拒 `LIMIT_PRICE_CROSSES_SPREAD`：报价锚定对冲腿，rh 比 Katana 高 6–10bp，
     算出的买价穿过 Katana 卖一 → 每 0.5s 重试并烧光订单预算
   - rh 对冲 `invalid nonce`（§4）
3. ZEC 首笔成交后对冲 3 连败 → **EXPOSED 熔断**，兜底 `_hedge` 在 Katana 平掉敞口，损失 ≈ $0.37
4. HYPE 同类失败，部分对冲成功

**⚠️ 我的失误（必须记住）**
看到 rh 账户接口 `position: 4.386` 就判定"HYPE 两边同时做多"，于是把 **Katana 腿卖掉** ——
实际当时是**正确的 delta 中性对冲**（Katana 多 4.39 / rh 空 4.386，因为 **`position` 是无符号量，
方向在 `sign` 字段**）。结果制造了 4.386 HYPE 的**裸空**，约 4 分钟后买回纠正（买回价还略优）。

- **教训：平仓前先核对 `sign`；账户接口的 `position` 不带方向。**
- 本次事故总成本 **≈ $1**（Katana −$0.55，rh ≈ −$0.5）

**安全梯表现（正面）**：成交后对冲失败 → 撤光所有挂单 → 熔断停机 → 兜底减敞口。全链路按设计工作，
未出现失控敞口。

---

## 6. 本次已完成的代码改动

| 提交 | 内容 |
|---|---|
| `af9a26c` | **基准腿解耦**：`--base {hl,lighter,lighter-rh,katana}`（默认 hl，向后兼容）、`entropy.symbol` 别名、**per-leg Lighter 凭证**（`LIGHTER_BASE_*` / `LIGHTER_HEDGE_*` 覆盖共用 `LIGHTER_*`）、console/supervisor/webui 全链路支持；同时落地 Katana venue + maker 模式 |
| `f3d0426` / `87a6552` / `92429d8` | `entropy-probe-rh` systemd 单元（rh 5 标的；rh 无 DOGE 市场） |
| `07e9265` | `profiles/lighter-rh-{hype,zec}-katana.yaml` |
| `8dc8a97` | console：不再对不透明 API secret 做字母表校验（原规则误杀含 `/` 的 Katana secret） |
| `f1ed1bf` | **Katana 委托密钥（session key）**：订单结构 `delegatedPublicKey` / 撤单 `delegatedKey` 填签名者地址；签名信封修复（signature 提到顶层） |
| `0c8aa76` | **签名 0x 前缀**（ethers 格式；裸 hex 会导致 `INVALID_WALLET_SIGNATURE`）+ 请求体带 `delegatedKey` |
| `256ec8b` | maker：`ws_connect` 导入 + **`clamp_to_maker_book()`**（post-only 不穿越 maker 盘口，夹逼后不覆盖成本则放弃该侧） |

测试：**132 passed**。

---

## 7. 凭证与账户现状

**Katana**（`.env`）
- `KATANA_API_KEY` / `KATANA_API_SECRET`：HMAC 请求认证
- `KATANA_PRIVATE_KEY`：**session key 私钥**（`0x9F1D…261E`，已通过 `GET /v1/delegatedKeys` 确认注册，名称 `taoli`）
- `KATANA_WALLET`：**存保证金的钱包** `0x3fe9…f2fe`（委托模式下必须与私钥地址不同）
- session key **2026-10-25 11:43 到期**，到期需重新授权
- 账户：equity **$499.45**（已入金 $500）；初始保证金率 **10%**

**Lighter**
- `LIGHTER_*` = **rh 账户 27904**（ANTH/SNDK 的对冲腿 + 新线的基准腿共用）
- `LIGHTER_BASE_*` **未配置**（主网账户还没建立）
- rh 账户：available **$299.11**、collateral $496.54、IM_req $204.94；只剩 ANTH 的合法对冲空头 0.478 ANTHROPIC
- rh 初始保证金 **20%**（实测 ANTHROPIC $1023 → 占用 $204.6，**cross 全仓**）
- ⚠️ **主网 base 与 rh base 不能同时跑**：`LIGHTER_BASE_*` 只有一组，会互相抢（需要按部署命名才可并行）

---

## 8. 恢复运行步骤（修完 nonce 后）

1. 修 nonce（§4 的 1+2），确认每个 worker 的 Lighter key 唯一
2. 核对两端余额与保证金模式；建议同时检查引擎页"距强平/保证金占用"遥测
3. **先只开 HYPE**（`lighter-rh-hype-katana`，size_base 2.0 ≈ $183/笔，cap $600）
4. 观察 `logs/maker-selection-HYPE-lighter-rh.csv` 的逆向选择指标（`prem_at_fill/1s/10s`）与
   `logs/maker-trades-HYPE-lighter-rh.csv`
5. 稳定后再开 ZEC（`size_base 0.12`，cap $400，幻影上限 25bp）

启动方式（console API，base=lighter-rh）：
```bash
POST /api/workers {"profile":"lighter-rh-hype-katana","symbol":"HYPE",
                   "base":"lighter-rh","hedge":"katana","mode":"live","confirm":"HYPE"}
```

---

## 9. 设计约束与坑（都会再咬人）

1. **报价锚定对冲腿 → 基差大时必须夹逼**：已修（`clamp_to_maker_book`）。若基差大到算出的价格
   落在 maker 盘口另一侧，会先夹到 touch 内侧，再按成本判断是否放弃该侧
2. **重启 live maker 会留孤儿单**：本次启动瞬间交易所上有 4 笔挂单（旧进程遗留 + 新进程报价）。
   **待办**：maker 启动时应先执行一次 cancel-all（现在只在 blocked 状态才撤）
3. **maker 私有订单流是报价的前提**：`maker_stream_down` 时引擎拒绝报价——这是特性不是 bug
4. **session key 30 天过期**；过期后下单会报签名错误
5. **ZEC 薄盘假行**：`max_signal_edge_bps: 25` 必须保留；HYPE 为 40
6. **`position` 无符号**（rh 账户接口）——见 §5 教训
7. **rh 全仓（cross）保证金**：一条线的亏损会吃掉另一条的保证金（可用 `tools/isolated_margin.py`
   的思路做逐仓，目前该工具只支持 HL）

---

## 10. 待办清单

- [ ] **P0** Lighter nonce 隔离（per-worker API key + 撞 nonce 自动刷新重试）
- [ ] **P0** maker 启动时 cancel-all（清孤儿单）
- [ ] P1 主网 ETH 线：`LIGHTER_BASE_*` 凭证 + 按部署命名（`LIGHTER_MAINNET_*` / `LIGHTER_RH_*`）以支持并行
- [ ] P1 nonce 冲突若无法根治 → 明确"一个 rh worker 独占"的运行纪律并写进 OPERATIONS
- [ ] P2 rh 侧逐仓/隔离保证金（`tools/isolated_margin.py` 目前仅 HL）
- [ ] P2 band 自动校准对 rh 路线的验证（`auto_band` 需要每个 profile 单独跑）

---

## 11. 常用命令

```bash
# 采集（已由 systemd 托管）
systemctl status entropy-probe entropy-probe-rh
tail -f logs/probe-lighter-rh-vs-katana.log

# 数据体检：两路同窗口对比（sd/成本、回归半衰期、band 回测）
python3 tools/analyze.py --csv logs/minutes-HYPE-lighter-rh-vs-katana.csv --fees-bps 0.95
python3 tools/basis_matrix.py --minutes 1440

# 引擎侧只读检查（不花钱验证凭证/签名链路）
python3 /tmp/katana_check.py          # 账户/持仓可读
python3 /tmp/katana_order_path.py     # 远离盘口 post-only 挂单+撤单

# 平掉意外敞口（只减仓）：把 qty 换成实际持仓，注意核对 sign
#   参考 §5：先 print(fetch_position())，确认方向再下单
```

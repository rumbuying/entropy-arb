# Feedback to bulk.trade — API stability & documentation (draft, 2026-11-07)

**To:** bulk.trade team (Telegram @tradebulk / X @bulktrade — no public support email found in docs)
**From:** an automated grid-trading operator on mainnet
**Account:** CEQCFMNRS2mCED3CPz9sc4ReaWhgxA3xkJs5kzCdWW87 (server IP available on request)
**Subject:** Fills coverage certification removed, 2-minute full API outage, WAF 403s below documented limits — and undocumented pagination fields

---

Dear bulk.trade team,

We operate an automated grid-trading engine on bulk mainnet (live since 2026-10-29).
Our client respects the documented budget — sustained ≤2 requests/second against the
5/s limit, WS-first for account streams and risk metrics — and we recorded zero 429
responses across 2026-11-06. During 11-05/11-06 we hit four issues we'd like to report
and ask about. Evidence below is from our production journal; raw responses and longer
excerpts are available on request.

## 1. Coverage certification no longer published on fills history (most impactful)

`POST /api/v1/account` with `{"type":"fills", "startSlot": …}` returns pages whose
`page.coverage` is `"unknown"` and `page.minAvailableSlot` is absent. As of 2026-11-05
every fills response we receive looks like this:

```json
{"data": [ …44 rows… ],
 "page": {"startSlot": 640469011, "endSlot": 706571289, "asOfSlot": 706571289,
          "coverage": "unknown", "hasMore": false, "nextCursor": null,
          "minAvailableSlot": null}}
```

Prior to 11-05 our reconciliation never refused a window on this ground, so this looks
like a behavior change. Questions:

1. Is `coverage:"unknown"` (and the missing floor) intentional or a regression? Will
   certification return?
2. The semantics of `coverage`, `minAvailableSlot`, `hasMore`, `nextCursor`,
   `startSlot/endSlot/asOfSlot` are **not documented anywhere** in docs.bulk.trade.
   These fields are exactly what a client needs to reconcile a fill ledger safely —
   please document them (a `.md` under api-reference would fit).

Impact on us: our engine requires coverage certification (or an explicit "uncertified"
acknowledgment) before reconciling fills into its ledger; the silent change produced a
full day of reconciliation churn on a live grid until we adapted.

## 2. Two-minute full API outage — 2026-11-06, 19:28–19:29 CST (11:28–11:29 UTC)

All public reads from our server failed for ~2 minutes (82 failures, both of our
read paths), causing startup-reconciliation failures on active grids. Reads resumed
normally immediately after. Question: was this a known incident/gateway event, and is
there a status page or a response header that signals degraded mode so clients can
back off instead of churning?

## 3. Read latency variability (20–115 s per reconcile pass)

The same account-read pipeline costs 1–3 s per request during healthy windows and
20–115 s per multi-read reconciliation pass during degraded windows (observed across
11-05/11-06 nights). Pacing on our side is a constant 1 rps. Question: is this known
capacity behavior, and is there a recommended client posture (e.g. reduce polling,
increase page size) when latency rises?

## 4. WAF 403s below the documented limits

A diagnostic client at **1 request/second** (well under the documented 5/s) received
plain **403 Forbidden** — not 429, no `Retry-After` — after ~9 requests, and stayed
blocked afterwards. The docs' note "handle rejections even when below published
sustained rates" suggests a WAF layer exists, but its triggers (User-Agent? burst
shape?) and signals are undocumented. Ask: please document the WAF behavior and give
clients a distinguishable signal (header/status contract) for WAF blocks vs rate
limits vs origin failures — 403-with-no-body is indistinguishable from a ban.

## What we ask for

1. Document the history pagination & coverage fields (`coverage`, `minAvailableSlot`,
   `hasMore`, `nextCursor`, slot windows) and their guarantees.
2. Confirm whether fills coverage certification was withdrawn intentionally, and its
   expected semantics going forward.
3. A machine-readable signal for degraded mode / WAF blocks (header or status code).
4. Post-mortem note for the 19:28 UTC+8 outage on 11-06.

We're happy to share full journal excerpts, raw responses, or reproduce probes with
your guidance. Thank you for the API — the execution architecture is excellent, and we
want our client to be a good citizen on it.

Best regards,
<your name / Telegram handle>

---

## 附：中文摘要（给你自己看，不用发）

四个问题按影响排序：
1. **fills 覆盖证明被撤**（11-05 起 `coverage:"unknown"`、无 floor）——我们账本对账的安全依据，引发全天对账churn；且这些字段文档里完全没有
2. **19:28-19:29（UTC+8）2 分钟全故障**——82 次公开读全挂，两个网格启动对账失败
3. **读延迟波动**——同管道白天 1-3 秒、夜间 20-115 秒
4. **WAF 层未文档化**——1 rps（低于限额 5 rps）也被 403，无 Retry-After，与限流不可区分

渠道：文档和官网都没有邮箱；官方频道是 **Telegram @tradebulk**、公告 @BULKnetAnnouncements、X @bulktrade。草稿可直接粘到 Telegram。

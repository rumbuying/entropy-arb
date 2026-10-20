/* 共享的“引擎成交口径”估算数学（P&L 看板 / 账户真值对照 / 总览估算列共用）。
   这是估算，不是账本：FIFO 配对只用 trades CSV 的引擎记录值，不含实际
   手续费明细、资金费和对冲腿成交（R-02 M3 才补采集）。 */

export function fnum(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// 熵腿实际成交量（多头腿方向）——决定仓位路径
export function legQty(r) {
  if (r.direction === "buy_entropy") return fnum(r.buy_fill) || 0;
  if (r.direction === "sell_entropy") return fnum(r.sell_fill) || 0;
  return 0;
}

// FIFO 配对：今日已平仓部分的配对盈亏（入场+离场两腿价差，含引擎估费）
export function lotStats(rows, cutoff) {
  const chrono = (rows || []).slice().reverse();   // API 新→旧，反转成时间正序
  const lots = [];
  let realizedToday = 0;
  for (const r of chrono) {
    const q = legQty(r);
    if (!(q > 0)) continue;                        // 单腿/未成交行不动仓位
    const e = fnum(r.fill_edge_usd) ?? fnum(r.edge_usd) ?? 0;
    const epu = e / q;
    const signed = r.direction === "buy_entropy" ? q : -q;
    if (!lots.length || Math.sign(signed) === Math.sign(lots[0].qty)) {
      lots.push({ qty: signed, epu });
      continue;
    }
    let remain = q;
    while (remain > 1e-12 && lots.length) {
      const lot = lots[0];
      const take = Math.min(remain, Math.abs(lot.qty));
      const pnl = take * (lot.epu + epu);
      if ((fnum(r.ts) || 0) >= cutoff) realizedToday += pnl;
      if (take >= Math.abs(lot.qty) - 1e-12) lots.shift();
      else lot.qty -= take * Math.sign(lot.qty);
      remain -= take;
    }
    if (remain > 1e-12) lots.push({ qty: signed > 0 ? remain : -remain, epu });
  }
  return { realizedToday };
}

/* 一条 /api/pnl/lines 线的今日估算：锁定（FIFO）+ 浮动（session MTM）。
   返回 hasData=false 时调用方必须显示 “—”，不能把缺 CSV 当成 0。 */
export function estimateLine(line, cutoff) {
  const rows = Array.isArray(line.trades) ? line.trades : [];
  const today = rows.filter(r => (fnum(r.ts) || 0) >= cutoff);
  const complete = today.filter(r =>
    ("buy_fill" in r) ? (fnum(r.buy_fill) || 0) > 0
      && (fnum(r.sell_fill) || 0) > 0
      : (r.ok === "1" || r.ok === true));
  const realized = lotStats(rows, cutoff).realizedToday;
  const volToday = today.reduce((acc, r) => acc + Math.max(
    fnum(r.buy_notional) || 0, fnum(r.sell_notional) || 0), 0);
  const running = line.status === "running";
  const ses = line.session || {};
  const floatPnl = running
    ? fnum(ses.unrealized_usd ?? ses.unrealized_pnl) : null;
  return {
    rows, today, complete, realized, volToday, running, floatPnl,
    hasData: line.trades_exists !== false,
  };
}

/* ---- 当前 run 起始库存反推（2026-10-20 ANTH $0 事故的修正） ----------------
   引擎重启会从交易所继承仓位，trades CSV 不知道它的成本。只用最近的成交窗口
   做 FIFO 会把“平掉继承仓位”误当成“开新仓”，得到错误的 0。
   做法：
   - FIFO 只吃当前 run 的行（ts >= started_ts）；
   - 起始库存 = 当前熵腿仓位 − 本 run 净变化（只有数量、成本未知的种子 lot）；
   - 今日配对到种子 lot 的数量记为 unknownQty，不计入 realized；
   - 全部不可估时 realized=null（调用方显示 “—”，禁止用 0 冒充）。 */

/* /api/pnl/lines 每线返回的成交行上限（与 pnl.js 的 limit=2000 保持一致）。
   达到上限且最早一行仍晚于段起点 ⇒ 窗口没覆盖 run 起点，不能反推库存。 */
const API_ROW_LIMIT = 2000;

export function lotStatsSeeded(rows, cutoff, startPos) {
  const chrono = (rows || []).slice().reverse();   // API 新→旧，反转成时间正序
  const lots = [];
  if (startPos !== null && Number.isFinite(startPos)
      && Math.abs(startPos) > 1e-12) {
    lots.push({ qty: startPos, epu: null, seed: true });   // 成本未知
  }
  let realized = 0, knownQty = 0, unknownQty = 0;
  for (const r of chrono) {
    const q = legQty(r);
    if (!(q > 0)) continue;
    const e = fnum(r.fill_edge_usd) ?? fnum(r.edge_usd) ?? 0;
    const epu = e / q;
    const signed = r.direction === "buy_entropy" ? q : -q;
    if (!lots.length || Math.sign(signed) === Math.sign(lots[0].qty)) {
      lots.push({ qty: signed, epu });
      continue;
    }
    let remain = q;
    while (remain > 1e-12 && lots.length) {
      const lot = lots[0];
      const take = Math.min(remain, Math.abs(lot.qty));
      if ((fnum(r.ts) || 0) >= cutoff) {
        if (lot.epu === null) unknownQty += take;
        else { realized += take * (lot.epu + epu); knownQty += take; }
      }
      if (take >= Math.abs(lot.qty) - 1e-12) lots.shift();
      else lot.qty -= take * Math.sign(lot.qty);
      remain -= take;
    }
    if (remain > 1e-12) lots.push({ qty: signed > 0 ? remain : -remain, epu });
  }
  return { realized, knownQty, unknownQty };
}


/* ---- 跨 run 分段 FIFO（2026-10-21 灰度重启后“锁定全 0”事故的修正） --------
   一天内可能重启多次；今日的成交分散在多个 run 里。做法：
   - 按 segments（started_ts 升序）把成交行分段；
   - 从最新段向最早段倒推：pos_end(最新)=当前仓位，pos_start=pos_end−段净变化，
     run 切换不改仓位，所以 pos_end(上一段)=pos_start(本段)；
   - 每段用“成本未知种子”做 FIFO，只把今日（ts>=cutoff）配对计入；
   - 段起始库存不可知（窗口被 2000 行/tail 截断且最早一行在段中间）时，
     该段及更早段的今日平仓一律记 unknown，不猜；全部不可估则 realized=null。 */
function crossRunEstimate(line, rows, cutoff, posE, out) {
  const segs = (line.segments || [])
    .map(s => ({
      started_ts: fnum(s.started_ts),
      ended_ts: s.ended_ts === null || s.ended_ts === undefined
        ? null : fnum(s.ended_ts),
    }))
    .filter(s => s.started_ts !== null)
    .sort((a, b) => a.started_ts - b.started_ts);
  if (!segs.length) return null;

  const segRows = segs.map(() => []);
  for (const r of rows) {
    const ts = fnum(r.ts);
    if (ts === null) continue;
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      if (ts >= s.started_ts && (s.ended_ts === null || ts < s.ended_ts)) {
        segRows[i].push(r);
        break;
      }
    }
  }
  const nets = segRows.map(rs => {
    let n = 0;
    for (const r of rs) {
      const q = legQty(r);
      if (q > 0) n += r.direction === "buy_entropy" ? q : -q;
    }
    return n;
  });

  // 截断只影响返回窗口最老的一侧：若最早一行落在第 k 段中间，则第 k 段
  // 及更早段的段起始库存不可知（链断了）。
  const oldest = rows.length ? fnum(rows[rows.length - 1].ts) : null;
  const windowTruncated = rows.length >= API_ROW_LIMIT
    || line.tail_truncated === true;
  let invalidFrom = null;
  if (windowTruncated && oldest !== null) {
    let k = -1;
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      if (oldest >= s.started_ts
          && (s.ended_ts === null || oldest < s.ended_ts)) { k = i; break; }
    }
    if (k < 0 || oldest > segs[k].started_ts + 1e-6) {
      invalidFrom = Math.max(0, k);
    }
  }

  // 从最新段往回推种子；一旦断了，更早的段都不可知
  const seeds = new Array(segs.length).fill(null);
  let posEnd = posE;
  let broken = false;
  for (let i = segs.length - 1; i >= 0; i--) {
    if (broken || (invalidFrom !== null && i <= invalidFrom)) {
      seeds[i] = null;
      broken = true;
      continue;
    }
    seeds[i] = posEnd - nets[i];
    posEnd = seeds[i];
  }

  let realized = 0, knownQty = 0, unknownQty = 0;
  for (let i = 0; i < segs.length; i++) {
    if (seeds[i] === null) {
      // 段起始库存不可知：该段今日的成交一律不估（保守，不猜）
      for (const r of segRows[i]) {
        if ((fnum(r.ts) || 0) >= cutoff) unknownQty += legQty(r);
      }
      continue;
    }
    const st = lotStatsSeeded(segRows[i], cutoff, seeds[i]);
    realized += st.realized;
    knownQty += st.knownQty;
    unknownQty += st.unknownQty;
  }
  out.seeded = true;
  out.startPos = seeds.length ? seeds[seeds.length - 1] : null;
  out.knownQty = knownQty;
  out.unknownQty = unknownQty;
  out.partial = knownQty > 1e-12 && unknownQty > 1e-12;
  if (knownQty <= 1e-12 && unknownQty > 1e-12) {
    out.reason = invalidFrom !== null ? "truncated" : "inherited_unknown";
  } else {
    out.realized = realized;                     // 0 = 今日没有可估平仓
  }
  return out;
}

/* 看板用的“带起始库存”估算。返回字段：
   realized: number|null（null=不可估，UI 必须显示 “—”）
   partial: 今日既有可估平仓、又有继承仓位平仓
   reason: null | no_data | stopped | no_position | no_started_ts
           | inherited_unknown | truncated
   today/complete/volToday 仍按“今日（cutoff 起）的全部成交行”统计。 */
export function estimateLineSeeded(line, cutoff) {
  const rows = Array.isArray(line.trades) ? line.trades : [];
  const today = rows.filter(r => (fnum(r.ts) || 0) >= cutoff);
  const complete = today.filter(r =>
    ("buy_fill" in r) ? (fnum(r.buy_fill) || 0) > 0
      && (fnum(r.sell_fill) || 0) > 0
      : (r.ok === "1" || r.ok === true));
  const volToday = today.reduce((acc, r) => acc + Math.max(
    fnum(r.buy_notional) || 0, fnum(r.sell_notional) || 0), 0);
  const running = line.status === "running";
  const ses = line.session || {};
  const floatPnl = running
    ? fnum(ses.unrealized_usd ?? ses.unrealized_pnl) : null;
  const hasData = line.trades_exists !== false;
  const out = {
    rows, today, complete, volToday, running, floatPnl, hasData,
    realized: null, partial: false, knownQty: 0, unknownQty: 0,
    startPos: null, reason: null,
  };
  if (!hasData) { out.reason = "no_data"; return out; }
  if (!running) { out.reason = "stopped"; return out; }
  const venues = (line.state && line.state.venues) || {};
  const posE = fnum((venues.entropy || {}).position);
  if (posE === null) { out.reason = "no_position"; return out; }
  // 跨 run：接口给了 segments（回看窗口内有交集的 run，升序）
  if (Array.isArray(line.segments) && line.segments.length) {
    const cross = crossRunEstimate(line, rows, cutoff, posE, out);
    if (cross) return cross;
  }
  const started = fnum(line.started_ts);
  if (started === null) { out.reason = "no_started_ts"; return out; }
  // 500 行窗口 / tail 截断：如果最早返回的一行仍晚于 run 起点，说明
  // 本 run 的成交没被完整覆盖，净变化不可知 ⇒ 不能反推起始库存。
  // 宁可显示 “—” 也不给出错误数字（Chief/Backend 2026-10-20 要求）。
  const oldest = rows.length ? fnum(rows[rows.length - 1].ts) : null;
  const windowTruncated = rows.length >= API_ROW_LIMIT
    || line.tail_truncated === true;
  if (windowTruncated && oldest !== null && oldest > started + 1e-6) {
    out.reason = "truncated";
    return out;
  }
  const runRows = rows.filter(r => (fnum(r.ts) || 0) >= started);
  let net = 0;
  for (const r of runRows) {
    const q = legQty(r);
    if (q > 0) net += r.direction === "buy_entropy" ? q : -q;
  }
  const startPos = posE - net;
  const st = lotStatsSeeded(runRows, cutoff, startPos);
  out.startPos = startPos;
  out.knownQty = st.knownQty;
  out.unknownQty = st.unknownQty;
  out.partial = st.unknownQty > 1e-12 && st.knownQty > 1e-12;
  if (st.knownQty <= 1e-12 && st.unknownQty > 1e-12) {
    out.reason = "inherited_unknown";            // 今日全部落在继承仓位上
  } else {
    out.realized = st.realized;                  // 0 = 今日没有可估平仓
  }
  return out;
}

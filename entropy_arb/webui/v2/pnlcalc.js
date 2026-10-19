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

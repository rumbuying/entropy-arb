/* Dependency-free canvas charts.
   TimeSeriesChart: rolling multi-series lines + horizontal reference lines
   (midline / entry bands).
   Histogram: distribution bins + vertical marks (band preview).
   Both handle devicePixelRatio and container resize. */

const AXIS_COLOR = "#3a4a5a";
const TEXT_COLOR = "#7a8b9c";
const GRID_COLOR = "#16202b";
const FONT = "11px ui-monospace, monospace";

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const h = canvas.clientHeight || 200;
  if (canvas.width !== Math.round(w * dpr) ||
      canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

export class TimeSeriesChart {
  /**
   * canvas: <canvas class="chart">
   * series: [{key, color, width?}] — values looked up in each pushed point
   */
  constructor(canvas, series, opts = {}) {
    this.canvas = canvas;
    this.series = series;
    this.points = [];               // [{t, values:{key:number|null}}]
    this.maxPoints = opts.maxPoints || 3600;
    this.yPadFrac = 0.12;
    this.lines = [];                // [{v, color, label, dash?}]
    this.yMin = this.yMax = null;   // fixed scale override
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(canvas.parentElement || canvas);
  }

  setLines(lines) { this.lines = lines; }
  setFixedScale(min, max) { this.yMin = min; this.yMax = max; }

  push(p) {
    this.points.push(p);
    if (this.points.length > this.maxPoints) this.points.shift();
  }

  reset() { this.points = []; }

  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const padL = 8, padR = 52, padT = 8, padB = 18;
    const iw = w - padL - padR, ih = h - padT - padB;
    if (iw < 10 || ih < 10) return;

    // y range over series values + reference lines
    let lo = this.yMin, hi = this.yMax;
    if (lo === null || hi === null) {
      lo = Infinity; hi = -Infinity;
      for (const p of this.points)
        for (const s of this.series) {
          const v = p.values[s.key];
          if (v === null || v === undefined) continue;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      for (const ln of this.lines) {
        if (ln.v === null || ln.v === undefined) continue;
        lo = Math.min(lo, ln.v); hi = Math.max(hi, ln.v);
      }
      if (!Number.isFinite(lo)) { lo = -1; hi = 1; }
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      const pad = (hi - lo) * this.yPadFrac;
      lo -= pad; hi += pad;
    }

    const X = i => padL + (this.points.length <= 1 ? iw / 2 :
      (i / (this.points.length - 1)) * iw);
    const Y = v => padT + (1 - (v - lo) / (hi - lo)) * ih;

    // horizontal grid + y labels
    ctx.font = FONT;
    ctx.strokeStyle = GRID_COLOR; ctx.fillStyle = TEXT_COLOR;
    ctx.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const v = lo + (hi - lo) * g / 4;
      const y = Y(v);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(v.toFixed(1), padL + iw + 5, y);
    }

    // reference lines (bands/midline)
    for (const ln of this.lines) {
      if (ln.v === null || ln.v === undefined) continue;
      const y = Y(ln.v);
      ctx.save();
      ctx.strokeStyle = ln.color;
      ctx.setLineDash(ln.dash === false ? [] : [5, 4]);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.restore();
      if (ln.label) {
        ctx.fillStyle = ln.color; ctx.textAlign = "left"; ctx.textBaseline =
          ln.v >= (lo + hi) / 2 ? "bottom" : "top";
        ctx.fillText(ln.label, padL + 4, y + (ln.v >= (lo + hi) / 2 ? -2 : 2));
      }
    }

    // x labels: first / middle / last timestamp
    if (this.points.length > 1) {
      ctx.fillStyle = TEXT_COLOR;
      ctx.textBaseline = "top";
      const fmt = t => {
        const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
        return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
      };
      ctx.textAlign = "left";
      ctx.fillText(fmt(this.points[0].t), padL, h - padB + 3);
      ctx.textAlign = "center";
      ctx.fillText(fmt(this.points[Math.floor(this.points.length / 2)].t),
                   padL + iw / 2, h - padB + 3);
      ctx.textAlign = "right";
      ctx.fillText(fmt(this.points[this.points.length - 1].t),
                   padL + iw, h - padB + 3);
    }

    // series
    for (const s of this.series) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 1.6;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < this.points.length; i++) {
        const v = this.points[i].values[s.key];
        if (v === null || v === undefined) { started = false; continue; }
        const x = X(i), y = Y(v);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }
}

export class Histogram {
  /** canvas, then setData({bins:[{x0,x1,count}], marks:[{v,color,label}]}) */
  constructor(canvas) {
    this.canvas = canvas;
    this.bins = [];
    this.marks = [];
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(canvas.parentElement || canvas);
  }

  setData(bins, marks = []) { this.bins = bins; this.marks = marks; this.draw(); }

  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const padL = 8, padR = 8, padT = 8, padB = 18;
    const iw = w - padL - padR, ih = h - padT - padB;
    if (iw < 10 || ih < 10 || !this.bins.length) {
      ctx.font = FONT; ctx.fillStyle = TEXT_COLOR;
      ctx.fillText("no data", padL, padT + 12);
      return;
    }
    const maxC = Math.max(...this.bins.map(b => b.count), 1);
    const lo = this.bins[0].x0, hi = this.bins[this.bins.length - 1].x1;
    const X = v => padL + ((v - lo) / (hi - lo || 1)) * iw;
    const Y = c => padT + (1 - c / maxC) * ih;

    // bars
    for (const b of this.bins) {
      if (!b.count) continue;
      const x0 = X(b.x0), x1 = X(b.x1);
      ctx.fillStyle = b.color || "#2a6f8f";
      ctx.fillRect(x0 + 0.5, Y(b.count), Math.max(1, x1 - x0 - 1),
                   padT + ih - Y(b.count));
    }

    // marks (threshold lines)
    ctx.font = FONT;
    for (const m of this.marks) {
      if (m.v === null || m.v === undefined || m.v < lo || m.v > hi) continue;
      const x = X(m.v);
      ctx.save();
      ctx.strokeStyle = m.color; ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + ih); ctx.stroke();
      ctx.restore();
      if (m.label) {
        ctx.fillStyle = m.color;
        ctx.textAlign = "center"; ctx.textBaseline = "bottom";
        ctx.fillText(m.label, x, padT - 1 + 10);
      }
    }

    // x axis: ~6 ticks
    ctx.fillStyle = TEXT_COLOR; ctx.textBaseline = "top";
    const ticks = 6;
    for (let i = 0; i <= ticks; i++) {
      const v = lo + (hi - lo) * i / ticks;
      ctx.textAlign = i === 0 ? "left" : (i === ticks ? "right" : "center");
      ctx.fillText(v.toFixed(1), X(v), h - padB + 3);
    }
  }
}

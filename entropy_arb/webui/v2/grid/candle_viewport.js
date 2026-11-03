// Time slots, including missing candles. Viewing never requests more history.
export const candleSteps = { '1m':60000, '5m':300000, '15m':900000, '1h':3600000, '4h':14400000, '1d':86400000 };
export class CandleViewport {
  reset(data) {
    this.data = data;
    this.step = candleSteps[data?.interval];
    this.total = this.step ? Math.max(1, Math.floor((data.end_ms-data.start_ms)/this.step)+1) : 0;
    this.size = this.total; this.offset = 0;
  }
  zoom(factor, anchor = 1) {
    if (!this.total) return;
    anchor = Number.isFinite(anchor) ? Math.max(0,Math.min(1,anchor)) : 1;
    const size = Math.min(this.total, Math.max(Math.min(8,this.total), Math.round(this.size*factor)));
    const fixed = this.offset + this.size*anchor;
    this.offset = Math.max(0,Math.min(this.total-size,Math.round(fixed-size*anchor)));
    this.size = size;
  }
  pan(slots) {
    if (!this.total) return;
    this.offset = Math.max(0,Math.min(this.total-this.size,this.offset+Math.round(slots)));
  }
  visible() {
    if (!this.data || !this.step) return this.data;
    const start_ms = this.data.start_ms + this.offset*this.step;
    const end_ms = Math.min(this.data.end_ms,start_ms+this.size*this.step-1);
    return {...this.data,start_ms,end_ms,candles:this.data.candles.filter(c=>c.open_ms>=start_ms&&c.open_ms<=end_ms)};
  }
}

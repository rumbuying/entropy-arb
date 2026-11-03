import { el } from '../components.js';
import { gridGet } from './common.js';
import { chart } from './widgets.js';
import { CandleViewport } from './candle_viewport.js';

// Independent from trade previews and lifecycle polling. No timer-driven reads.
export class CandlePanel {
  constructor(container) {
    this.alive = true;
    this.sequence = 0;
    this.interval = '15m';
    this.viewport = new CandleViewport();
    this.graph = el('div', { tabindex: 0, 'aria-label': '图表视野，方向键移动，加减号缩放，Home复位', style: 'touch-action:pan-y;cursor:grab' });
    this.note = el('div', { class: 'grid-note' });
    this.select = el('select', { 'aria-label': 'K 线周期' });
    for (const [value, text] of [['1m','1 分钟'],['5m','5 分钟'],['15m','15 分钟'],['1h','1 小时'],['4h','4 小时'],['1d','1 天']])
      this.select.append(el('option', { value, text, selected: value === this.interval }));
    this.button = el('button', { text: '更新 K 线', disabled: true });
    this.includeGrid = false;
    this.scopeButton = el('button', { text: '包含网格区间', 'aria-pressed': 'false' });
    this.scopeButton.onclick = () => {
      this.includeGrid = !this.includeGrid;
      this.scopeButton.textContent = this.includeGrid ? '行情优先视野' : '包含网格区间';
      this.scopeButton.setAttribute('aria-pressed', String(this.includeGrid));
      this.render();
    };
    this.scopeNote = el('div', { class: 'grid-note' });
    this.fillNote = el('div', { class: 'grid-note' });
    const controls = el('div', { class: 'grid-chart-controls' });
    this.viewButtons = [];
    for (const [text, action] of [['放大',()=>this.viewport.zoom(.67)], ['缩小',()=>this.viewport.zoom(1.5)],
      ['较早',()=>this.viewport.pan(-this.viewport.size/4)], ['较新',()=>this.viewport.pan(this.viewport.size/4)], ['复位',()=>this.viewport.reset(this.data)]]) {
      const button = el('button', { text, disabled: true });
      button.onclick = () => {action(); this.render();};
      controls.append(button); this.viewButtons.push(button);
    }
    this.graph.addEventListener('wheel', event => {
      if (!this.data?.candles.length || !event.deltaY) return;
      event.preventDefault();
      const box = this.graph.getBoundingClientRect();
      const anchor = Math.max(0,Math.min(1,((event.clientX-box.left)/box.width*760-24)/616));
      this.viewport.zoom(event.deltaY<0 ? .8 : 1.25, anchor); this.render();
    }, { passive: false });
    this.graph.addEventListener('keydown', event => {
      const action = {ArrowLeft:()=>this.viewport.pan(-this.viewport.size/4),ArrowRight:()=>this.viewport.pan(this.viewport.size/4),
        '+':()=>this.viewport.zoom(.67),'=':()=>this.viewport.zoom(.67),'-':()=>this.viewport.zoom(1.5),Home:()=>this.viewport.reset(this.data)}[event.key];
      if (action && this.data?.candles.length) {event.preventDefault(); action(); this.render();}
    });
    this.graph.addEventListener('pointerdown', event => {
      if (event.button!==0 || !this.data?.candles.length) return;
      this.drag = {id:event.pointerId,x:event.clientX,offset:this.viewport.offset};
      this.graph.setPointerCapture(event.pointerId);
    });
    this.graph.addEventListener('pointermove', event => {
      if (!this.drag || this.drag.id!==event.pointerId) return;
      const width=this.graph.getBoundingClientRect().width*616/760;
      const slots=(this.drag.x-event.clientX)/width*this.viewport.size;
      this.viewport.offset=this.drag.offset;this.viewport.pan(slots);this.render();
    });
    const finish = () => {this.drag=null;};
    this.graph.addEventListener('pointerup', finish);
    this.graph.addEventListener('pointercancel', finish);
    this.graph.addEventListener('lostpointercapture', finish);
    this.select.onchange = () => { this.interval = this.select.value; this.load(); };
    this.button.onclick = () => this.load();
    container.replaceChildren(el('div', { class: 'grid-panel-body' }, this.select, this.button, this.scopeButton, controls, this.note, this.scopeNote, this.fillNote), this.graph);
    this.foreground = () => { if (!document.hidden && this.pending) this.load(); };
    document.addEventListener('visibilitychange', this.foreground);
  }
  observeFills(gid, status) {
    if (gid!==this.gridId) {this.fills=[];this.fillNote.textContent='正在读取成交标记…';}
    this.gridId=gid;this.gridStatus=status;
    const key=`${gid}:${status}`;
    if (key!==this.fillAttemptKey || ((status!=='CLOSED' || key!==this.fillKey) && Date.now()-(this.fillAttempt||0)>=15000)) this.loadFills();
  }
  async loadFills() {
    if (!this.alive || !this.gridId || this.fillRunning || document.hidden) return;
    this.fillRunning=true;this.fillAttempt=Date.now();
    const key=`${this.gridId}:${this.gridStatus}`;
    this.fillAttemptKey=key;
    this.fillAbort=new AbortController();
    try {
      const result=await gridGet(`/${encodeURIComponent(this.gridId)}/fills?limit=200`, {signal:this.fillAbort.signal});
      if (!this.alive || key!==`${this.gridId}:${this.gridStatus}`) return;
      this.fills=result.fills;this.fillKey=key;
      this.fillNote.textContent=`成交标记：最近 ${this.fills.length} 笔已入账成交${this.fills.length===200 ? '（上限200笔，完整记录见成交表与导出）' : ''} · ${new Date().toLocaleTimeString('zh-CN')} · 仅显示当前时间窗口内买入/卖出`;
      this.render();
    } catch(error) {
      if (this.alive && error.name!=='AbortError') this.fillNote.textContent='成交标记暂无法更新，保留上次结果；完整记录见成交表。';
    } finally {this.fillRunning=false;}
  }
  update(account, market, plan, history, mark) {
    const key = `${account}|${market}`;
    this.plan = plan; this.history = history; this.mark = mark;
    if (key !== this.key) {
      this.key = key; this.account = account; this.market = market;
      this.data = null; this.load();
    }
    this.render();
  }
  render() {
    if (!this.alive) return;
    const bars = this.data?.candles || [];
    for (const button of this.viewButtons) button.disabled=!bars.length;
    this.scopeNote.textContent = bars.length ? (this.includeGrid
      ? '视野包含行情与全部网格；区间相距较远时，K 线会被压缩。'
      : '行情优先视野 · 仅显示视野内的网格线；可切换查看完整网格区间。') : '';
    this.graph.replaceChildren(chart(this.plan, this.history, this.mark, this.viewport.visible(), this.includeGrid, this.fills));
  }
  async load() {
    this.abort?.abort();
    const own = ++this.sequence;
    this.data = null; this.viewport.reset(null);this.drag=null;this.render();
    this.loadFills();
    if (!this.account || !this.market || !this.alive) return;
    if (document.hidden) { this.pending = true; return; }
    this.pending = false;
    this.abort = new AbortController();
    this.button.disabled = true;
    this.note.textContent = '正在读取交易所真实 K 线…';
    try {
      const data = await gridGet(`/candles?account_ref=${encodeURIComponent(this.account)}&market_ref=${encodeURIComponent(this.market)}&interval=${this.interval}`, { signal: this.abort.signal });
      if (!this.alive || own !== this.sequence) return;
      this.data = data;
      this.viewport.reset(data);
      this.note.textContent = `Hyperliquid · 行情快照 ${new Date(data.observed_ms).toLocaleString('zh-CN')} · 手动更新${data.complete_window ? '' : ` · 缺少 ${data.missing_open_ms.length} 根，保留空位`}${data.candles.some(c => !c.closed) ? ' · 最后一根尚未收盘' : ''}`;
      this.render();
    } catch (error) {
      if (this.alive && own === this.sequence && error.name !== 'AbortError')
        this.note.textContent = `K 线暂不可用：${error.message}。可更新重试；图中保留标记价与网格档位。`;
    } finally {
      if (this.alive && own === this.sequence) this.button.disabled = false;
    }
  }
  destroy() {
    this.alive = false; ++this.sequence; this.abort?.abort();
    this.fillAbort?.abort(); this.drag=null;
    document.removeEventListener('visibilitychange', this.foreground);
  }
}

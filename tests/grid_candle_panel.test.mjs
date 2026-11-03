import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { CandleViewport } from '../entropy_arb/webui/v2/grid/candle_viewport.js';
const source = (await fs.readFile(new URL('../entropy_arb/webui/v2/grid/candle_panel.js', import.meta.url), 'utf8')).replace(/^import .*;\n/gm, '').replace('export class CandlePanel', 'class CandlePanel') + '\nglobalThis.CandlePanel = CandlePanel;';
function harness() {
  const calls = [], listeners = new Map();
  const el = (tag, attrs = {}, ...children) => ({ tag, ...attrs, children, listeners:{}, addEventListener(k,f){this.listeners[k]=f;}, getBoundingClientRect(){return {left:0,width:760};}, setPointerCapture(){}, setAttribute(k,v){this[k]=v;}, append(...nodes) { this.children.push(...nodes); }, replaceChildren(...nodes) {this.children=nodes;} });
  const document = { hidden:false, addEventListener(k,f) {listeners.set(k,f);}, removeEventListener(k,f) {if(listeners.get(k)===f)listeners.delete(k);} };
  const context = vm.createContext({document,el,AbortController,Date,encodeURIComponent,
    CandleViewport, chart:(plan,history,mark,data)=>({plan,history,mark,data}),
    gridGet:(path,options)=>new Promise((resolve,reject)=>calls.push({path,options,resolve,reject}))});
  vm.runInContext(source,context);
  const panel = new context.CandlePanel(el('div'));
  return {panel,calls,document,listeners};
}
const data = symbol => ({symbol,interval:'1m',start_ms:0,end_ms:150000,observed_ms:150000,complete_window:true,missing_open_ms:[],candles:[{open_ms:0,closed:false}]});
const settle = () => new Promise(resolve=>setImmediate(resolve));

test('polling chart updates do not request more candles; old market response is discarded', async()=>{
  const h=harness();h.panel.update('a','BTC',{},[],100);
  assert.equal(h.calls.length,1);
  h.panel.update('a','BTC',{latest:true},[],101);assert.equal(h.calls.length,1);
  h.panel.update('a','ETH',{},[],200);
  assert.ok(h.calls[0].options.signal.aborted);assert.equal(h.calls.length,2);
  h.calls[1].resolve(data('ETH'));await settle();
  h.calls[0].resolve(data('BTC'));await settle();
  assert.equal(h.panel.data.symbol,'ETH');assert.match(h.panel.note.textContent,/未收盘/);
  h.panel.destroy();assert.equal(h.listeners.size,0);
});

test('hidden changes wait for foreground; interval and teardown fence late responses', async()=>{
  const h=harness();h.document.hidden=true;
  h.panel.update('a','BTC',{},[],100);assert.equal(h.calls.length,0);
  h.document.hidden=false;h.listeners.get('visibilitychange')();assert.equal(h.calls.length,1);
  h.panel.select.value='1h';h.panel.select.onchange();
  assert.match(h.calls[1].path,/interval=1h/);assert.ok(h.calls[0].options.signal.aborted);
  h.panel.destroy();h.calls[1].resolve(data('BTC'));await settle();assert.equal(h.panel.data,null);
});

test('failed reads expose retry and do not fabricate candles', async()=>{
  const h=harness();h.panel.update('a','BTC',{},[],100);
  h.calls[0].reject(new Error('offline'));await settle();
  assert.equal(h.panel.data,null);assert.equal(h.panel.button.disabled,false);
  assert.match(h.panel.note.textContent,/offline/);
  h.panel.button.onclick();assert.equal(h.calls.length,2);
  h.calls[1].resolve({...data('BTC'),complete_window:false,missing_open_ms:[0]});await settle();
  assert.match(h.panel.note.textContent,/缺少 1 根/);h.panel.destroy();
});

const widgetSource = await fs.readFile(new URL('../entropy_arb/webui/v2/grid/widgets.js', import.meta.url), 'utf8');
const chartSource = widgetSource.slice(widgetSource.indexOf('export function chart('), widgetSource.indexOf('export function modal(')).replace('export function chart','function chart') + '\nglobalThis.chart=chart;';
test('actual chart draws truthful gap positions, OHLC wicks, volume and partial opacity',()=>{
  const node = tag => ({tag,attrs:{},children:[],setAttribute(k,v){this.attrs[k]=v;},append(n){this.children.push(n);}});
  const context = vm.createContext({document:{createElementNS:(ns,tag)=>node(tag)},Date,Number,Math});
  vm.runInContext(chartSource,context);
  const bars = [0,120000].map(open_ms=>({open_ms,open:'100',high:'105',low:'95',close:'101',volume:'3',closed:open_ms===0}));
  const graph = context.chart(null,[],101,{interval:'1m',start_ms:0,end_ms:150000,symbol:'BTC',candles:bars});
  const wicks=graph.children.filter(n=>n.tag==='line' && n.children.some(c=>c.tag==='title'));
  assert.equal(wicks.length,2); assert.equal(wicks[1].attrs.opacity,.55);
  assert.ok(Number(wicks[1].attrs.x1)-Number(wicks[0].attrs.x1)>400);
  const rectangles=graph.children.filter(n=>n.tag==='rect');assert.equal(rectangles.length,4);
  for(const n of rectangles) assert.ok(Number(n.attrs.height)>=0 && Number.isFinite(Number(n.attrs.y)));
  assert.match(graph.attrs['aria-label'],/真实 K 线/);
  assert.ok(graph.children.some(n=>n.textContent?.includes('未收盘')));
});

test('view scope changes only rendering, without another public query', async()=>{
 const h=harness();h.panel.update('a','BTC',{},[],100);h.calls[0].resolve(data('BTC'));await settle();
 h.panel.scopeButton.onclick();assert.equal(h.panel.includeGrid,true);assert.equal(h.calls.length,1);h.panel.destroy();
});

test('viewport respects cached edges, gaps and pointer anchored zoom',()=>{
 const v=new CandleViewport();v.reset({interval:'1m',start_ms:0,end_ms:7199999,candles:[0,60000,7140000].map(open_ms=>({open_ms}))});
 v.zoom(.5,.5);assert.equal(v.size,60);assert.equal(v.offset,30);assert.equal(v.visible().candles.length,0);
 v.pan(-99999);assert.equal(v.offset,0);assert.equal(v.visible().candles.length,2);
 v.pan(99999);assert.equal(v.offset,60);assert.equal(v.visible().candles[0].open_ms,7140000);
 for(let i=0;i<20;i++)v.zoom(.5);assert.equal(v.size,8);assert.equal(v.offset,112);
 v.zoom(100);assert.equal(v.size,120);assert.equal(v.offset,0);
});

test('wheel, drag and keyboard change only cached viewport and controls reset',async()=>{
 const h=harness();h.panel.update('a','BTC',{},[],100);
 const d={...data('BTC'),end_ms:7199999,candles:Array.from({length:120},(_,i)=>({open_ms:i*60000,closed:true}))};
 h.calls[0].resolve(d);await settle();
 let prevented=0;
 h.panel.graph.listeners.wheel({deltaY:-1,clientX:640,preventDefault(){prevented++;}});
 assert.equal(h.panel.viewport.size,96);
 h.panel.graph.listeners.pointerdown({button:0,pointerId:1,clientX:300});
 h.panel.graph.listeners.pointermove({pointerId:1,clientX:500});
 assert.ok(h.panel.viewport.offset<24);
 h.panel.graph.listeners.pointerup();assert.equal(h.panel.drag,null);
 h.panel.graph.listeners.keydown({key:'Home',preventDefault(){prevented++;}});
 assert.equal(h.panel.viewport.size,120);assert.equal(prevented,2);assert.equal(h.calls.length,1);h.panel.destroy();
});

test('closed fill markers load once; a changed status discards late data and reloads',async()=>{
 const h=harness();h.panel.observeFills('g','RUNNING');assert.equal(h.calls.length,1);
 h.panel.observeFills('g','RUNNING');assert.equal(h.calls.length,1);
 h.panel.observeFills('g','CLOSED');h.calls[0].resolve({fills:[{execution_key:'stale'}]});await settle();
 assert.equal(h.panel.fills.length,0);
 h.panel.observeFills('g','CLOSED');assert.equal(h.calls.length,2);
 h.calls[1].resolve({fills:[{execution_key:'real'}]});await settle();
 h.panel.observeFills('g','CLOSED');assert.equal(h.calls.length,2);assert.equal(h.panel.fills[0].execution_key,'real');
 assert.match(h.panel.fillNote.textContent,/最近 1 笔/);h.panel.destroy();
});

test('fill read errors back off instead of retrying each detail poll',async()=>{
 const h=harness();h.panel.observeFills('g','CLOSED');h.calls[0].reject(new Error('offline'));await settle();
 for(let i=0;i<10;i++)h.panel.observeFills('g','CLOSED');assert.equal(h.calls.length,1);
 h.panel.fillAttempt-=15001;h.panel.observeFills('g','CLOSED');assert.equal(h.calls.length,2);h.panel.destroy();
});

test('actual chart places only valid in-window fills using exact ledger labels',()=>{
 const node=tag=>({tag,attrs:{},children:[],setAttribute(k,v){this.attrs[k]=v;},append(n){this.children.push(n);}});
 const context=vm.createContext({document:{createElementNS:(ns,tag)=>node(tag)},Date,Number,Math});vm.runInContext(chartSource,context);
 const bar={open_ms:0,open:'100',high:'105',low:'95',close:'101',volume:'3',closed:true};
 const f={side:'buy',exchange_ts:'1970-01-01T00:00:30.000Z',price:'102.000000000000001',qty_base:'0.000000000123',fee_amount:'0.01',fee_asset:'USDC',trade_id:'9007199254740993123',execution_key:'owned'};
 const graph=context.chart(null,[],101,{interval:'1m',start_ms:0,end_ms:59999,symbol:'BTC',candles:[bar]},false,[f,{...f,exchange_ts:'1970-01-01T00:01:00Z'},{...f,price:'NaN'}]);
 const markers=graph.children.filter(n=>n.tag==='path');assert.equal(markers.length,1);
 assert.equal(markers[0].attrs['data-execution-key'],'owned');assert.match(markers[0].children[0].textContent,/9007199254740993123/);
 assert.match(markers[0].children[0].textContent,/102.000000000000001/);assert.match(markers[0].attrs.d,/M 332 /);
 const empty=context.chart(null,[{price:'100'},{price:'101'}],101,{interval:'1m',start_ms:60000,end_ms:119999,symbol:'BTC',candles:[]});
 assert.equal(empty.children.filter(n=>n.tag==='polyline').length,0);assert.ok(empty.children.some(n=>n.textContent?.includes('无 K 线')));
});

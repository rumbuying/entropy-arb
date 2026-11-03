import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { presentOrder } from '../entropy_arb/webui/v2/grid/order_view.js';

const raw = await fs.readFile(new URL('../entropy_arb/webui/v2/grid/detail.js', import.meta.url), 'utf8');
const source = raw.replace(/^import[\s\S]*?;\n/gm, '').replace('export function mount', 'function mount') + '\nglobalThis.mount=mount;';
const settle = () => new Promise(resolve => setImmediate(resolve));
const detail = () => ({execution_mode:'production', performance:{net_pnl:'0',total_fees:'0',funding_net:'0'},
  grid:{status:'RUNNING',desired_mode:'ACTIVE',active_revision:1,net_position:'1',mark:'100',
    plan:{market:'||ETH|linear_perp|USDC',qty_per_grid:'1'},budget:{reserved_amount:'40'},
    config:{market_ref:'||ETH|linear_perp|USDC',account_ref:'test',direction:'long',grid_count:5,
      lower_price:'90',upper_price:'110',leverage:3,spacing:'arithmetic',initial_position:'immediate',
      margin_mode:'cross',execution_policy:'gtc',capital:{asset:'USDC',total:'40',reserve:'20'}}}});
const rows = [{side:'buy',price:'102',qty_base:'1',cumulative_qty:'1',state:'FILLED',cause_key:'initial-position:test:e1',revision:1},
  {side:'sell',price:'104',qty_base:'1',cumulative_qty:'0',state:'ACCEPTED',cause_key:'refill:native-source',reduce_only:true,revision:1}];

function harness() {
  const gets=[],posts=[],timers=new Map(),dialogs=[],chartUpdates=[];
  let timer=0;
  const descendants = node => [node,...(node.children||[]).flatMap(child=>typeof child==='object'&&child?descendants(child):[])];
  const el=(tag,attrs={},...children)=>{
    const node={tag,...attrs,textContent:String(attrs.text??''),className:attrs.class||'',children,
      append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;},
      setAttribute(key,value){this[key]=value;},
      querySelectorAll(selector){return descendants(this).filter(n=>n!==this&&n.tag===selector);},
      insertBefore(child,before){this.children.splice(this.children.indexOf(before),0,child);}};
    node.classList={add:value=>{node.className+=' '+value;}};
    return node;
  };
  const defer=(list,path,body)=>new Promise((resolve,reject)=>list.push({path,body,resolve,reject}));
  class CandlePanel {observeFills(){} update(...args){chartUpdates.push(args);} destroy(){}}
  const context=vm.createContext({el,CandlePanel,presentOrder,AbortController,Date,encodeURIComponent,
    gridGet:path=>defer(gets,path),gridPost:(path,body)=>defer(posts,path,body),gridDownload:()=>{},
    statusBadge:status=>({text:status,cls:'badge'}),allowedActions:g=>g.status==='RUNNING'?['pause','stop_flat','stop_keep']:[],
    newRequestId:()=> 'unit-request',panel:(title,...nodes)=>el('section',{},...nodes),shell:()=>el('nav'),
    kv:items=>el('dl',{items}),notice:(text,type='')=>el('div',{text,class:'grid-notice '+type}),
    dataTable:(headers,values)=>el('table',{headers},...values.map(values=>el('tr',{},...values.map(value=>typeof value==='object'?value:el('td',{text:value}))))),
    modal:(title,body)=>{const dialog=el('dialog',{open:true,title},body);dialog.close=()=>dialog.open=false;dialogs.push(dialog);return dialog;},
    field:()=>el('label'),directionName:{long:'多头'},orderState:{ACCEPTED:'已受理',FILLED:'已成交'},amount:value=>String(value??'—'),
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source,context);
  const root=el('main');const page=context.mount(root,{params:{gridId:'test'}});
  const take=part=>{const request=gets.find(r=>!r.taken&&r.path.includes(part));assert.ok(request,`missing GET ${part}`);request.taken=true;return request;};
  const find=(tag,text)=>descendants(root).find(n=>n.tag===tag&&n.textContent===text);
  const text=()=>descendants(root).map(n=>n.textContent).join(' ');
  return {page,gets,posts,dialogs,chartUpdates,timers,root,take,find,text,descendants};
}
async function loadedLedger(h) {
  h.take('/test').resolve(detail());await settle();
  // This response must be requested and rendered while the market request is unresolved.
  h.take('/orders?').resolve({orders:rows,total:2});await settle();
  return h.take('/market?');
}

test('pending and failed market reads cannot hide the ledger or disable valid lifecycle controls',async()=>{
  const h=harness();const quote=await loadedLedger(h);
  assert.ok(h.text().includes('初始建仓'));assert.ok(h.text().includes('网格补单 · 减仓'));
  assert.equal(h.find('button','结束并平仓').disabled,false);
  assert.equal(h.posts.length,0);
  quote.reject(new Error('quote only offline'));await settle();
  assert.ok(h.text().includes('行情暂时无法更新'));
  assert.ok(h.text().includes('初始建仓'));
  assert.equal(h.find('button','结束并平仓').disabled,false);
  assert.equal(h.posts.length,0);h.page.destroy();
});

test('unfinished manual close explains reconciliation and possible remaining exposure',async()=>{
  const h=harness();const value=detail();
  value.grid.status='NEEDS_ATTENTION';value.grid.desired_mode='STOPPED';
  value.grid.protection_exit={phase:'reconcile',reason:'stop_flat_transient_reconciliation'};
  h.take('/test').resolve(value);await settle();
  h.take('/orders?').resolve({orders:rows,total:2});await settle();
  assert.ok(h.text().includes('结束平仓尚未完成，可能仍有仓位或挂单'));
  assert.ok(h.text().includes('请勿重复提交平仓'));
  assert.equal(h.posts.length,0);
  h.take('/market?').reject(new Error('quote offline'));await settle();h.page.destroy();
});

test('a hanging quote cannot block later ledger refreshes or create duplicate quote requests',async()=>{
  const h=harness();const quote=await loadedLedger(h);
  const newer=h.page.refresh();const fresh=detail();fresh.grid.net_position='2';
  h.take('/test').resolve(fresh);await settle();
  h.take('/orders?').resolve({orders:[...rows,{...rows[1],price:'106',state:'FILLED'}],total:3});
  await newer;
  assert.ok(h.text().includes('106'));
  assert.equal(h.gets.filter(r=>r.path.includes('/market?')).length,1);
  assert.equal(h.find('button','结束并平仓').disabled,false);
  assert.equal(h.posts.length,0);
  quote.resolve({history:[],snapshot:{mark:'105'}});await settle();
  assert.equal(h.chartUpdates.length,1);h.page.destroy();
});

test('a later detail failure still disables existing controls and preserves the previous ledger',async()=>{
  const h=harness();(await loadedLedger(h)).reject(new Error('quote offline'));await settle();
  const refreshing=h.page.refresh();h.take('/test').reject(new Error('detail offline'));await refreshing;
  assert.equal(h.find('button','结束并平仓').disabled,true);
  assert.ok(h.text().includes('初始建仓'));assert.ok(h.text().includes('数据暂时无法更新'));
  assert.equal(h.posts.length,0);h.page.destroy();
});

test('quote failure does not bypass operation preview or arm a failed preview',async()=>{
  const h=harness();(await loadedLedger(h)).reject(new Error('quote offline'));await settle();
  // In-memory UI only; every write is a deferred test double.
  const confirming=h.find('button','结束并平仓').onclick();
  assert.equal(h.posts.length,1);assert.match(h.posts[0].path,/\/operations\/previews$/);
  assert.equal(h.posts[0].body.action,'stop_flat');
  const confirm=h.descendants(h.dialogs[0]).find(n=>n.tag==='button'&&n.textContent==='确认执行');
  assert.equal(confirm.disabled,true);
  h.posts[0].reject(new Error('preview not available'));await confirming;
  assert.equal(confirm.disabled,true);assert.equal(confirm.onclick,undefined);assert.equal(h.posts.length,1);h.page.destroy();
});

test('late market failures cannot overwrite a newer successful refresh or a destroyed page',async()=>{
  const h=harness();const old=await loadedLedger(h);
  const newer=h.page.refresh();const revision2=detail();revision2.grid.active_revision=2;h.take('/test').resolve(revision2);await settle();
  h.take('/orders?').resolve({orders:rows,total:2});await settle();
  h.take('/market?').resolve({history:[],snapshot:{mark:'101'}});await newer;await settle();
  assert.equal(h.chartUpdates.length,1);
  old.reject(new Error('late old error'));await settle();
  assert.ok(!h.text().includes('行情暂时无法更新'));
  const last=h.page.refresh();h.take('/test').resolve(detail());await settle();
  h.take('/orders?').resolve({orders:rows,total:2});await settle();
  const late=h.take('/market?');h.page.destroy();late.resolve({history:[],snapshot:{mark:'200'}});await last;
  assert.equal(h.chartUpdates.length,1);assert.equal(h.timers.size,0);
});

test('whole-position residual exits remain exits rather than protective stops',()=>{
  const native={whole_position:true,trigger_price:'99',price:'98',qty_base:'0.001',state:'OPEN',reduce_only:true};
  assert.equal(presentOrder({...native,cause_key:'protection:g:e1:sell'}).purpose,'止损保护');
  const exit=presentOrder({...native,cause_key:'exit-flat:g:native-residual:3'});
  assert.equal(exit.purpose,'残仓退出');assert.equal(exit.quantity,'整个仓位');
  assert.equal(exit.waitingTrigger,true);assert.equal(exit.price,'99 · 退出触发');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

const raw = await fs.readFile(new URL('../entropy_arb/webui/v2/grid/create.js',import.meta.url),'utf8');
const source = raw.replace(/^import[\s\S]*?;\n/gm,'').replace('export function mount','function mount')+'\nglobalThis.mount=mount;';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const market={market_ref:'bulk:mainnet||BTC-USD|linear_perp|USDC',symbol:'BTC-USD',base_asset:'BTC',asset:'USDC',max_leverage:40,live_eligible:false};
const bulk={account_ref:'bulk-account',credential_ref:'bulk-credential',name:'Bulk',venue:'bulk',simulated:false,
  preview_only:true,preview_capabilities:{directions:['long'],margin_modes:['cross'],protection_modes:['native']},markets:[market]};

function harness(){
  const gets=[],posts=[],queues=[];
  const descend=node=>[node,...(node.children||[]).flatMap(n=>typeof n==='object'&&n?descend(n):[])];
  const el=(tag,attrs={},...children)=>{
    const node={tag,...attrs,textContent:String(attrs.text??''),className:attrs.class||'',children:children.flat(),value:attrs.value??'',
      setAttribute(k,v){this[k]=v;},getAttribute(k){return this[k]??null;},addEventListener(){},
      append(...nodes){for(const n of nodes){this.children.push(n);if(this.tag==='select'&&(this.value===''||n.selected))this.value=n.value;}},
      replaceChildren(...nodes){this.children=[];if(this.tag==='select')this.value='';this.append(...nodes);},
      querySelectorAll(selector){return descend(this).filter(n=>n!==this&&selector.split(',').includes(n.tag));}};
    return node;
  };
  const defer=(list,path,body)=>new Promise((resolve,reject)=>list.push({path,body,resolve,reject}));
  class PreviewQueue{constructor(run){this.run=run;this.running=false;queues.push(this);}request(){}foreground(){}close(){}}
  class CandlePanel{update(){}destroy(){}}
  const context=vm.createContext({el,PreviewQueue,CandlePanel,AbortController,Date,encodeURIComponent,structuredClone,
    gridGet:path=>defer(gets,path),gridPost:(path,body)=>defer(posts,path,body),newRequestId:()=> 'test-request',
    qualifiedDefaults:()=>null,panel:(title,...nodes)=>el('section',{},...nodes),shell:()=>el('nav'),
    kv:rows=>el('dl',{},...rows.map(row=>el('dd',{text:row[1]}))),field:(name,input)=>el('label',{text:name},input),
    notice:(text,type)=>el('div',{text,class:'grid-notice '+type}),dataTable:()=>el('table'),
    directionName:{long:'做多',neutral:'中性',short:'做空'},amount:value=>String(value??'—'),
    document:{hidden:false,addEventListener(){},removeEventListener(){}},setInterval:()=>1,clearInterval(){},location:{hash:''}});
  vm.runInContext(source,context);const root=el('main');const page=context.mount(root);
  const take=(list,part)=>{const r=list.find(r=>!r.taken&&r.path.includes(part));assert.ok(r,`missing ${part}`);r.taken=true;return r;};
  const find=(tag,text)=>descend(root).find(n=>n.tag===tag&&n.textContent===text);
  return{page,root,gets,posts,queues,find,descend,takeGet:part=>take(gets,part),takePost:part=>take(posts,part),
    text:()=>descend(root).map(n=>n.textContent).join(' ')};
}

test('Bulk account renders a long cross preview, preserves quantities, and cannot create a live grid',async()=>{
  const h=harness();h.takeGet('/catalog').resolve({execution_mode:'production',real_dispatch:false,accounts:[bulk]});await settle();
  assert.equal(h.find('button','中性').disabled,true);assert.equal(h.find('button','做空').disabled,true);
  assert.equal(h.find('button','做多')['aria-pressed'],true);
  assert.equal(h.find('option','逐仓（需交易所支持）').disabled,true);
  assert.equal(h.find('option','原生止损 + 服务端过渡保护').disabled,true);
  h.takeGet('/market?').resolve({snapshot:{mark:'100'},default_lower:'90',default_upper:'110',history:[],
    rules:{base_asset:'BTC',tick:'1',maker_fee_rate:'0.0001'}});await settle();
  const request=h.queues[0].run();const preview=h.takePost('/api/grid/previews');
  assert.equal(preview.body.config.direction,'long');assert.equal(preview.body.config.margin_mode,'cross');
  preview.resolve({plan:{validation_status:'BLOCKED',code:'CAPABILITY_UNVERIFIED',live_eligible:false,
    qty_per_grid:'0.12345678',levels:[],candidate_orders:[],initial_position_qty:'0'},
    sizing:{validation_status:'VALID',ordinary_required:'18',adverse_required:'38'},preview_id:'preview',expires_ts:Date.now()/1000+30});
  await request;await settle();
  assert.ok(h.text().includes('规模预览已计算'));assert.ok(h.text().includes('当前不会下单'));
  assert.ok(h.text().includes('0.12345678 BTC'));
  const button=h.find('button','真实开单待验证');assert.ok(button.disabled);
  await button.onclick();assert.equal(h.posts.length,1);h.page.destroy();
});

test('switching away from Bulk restores other account choices instead of leaking its restrictions',async()=>{
  const h=harness();h.takeGet('/catalog').resolve({execution_mode:'production',real_dispatch:false,
    accounts:[bulk,{...bulk,account_ref:'hl-account',credential_ref:'hl-credential',venue:'hl',name:'HL',preview_only:false,
      preview_capabilities:{},markets:[{...market,market_ref:'||ETH|linear_perp|USDC',symbol:'ETH',base_asset:'ETH'}]}]});
  await settle();const select=h.descend(h.root).find(n=>n.tag==='select'&&n['aria-label']==='交易账户');
  select.value='hl-account';select.onchange();
  assert.equal(h.find('button','中性').disabled,false);assert.equal(h.find('button','做空').disabled,false);
  assert.equal(h.find('option','逐仓（需交易所支持）').disabled,false);
  assert.equal(h.find('option','原生止损 + 服务端过渡保护').disabled,false);
  assert.ok(h.gets.some(r=>r.path.includes('account_ref=hl-account')));h.page.destroy();
});

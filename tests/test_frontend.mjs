// Offline DOM contract checks. Run: node tests/test_frontend.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.value='';this.checked=false;this.textContent='';this.open=false;this.classList={add(){},remove(){},toggle(){}};}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  addEventListener() {}
  setAttribute() {}
  reportValidity() {return true;}
  querySelectorAll() {return [];}
  scrollIntoView() {}
  close() {this.open=false;}
  showModal() {this.open=true;}
}
const html=readFileSync(new URL('../creator_archive/static/index.html',import.meta.url),'utf8');
const elements=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match=>[match[1],new Element()]));
elements['filter-sort'].value='published_at';elements['filter-order'].value='desc';
const document={getElementById:id=>elements[id],createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),querySelectorAll:()=>[],documentElement:{dataset:{}}};
const context=vm.createContext({document,URL,URLSearchParams,Option:class extends Element {constructor(text,value){super('option');this.textContent=text;this.value=value;}},location:{origin:'http://127.0.0.1:8765'},localStorage:{getItem(){},setItem(){}},setInterval(){},fetch(){throw new Error('Unexpected network call');}});
let source=readFileSync(new URL('../creator_archive/static/app.js',import.meta.url),'utf8');
source=source.replace('refresh(true).catch(e=>notify(e.message,true));setInterval','setInterval');
vm.runInContext(source,context);
const run=code=>vm.runInContext(code,context);
assert.equal(run('metricValue({value:0,quality:"exact"})'),'0');
assert.equal(run('metricValue({value:null,quality:"unknown"})'),'未知');
assert.equal(run('metricValue({value:12000,quality:"approximate"})'),'约 12,000');
assert.equal(run('metricValue({value:100,quality:"lower_bound"})'),'≥ 100');
assert.match(run('metricEvidence({value:10,source:"detail",collected_at:100,last_attempt_at:200,last_attempt_status:"failed"})'),/更新失败.*已有有效值及时间保留/);
run('globalThis.calls=[];api=async path=>{calls.push(path);return {total:42,items:[{platform:"xiaohongshu",author_id:"author",item_id:"cross-page-result",metrics:{likes:{value:200,quality:"exact"}}}]};}');
elements['filter-author'].value='xiaohongshu|author';
elements['filter-type'].value='video';elements['filter-sort'].value='likes';elements['filter-order'].value='asc';
elements['filter-min-likes'].value='0';elements['filter-min-collects'].value='10';elements['filter-min-comments'].value='2';
elements['filter-date-from'].value='2026-01-01';elements['filter-date-to'].value='2026-09-27';
elements['filter-missing'].value='comments';elements['filter-assets'].checked=true;
await run('applyFilters()');
let params=new URL(run('calls.at(-1)'), 'http://local').searchParams;
assert.equal(params.get('offset'),'0');assert.equal(params.get('author_id'),'author');assert.equal(params.get('platform'),'xiaohongshu');
for(const [key,value] of Object.entries({content_type:'video',sort:'likes',order:'asc',min_likes:'0',min_collects:'10',min_comments:'2',date_from:'2026-01-01',date_to:'2026-09-27',missing_metric:'comments',has_assets:'true'})) assert.equal(params.get(key),value);
assert.match(elements['item-count'].textContent,/42/);
assert.equal(elements['item-list'].children.length,1,'Server results rendered without per-page client filtering');
await run('ui.offset=20;loadItems()');
params=new URL(run('calls.at(-1)'), 'http://local').searchParams;
assert.equal(params.get('offset'),'20');assert.equal(params.get('min_likes'),'0');assert.equal(params.get('sort'),'likes');
const requestCount=run('calls.length');elements['filter-date-from'].value='2026-10-01';
await run('applyFilters()');assert.equal(run('calls.length'),requestCount,'Invalid dates must not replace applied filters');assert.match(elements.feedback.textContent,/起始日期不能晚于/);
run('globalThis.pending=[];api=path=>new Promise(resolve=>pending.push({path,resolve}));globalThis.firstLoad=loadItems();ui.offset=0;globalThis.lastLoad=loadItems();');
run('pending[1].resolve({total:1,items:[]})');await run('lastLoad');
run('pending[0].resolve({total:999,items:[]})');await run('firstLoad');assert.equal(run('ui.total'),1,'Older requests cannot overwrite latest applied results');
run('$("item-dialog").open=true;notify("作者当前正在冷却，已有进度保留。",true)');
assert.equal(elements['detail-feedback'].hidden,false,'Modal errors must remain visible inside dialog');
assert.match(elements['detail-feedback'].textContent,/冷却/);
run('globalThis.jobBody=null;api=async (path,body)=>{jobBody=body;return {job_id:7}};refresh=async()=>{}');
await run('startJob("metrics",{platform:"xiaohongshu",author_id:"author",item_id:"item"})');
assert.deepEqual(JSON.parse(run('JSON.stringify(jobBody)')),{mode:'metrics',platform:'xiaohongshu',author_id:'author',item_id:'item'});
assert.equal(elements['item-dialog'].open,false,'Successful enqueue returns user to durable jobs');
await run('startJob("content",{platform:"xiaohongshu",author_id:"author",item_id:"item",source_url:"https://ignored.invalid/automatic"}," https://www.xiaohongshu.com/explore/item?xsec_token=synthetic-test ")');
assert.equal(run('jobBody.source_url'),'https://www.xiaohongshu.com/explore/item?xsec_token=synthetic-test');
await run('startJob("content",{platform:"xiaohongshu",author_id:"author",item_id:"item",source_url:"https://ignored.invalid/automatic"})');
assert.equal(run('jobBody.source_url'),undefined,'Saved metadata links are never passed as explicit user input');
await run('startJob("content",{platform:"xiaohongshu",author_id:"author"},"https://www.xiaohongshu.com/explore/item")');
assert.equal(run('jobBody.source_url'),undefined,'Transient source links must never leak to author batches');
await run('startJob("metrics",undefined,"https://www.xiaohongshu.com/explore/item")');
assert.equal(run('jobBody.source_url'),undefined,'Transient source links must never leak to all-author batches');
console.log('Frontend offline checks passed: metric semantics, full-library query contract, pagination, invalid dates, stale response guard, modal errors, metrics-only job payload, explicit transient single-item link scope.');

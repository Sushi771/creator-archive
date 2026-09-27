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

run('globalThis.recoveryCalls=[];api=async(path,body)=>{recoveryCalls.push({path,body});return body?{job_id:19}:{total:1,items:[{item_id:"failed",message:"引用缺失",next_step:"补充链接"}]};}');
await run('openRecovery({id:19,can_resume:true})');
assert.equal(elements['recovery-dialog'].open,true);
assert.match(elements['recovery-page'].textContent,/1 个未完成/);
elements['recovery-links'].value=' https://www.xiaohongshu.com/explore/a?xsec_token=synthetic-one\nhttps://www.xiaohongshu.com/explore/b?xsec_token=synthetic-two ';
await run('$("recovery-resume").onclick()');
assert.equal(run('recoveryCalls.at(-1).path'),'/api/jobs/19/resume');
assert.equal(run('recoveryCalls.at(-1).body.source_urls.length'),2);
assert.equal(elements['recovery-links'].value,'');
assert.equal(elements['recovery-dialog'].open,false);
console.log('Recovery frontend checks passed: original job scope, batch links, clear on submission.');

run('globalThis.itemCalls=[];api=async(path,body)=>{itemCalls.push({path,body});return path==="/api/items/resolve"?{platform:"xiaohongshu",author_id:"a".repeat(24),item_id:"b".repeat(24)}:{job_id:29};}');
const fullLink='https://www.xiaohongshu.com/explore/'+ 'b'.repeat(24) +'?xsec_token=SYNTHETIC_ONLY';
elements['resolve-item-link'].value='分享这篇 '+fullLink;
await run('resolveAndSaveItem()');
assert.deepEqual(JSON.parse(run('JSON.stringify(itemCalls.map(call=>call.path))')),['/api/items/resolve','/api/jobs']);
assert.deepEqual(JSON.parse(run('JSON.stringify(itemCalls[1].body)')),{mode:'content',platform:'xiaohongshu',author_id:'a'.repeat(24),item_id:'b'.repeat(24),source_url:fullLink},'Only the verified item enters the persistent content job');
assert.equal(elements['resolve-item-link'].value,'','Successful submission clears the transient link');
assert.match(elements['resolve-item-result'].textContent,/任务 #29 已创建/);
run('itemCalls=[];api=async(path,body)=>{itemCalls.push({path,body});throw new Error("登录失效；请在专用浏览器重新登录。")};');
elements['resolve-item-link'].value=fullLink;
await assert.rejects(run('resolveAndSaveItem()'),/登录失效/);
assert.equal(run('itemCalls.length'),1,'Failed verification cannot create a content job');
assert.equal(elements['resolve-item-link'].value,fullLink,'Failed verification keeps the input for retry');
run('itemCalls=[];api=async(path,body)=>{itemCalls.push({path,body});if(path==="/api/items/resolve")return {platform:"xiaohongshu",author_id:"a".repeat(24),item_id:"b".repeat(24)};throw new Error("该作者已有任务排队或运行")};');
await assert.rejects(run('resolveAndSaveItem()'),/已核验收录，但保存任务未创建/);
assert.equal(run('itemCalls.length'),2);
assert.equal(elements['resolve-item-link'].value,fullLink,'Failed enqueue keeps the input');
assert.match(elements['resolve-item-result'].textContent,/作品资料库/);
console.log('Full-link UI checks passed: verified single-item job, failure isolation, transient link clearing.');

run('itemCalls=[];api=async(path,body)=>{itemCalls.push({path,body});return path==="/api/items/resolve"?{platform:"xiaohongshu",author_id:"a".repeat(24),item_id:"b".repeat(24),resolved_from_short_link:true}:{job_id:30};}');
elements['resolve-item-link'].value='Share https://xhslink.cn/o/synthetic';
await run('resolveAndSaveItem()');
assert.deepEqual(JSON.parse(run('JSON.stringify(itemCalls[1].body)')),{mode:'content',platform:'xiaohongshu',author_id:'a'.repeat(24),item_id:'b'.repeat(24)},'Short-link job uses only verified IDs and the server memory reference');
assert.equal(elements['resolve-item-link'].value,'');
console.log('Short-link UI checks passed: exact single-item scope, no short URL forwarded as detail source.');

run('ui.workspace={subscriptions:[{platform:"xiaohongshu",author_id:"a".repeat(24),display_name:"Observed",subscription_confirmation_required:true}]};itemCalls=[];api=async(path,body)=>{itemCalls.push({path,body});return path==="/api/items/resolve"?{platform:"xiaohongshu",author_id:"a".repeat(24),item_id:"b".repeat(24),subscription_confirmation_required:true}:{job_id:31};}');
elements['resolve-item-link'].value=fullLink;
await run('resolveAndSaveItem()');
assert.equal(elements['confirm-subscription-dialog'].open,true,'Verified note offers a separate subscription decision');
assert.deepEqual(JSON.parse(run('JSON.stringify(itemCalls.map(call=>call.path))')),['/api/items/resolve','/api/jobs'],'Saving one note does not silently confirm the author');
assert.match(elements['resolve-item-result'].textContent,/确认本身不会扫描历史/);
elements['confirm-subscription-dialog'].close();
run('globalThis.confirmCalls=[];api=async(path,body)=>{confirmCalls.push({path,body});return {subscription_confirmed:true,enabled:true,message:"已确认订阅；不会自动扫描历史。"};}');
run('openSubscriptionConfirmation({platform:"xiaohongshu",author_id:"a".repeat(24),display_name:"Observed",subscription_confirmation_required:true})');
assert.equal(elements['confirm-subscription-dialog'].open,true);
assert.match(elements['confirm-subscription-author'].textContent,/Observed/);
await run('confirmSubscription()');
assert.deepEqual(JSON.parse(run('JSON.stringify(confirmCalls)')),[{path:'/api/subscriptions/confirm',body:{platform:'xiaohongshu',author_id:'a'.repeat(24)}}]);
assert.equal(elements['confirm-subscription-dialog'].open,false);
assert.match(elements.feedback.textContent,/不会自动扫描历史/);
run('refresh=async()=>{throw new Error("offline")};openSubscriptionConfirmation({platform:"xiaohongshu",author_id:"a".repeat(24),subscription_confirmation_required:true})');
await run('confirmSubscription()');
assert.equal(elements['confirm-subscription-dialog'].open,false,'Persisted confirmation closes even if refresh fails');
assert.match(elements.feedback.textContent,/订阅已确认，但页面刷新失败/);
run('refresh=async()=>{}');
run('api=async()=>{throw new Error("确认暂不可用，请重试")};openSubscriptionConfirmation({platform:"xiaohongshu",author_id:"a".repeat(24),subscription_confirmation_required:true})');
await assert.rejects(run('confirmSubscription()'),/确认暂不可用/);
assert.equal(elements['confirm-subscription-dialog'].open,true,'Failed confirmation keeps the decision visible');
assert.match(elements['confirm-subscription-error'].textContent,/已核验作品与旧任务保留/);
console.log('Author confirmation UI checks passed: explicit identity, durable request, no history job, retry state.');

run('globalThis.pageCalls=[];api=async(path,body)=>{pageCalls.push({path,body});return body?{job_id:32}:{total:0,items:[]};};refresh=async()=>{}');
await run('startJob("page_archive",{platform:"xiaohongshu",author_id:"author"})');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls[0].body)')),{mode:'page_archive',platform:'xiaohongshu',author_id:'author'});
await run('openRecovery({id:32,mode:"page_archive",can_resume:true})');
assert.equal(elements['recovery-link-fields'].hidden,true);
assert.match(elements['recovery-resume'].textContent,/原两页任务/);
await run('$("recovery-resume").onclick()');
assert.equal(run('pageCalls.at(-1).path'),'/api/jobs/32/resume');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.at(-1).body)')),{});
run('renderJobs([{id:32,mode:"page_archive",state:"interrupted",pages:1,page_limit:2,target_count:10,item_count:9,failed_count:1,pending_count:0,coverage:"partial",can_resume:true}])');
assert.match(elements['job-list'].children[0].children[1].textContent,/列表 1 \/ 2 页.*正文媒体 9 \/ 10 篇/);
console.log('Page archive UI checks passed: explicit two-page scope, separate progress, parent recovery without unsupported links.');

const flattenText=element=>[element.textContent,...(element.children||[]).map(flattenText)].join(' ');
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"author",identity_verified:true,enabled:true}])');
assert.match(flattenText(elements['subscription-list']),/单作者全历史归档（实验）/);
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"pending",identity_verified:true,subscription_confirmation_required:true}])');
assert.doesNotMatch(flattenText(elements['subscription-list']),/单作者全历史归档（实验）/,'Pending subscription authors cannot start a full-author pipeline');
run('renderSubscriptions([{platform:"wechat",author_id:"author",identity_verified:true,enabled:true}])');
assert.doesNotMatch(flattenText(elements['subscription-list']),/单作者全历史归档（实验）/,'Unvalidated platforms do not expose this experiment');
run('pageCalls=[]');
await run('startJob("author_archive",{platform:"xiaohongshu",author_id:"author"})');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls[0].body)')),{mode:'author_archive',platform:'xiaohongshu',author_id:'author'},'Full-author collection creates a separate mode and never resumes or expands old task 32');
await run('openRecovery({id:35,mode:"author_archive",can_resume:true})');
assert.equal(elements['recovery-link-fields'].hidden,true);
assert.match(elements['recovery-resume'].textContent,/继续原单作者任务/);
await run('$("recovery-resume").onclick()');
assert.equal(run('pageCalls.at(-1).path'),'/api/jobs/35/resume');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.at(-1).body)')),{});
run('renderJobs([{id:35,mode:"author_archive",state:"interrupted",pages:4,page_limit:null,list_finished:false,target_count:90,item_count:80,failed_count:1,pending_count:9,coverage:"partial",can_resume:true}])');
let fullProgress=elements['job-list'].children[0].children[1].textContent;
assert.match(fullProgress,/列表已保存 4 页.*尚未核验末页.*正文媒体 80 \/ 90 篇.*已尝试未完成 1.*待处理 9/);
assert.doesNotMatch(fullProgress,/4 \/ (0|2)/,'Unknown full-author page count is never rendered as a denominator');
run('renderJobs([{id:35,mode:"author_archive",state:"partial",pages:5,page_limit:null,list_finished:true,target_count:100,item_count:99,failed_count:1,pending_count:0,coverage:"complete_for_accessible_scope",can_resume:true},{id:36,mode:"content",state:"partial",parent_job_id:35}])');
fullProgress=elements['job-list'].children[0].children[1].textContent;
assert.match(fullProgress,/已核验可信末页.*正文媒体 99 \/ 100 篇.*已尝试未完成 1/,'Trusted list completion retains the separate incomplete content count');
assert.match(flattenText(elements['job-list'].children[1]),/属于按页归档任务 #35/);
assert.doesNotMatch(flattenText(elements['job-list'].children[1]),/两页任务/);
console.log('Author archive UI checks passed: separate author scope, unknown page total, independent terminal/content progress, parent-only recovery and subscription/platform boundaries.');

const visibleText=element=>element.tagName==='details'?visibleText(element.children[0]):[element.textContent,...(element.children||[]).map(visibleText)].join(' ');
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"author",identity_verified:true,enabled:true}])');
assert.match(visibleText(elements['subscription-list']),/测试前10篇并归档/);
assert.doesNotMatch(visibleText(elements['subscription-list']),/单作者全历史归档（实验）|两页采集并归档（实验）|保存全部已收录内容/,'Broad jobs are collapsed by default');
assert.match(flattenText(elements['subscription-list']),/完整归档实验（超出本轮10篇测试）.*单作者全历史归档/,'Existing P0 experiments remain available in advanced controls');
const batchAdvanced=html.match(/<details class="archive-experiments">[\s\S]*?<\/details>/)?.[0];
assert.ok(batchAdvanced);
assert.match(batchAdvanced,/data-job="full"/);
assert.match(batchAdvanced,/data-job="content"/);
assert.doesNotMatch(batchAdvanced,/data-job="demo_archive"/);
assert.match(html.replace(batchAdvanced,''),/data-job="demo_archive"[^>]*>测试各作者前10篇/);
assert.doesNotMatch(html.replace(batchAdvanced,''),/data-job="(?:full|content|metrics|latest)"/,'Broad bulk actions are not primary controls');
run('pageCalls=[]');
await run('startJob("demo_archive",{platform:"xiaohongshu",author_id:"author"})');
await run('startJob("demo_archive")');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.map(call=>call.body))')),[{mode:'demo_archive',platform:'xiaohongshu',author_id:'author'},{mode:'demo_archive'}],'Demo requests keep the server-fixed limit and do not resume old jobs');
assert.match(elements.feedback.textContent,/已核验并确认的小红书作者（含暂停项）/);
run('renderJobs([{id:41,mode:"demo_archive",state:"succeeded",item_limit:10,pages:1,target_count:10,item_count:10,failed_count:0,pending_count:0,coverage:"partial",list_finished:false,library_item_count:144}])');
const demoProgress=elements['job-list'].children[0].children[1].textContent;
assert.match(demoProgress,/固定上限 10 篇.*已纳入 10 篇.*成功 10 \/ 10 篇.*包含已有资源复用.*不代表历史完整/);
assert.doesNotMatch(demoProgress,/144|历史列表已至末页|本次列表已到可信末页/,'Library totals and successful demo status never imply new downloads or terminal history');
await run('openRecovery({id:41,mode:"demo_archive",can_resume:true})');
assert.equal(elements['recovery-link-fields'].hidden,true);
assert.match(elements['recovery-resume'].textContent,/继续原前10篇测试/);
await run('$("recovery-resume").onclick()');
assert.equal(run('pageCalls.at(-1).path'),'/api/jobs/41/resume');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.at(-1).body)')),{});
console.log('Demo UI checks passed: primary fixed-ten scope, filtered-author batch request, collapsed full experiments, resource reuse semantics and original demo recovery.');

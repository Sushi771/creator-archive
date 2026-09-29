// Offline DOM contract checks. Run: node tests/test_frontend.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.value='';this.checked=false;this.textContent='';this.open=false;this.handlers={};this.classList={add(){},remove(){},toggle(){}};}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  addEventListener(event,callback) {this.handlers[event]=callback;}
  closest(selector) {return selector==='button'&&this.tagName==='button'?this:null;}
  setAttribute() {}
  reportValidity() {return true;}
  reset() {}
  querySelectorAll() {return [];}
  scrollIntoView() {}
  close() {this.open=false;}
  showModal() {this.open=true;}
}
const html=readFileSync(new URL('../creator_archive/static/index.html',import.meta.url),'utf8');
const elements=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match=>[match[1],new Element()]));
elements['filter-sort'].value='published_at';elements['filter-order'].value='desc';
const document={getElementById:id=>elements[id],createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),querySelectorAll:()=>[],documentElement:{dataset:{}},handlers:{},addEventListener(event,callback){this.handlers[event]=callback;}};
const timers=new Map(),globalHandlers={};let nextTimer=1;
const flushTimers=()=>{const pending=[...timers.values()];timers.clear();for(const callback of pending)callback();};
const context=vm.createContext({document,URL,URLSearchParams,Option:class extends Element {constructor(text,value){super('option');this.textContent=text;this.value=value;}},location:{origin:'http://127.0.0.1:8765'},localStorage:{getItem(){},setItem(){}},setInterval(){},setTimeout(callback){const id=nextTimer++;timers.set(id,callback);return id;},clearTimeout(id){timers.delete(id);},addEventListener(event,callback){globalHandlers[event]=callback;},fetch(){throw new Error('Unexpected network call');}});
let source=readFileSync(new URL('../creator_archive/static/app.js',import.meta.url),'utf8');
source=source.replace('refresh(true).catch(e=>notify(e.message,true));setInterval','setInterval');
vm.runInContext(source,context);
const run=code=>vm.runInContext(code,context);
run('globalThis.realRefresh=refresh');
assert.equal(run('metricValue({value:0,quality:"exact"})'),'0');
assert.equal(run('metricValue({value:null,quality:"unknown"})'),'未知');
assert.equal(run('metricValue({value:12000,quality:"approximate"})'),'约 12,000');
assert.equal(run('metricValue({value:100,quality:"lower_bound"})'),'≥ 100');
assert.match(run('metricEvidence({value:10,source:"detail",collected_at:100,last_attempt_at:200,last_attempt_status:"failed"})'),/更新失败.*已有有效值及时间保留/);
// Trigger: an observation contains no valid metric, but has an observation time.
// Expected: the time remains an observation time, never an effective value time.
assert.match(run('metricEvidence({value:null,quality:"unknown",source:"detail",collected_at:100,last_attempt_at:100,last_attempt_status:"missing"})'),/有效值时间：未知/);
assert.doesNotMatch(run('metricEvidence({value:null,quality:"unknown",last_attempt_status:"missing"})'),/已有有效值及时间保留/);
assert.match(run('metricEvidence({value:null,quality:"unknown",raw:"",last_attempt_status:"missing"})'),/原始字段：空字符串.*不能当作 0.*未取得有效数字，原因未确认/);
assert.doesNotMatch(run('metricEvidence({value:null,quality:"unknown",raw:null,last_attempt_status:"failed"})'),/原始字段：空字符串/);
run('api=async()=>({item:{platform:"xiaohongshu",author_id:"author",item_id:"sample",metrics:{likes:{value:null,quality:"unknown"}},metric_snapshots:[{collected_at:100,source:"detail",status:"partial",metrics:{likes:{value:null,quality:"unknown"}}}]}})');
await run('openItem({platform:"xiaohongshu",author_id:"author",item_id:"sample"})');
const walk=(entry)=>[entry,...(entry?.children||[]).flatMap(walk)];
const snapshot=walk(elements['detail-content']).find(entry=>entry?.className==='snapshot-entry');
const snapshotLikes=walk(snapshot).find(entry=>entry?.className==='metric-value'&&entry.children?.[0]?.textContent==='点赞');
assert.equal(snapshotLikes?.children?.[1]?.textContent,'未知');
assert.match(snapshotLikes?.title||'',/有效值时间：未知/);
const detailLikes=walk(elements['detail-content']).find(entry=>entry?.className==='metric-grid')?.children?.[0];
assert.equal(detailLikes?.children?.[1]?.textContent,'未知');
assert.match(detailLikes?.title||'',/有效值时间：未知/);
assert.ok(walk(elements['detail-content']).some(entry=>entry?.textContent?.includes('原20篇固定样本中的35项未知')),'Detail explanation scopes live evidence to the fixed sample');
assert.match(html,/原20篇固定样本的35项未知经诊断为原始空串、页面无数字/);
run('api=async()=>({item:{platform:"xiaohongshu",author_id:"author",item_id:"sample",assets:[{asset_id:"sample",kind:"image",state:"missing",url:"/archive/synthetic.png"}]}})');
await run('openItem({platform:"xiaohongshu",author_id:"author",item_id:"sample"})');
assert.equal(walk(elements['detail-content']).filter(entry=>entry?.tagName==='a'&&entry?.textContent==='打开附件').length,0,'Missing asset must not have a clickable link even with a stale URL');
assert.equal(walk(elements['detail-content']).filter(entry=>entry?.tagName==='img'||entry?.tagName==='video').length,0,'Missing asset must not preview stale media');
assert.ok(walk(elements['detail-content']).some(entry=>entry?.textContent?.includes('本地文件缺失或校验失败')));
run('api=async()=>({item:{platform:"xiaohongshu",author_id:"author",item_id:"sample",assets:[{asset_id:"sample",kind:"image",state:"complete",url:"/archive/synthetic.png"}]}})');
await run('openItem({platform:"xiaohongshu",author_id:"author",item_id:"sample"})');
assert.equal(walk(elements['detail-content']).filter(entry=>entry?.tagName==='a'&&entry?.textContent==='打开附件').length,1);
assert.equal(walk(elements['detail-content']).filter(entry=>entry?.tagName==='img').length,1);
run('globalThis.calls=[];api=async path=>{calls.push(path);return {total:42,items:[{platform:"xiaohongshu",author_id:"author",item_id:"cross-page-result",archive_status:"partial",metrics:{likes:{value:200,quality:"exact"}}}]};}');
elements['filter-author'].value='xiaohongshu|author';
elements['filter-platform'].value='xiaohongshu';elements['filter-text'].value='中文正文';elements['filter-archive-status'].value='partial';
elements['filter-type'].value='video';elements['filter-sort'].value='likes';elements['filter-order'].value='asc';
elements['filter-min-likes'].value='0';elements['filter-min-collects'].value='10';elements['filter-min-comments'].value='2';
elements['filter-date-from'].value='2026-01-01';elements['filter-date-to'].value='2026-09-27';
elements['filter-missing'].value='comments';elements['filter-assets'].checked=true;
await run('applyFilters()');
let params=new URL(run('calls.at(-1)'), 'http://local').searchParams;
assert.equal(params.get('offset'),'0');assert.equal(params.get('author_id'),'author');assert.equal(params.get('platform'),'xiaohongshu');
for(const [key,value] of Object.entries({text:'中文正文',archive_status:'partial',content_type:'video',sort:'likes',order:'asc',min_likes:'0',min_collects:'10',min_comments:'2',date_from:'2026-01-01',date_to:'2026-09-27',missing_metric:'comments',has_assets:'true'})) assert.equal(params.get(key),value);
assert.match(elements['item-count'].textContent,/42/);
assert.equal(elements['item-list'].children.length,1,'Server results rendered without per-page client filtering');
assert.ok(walk(elements['item-list']).some(entry=>entry?.textContent==='本机归档 部分'));
const libraryMetrics=walk(elements['item-list']).filter(entry=>entry?.className==='metric-value');
assert.deepEqual(libraryMetrics.map(entry=>entry.children?.[1]?.textContent),['200','未知','未知'],'Absent library metrics remain unknown');
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
assert.match(html,/id="resolve-item-form"[^>]*hidden/,'Legacy single-note browser action is not advertised in the default background-source UI');
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"author",identity_verified:true,enabled:true,source_connected:true,source_kind:"feed_http"}])');
assert.match(flattenText(elements['subscription-list']),/后台 Feed 已配置.*刷新此作者来源.*导出本机已有资料/);
assert.match(flattenText(elements['subscription-list']),/已登记图片 0 \/ 视频 0.*后台来源历史末页未验证/);
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"pending",identity_verified:true,subscription_confirmation_required:true}])');
assert.match(flattenText(elements['subscription-list']),/配置后台来源.*确认订阅/,'Pending subscription authors may configure a source before feed verification');
assert.doesNotMatch(flattenText(elements['subscription-list']),/刷新此作者来源/,'Pending subscription authors cannot refresh all content');
run('renderSubscriptions([{platform:"wechat",author_id:"author",identity_verified:true,enabled:true}])');
assert.doesNotMatch(flattenText(elements['subscription-list']),/旧浏览器历史诊断/,'Wechat does not expose the old XHS experiment');
run('pageCalls=[]');
await run('startJob("author_archive",{platform:"xiaohongshu",author_id:"author"})');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls[0].body)')),{mode:'author_archive',platform:'xiaohongshu',author_id:'author'},'Full-author collection creates a separate mode and never resumes or expands old task 32');
await run('openRecovery({id:35,mode:"author_archive",can_resume:true})');
assert.equal(elements['recovery-link-fields'].hidden,true);
assert.match(elements['recovery-resume'].textContent,/继续原单作者任务/);
await run('$("recovery-resume").onclick()');
assert.equal(run('pageCalls.at(-1).path'),'/api/jobs/35/resume');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.at(-1).body)')),{});
run('renderJobs([{id:35,mode:"author_archive",state:"interrupted",pages:4,page_limit:null,list_finished:false,target_count:90,item_count:80,failed_count:1,pending_count:9,coverage:"partial",can_resume:true,scan_url:"/api/jobs/35/scan"}])');
let fullProgress=elements['job-list'].children[0].children[1].textContent;
assert.match(fullProgress,/列表已保存 4 页.*尚未核验末页.*正文媒体 80 \/ 90 篇.*已尝试未完成 1.*待处理 9/);
assert.doesNotMatch(fullProgress,/4 \/ (0|2)/,'Unknown full-author page count is never rendered as a denominator');
assert.match(flattenText(elements['job-list']),/查看当前扫描范围/,'An interrupted author run exposes its durable observed scope before export');
run('renderJobs([{id:35,mode:"author_archive",state:"partial",pages:5,page_limit:null,list_finished:true,target_count:100,item_count:99,failed_count:1,pending_count:0,coverage:"complete_for_accessible_scope",can_resume:true},{id:36,mode:"content",state:"partial",parent_job_id:35}])');
fullProgress=elements['job-list'].children[0].children[1].textContent;
assert.match(fullProgress,/已核验可信末页.*正文媒体 99 \/ 100 篇.*已尝试未完成 1/,'Trusted list completion retains the separate incomplete content count');
assert.match(flattenText(elements['job-list'].children[1]),/属于按页归档任务 #35/);
assert.doesNotMatch(flattenText(elements['job-list'].children[1]),/两页任务/);
console.log('Author archive UI checks passed: separate author scope, unknown page total, independent terminal/content progress, parent-only recovery and subscription/platform boundaries.');

const visibleText=element=>element.tagName==='details'?visibleText(element.children[0]):[element.textContent,...(element.children||[]).map(visibleText)].join(' ');
const tagAuthors=[{platform:'xiaohongshu',author_id:'stable',display_name:'Renamed',tags:['读书'],identity_verified:true,enabled:true},{platform:'wechat',author_id:'wx',tags:['家庭'],identity_verified:false,enabled:false,subscribed:false}];
elements['subscription-platform'].value='';elements['subscription-tag'].value='';
run(`ui.workspace={subscriptions:${JSON.stringify(tagAuthors)}}`);
run(`renderSubscriptionTags(${JSON.stringify(tagAuthors)});renderSubscriptions(${JSON.stringify(tagAuthors)})`);
assert.match(flattenText(elements['subscription-list']),/读书.*家庭/);
elements['subscription-tag'].value='读书';elements['subscription-tag'].onchange();
assert.match(flattenText(elements['subscription-list']),/Renamed/);
assert.doesNotMatch(flattenText(elements['subscription-list']),/wx/);
assert.match(elements['subscription-filter-count'].textContent,/1 \/ 2.*不改变批次范围/);
elements['subscription-tag'].value='';elements['subscription-platform'].value='wechat';elements['subscription-platform'].onchange();
assert.match(flattenText(elements['subscription-list']),/家庭/);
assert.doesNotMatch(flattenText(elements['subscription-list']),/Renamed/);
elements['subscription-platform'].value='';
run(`renderSubscriptions(${JSON.stringify(tagAuthors)})`);
await walk(elements['subscription-list']).find(x=>x.textContent==='编辑标签').handlers.click();
assert.equal(elements['tag-dialog'].open,true);
assert.equal(elements['tag-input'].value,'读书');
run('globalThis.tagCalls=[];api=async(path,body)=>{tagCalls.push({path,body});return {message:"作者标签已保存"}};refresh=async()=>{}');
elements['tag-input'].value='读书，家庭';
await elements['save-tags'].onclick();
assert.deepEqual(JSON.parse(run('JSON.stringify(tagCalls)')),[{path:'/api/subscriptions/tags',body:{platform:'xiaohongshu',author_id:'stable',tags:['读书','家庭']}}]);
assert.equal(elements['tag-dialog'].open,false);
console.log('Author tag UI checks passed: edit payload and full subscription list filtering.');
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"archived",identity_verified:true,enabled:false,subscribed:false,item_count:2}])');
assert.match(flattenText(elements['subscription-list']),/已取消订阅.*重新订阅.*归档已有资料/);
assert.doesNotMatch(flattenText(elements['subscription-list']),/测试前10篇并归档|暂停|启用|取消订阅（保留归档）/);
run('globalThis.lifecycleCalls=[];api=async(path,body)=>{lifecycleCalls.push({path,body});return {message:"已重新订阅；旧资料保留。"}};refresh=async()=>{}');
await walk(elements['subscription-list']).find(x=>x.textContent==='重新订阅').handlers.click();
assert.deepEqual(JSON.parse(run('JSON.stringify(lifecycleCalls)')),[{path:'/api/subscriptions/resubscribe',body:{platform:'xiaohongshu',author_id:'archived'}}]);
run('renderSubscriptions([{platform:"xiaohongshu",author_id:"author",identity_verified:true,enabled:true}])');
assert.doesNotMatch(visibleText(elements['subscription-list']),/测试前10篇并归档/);
assert.match(flattenText(elements['subscription-list']),/测试前10篇并归档/);
assert.match(flattenText(elements['subscription-list']),/取消订阅（保留归档）/);
await walk(elements['subscription-list']).find(x=>x.textContent==='取消订阅（保留归档）').handlers.click();
assert.deepEqual(JSON.parse(run('JSON.stringify(lifecycleCalls.at(-1))')),{path:'/api/subscriptions/cancel',body:{platform:'xiaohongshu',author_id:'author'}});
assert.match(visibleText(elements['subscription-list']),/配置后台来源.*导出本机已有资料/,'Core author actions are visible');
assert.doesNotMatch(visibleText(elements['subscription-list']),/旧浏览器历史诊断|两页采集并归档（实验）|保存全部已收录内容/,'Diagnostic actions remain collapsed');
assert.match(flattenText(elements['subscription-list']),/其他采集与验证操作.*测试前10篇并归档.*后台近期诊断.*两页采集并归档/,'Existing diagnostic experiments remain available');
const batchAdvanced=html.match(/<details class="archive-experiments">[\s\S]*?<\/details>/)?.[0];
assert.ok(batchAdvanced);
assert.match(batchAdvanced,/data-job="full"/);
assert.match(html.replace(batchAdvanced,''),/data-job="all_archive"/);
assert.match(html.replace(batchAdvanced,''),/id="preview-selected-archive"/);
assert.match(batchAdvanced,/data-job="content"/);
assert.match(batchAdvanced,/data-job="demo_archive"/);
assert.match(html.replace(batchAdvanced,''),/data-job="source_refresh"[^>]*>刷新已连接来源/);
assert.match(html.replace(batchAdvanced,''),/id="refresh-selected-sources"[^>]*>刷新所选来源/);
assert.match(html.replace(batchAdvanced,''),/id="export-selected-local"[^>]*>导出所选已保存/);
assert.match(html.replace(batchAdvanced,''),/data-job="archive"[^>]*>导出全部已保存/);
run('api=async(path,body)=>{pageCalls.push({path,body});return body?{job_id:32}:{total:0,items:[]};}');
await run('startJob("all_archive")');
assert.deepEqual(JSON.parse(run('JSON.stringify(pageCalls.at(-1).body)')),{mode:'all_archive'});
const selectable=[
  {platform:'xiaohongshu',author_id:'first',display_name:'First',tags:['A'],identity_verified:true,subscribed:true,enabled:true},
  {platform:'xiaohongshu',author_id:'second',display_name:'Second',tags:['B'],identity_verified:true,subscribed:true,enabled:false},
  {platform:'wechat',author_id:'cancelled',display_name:'Cancelled',identity_verified:true,subscribed:false,enabled:true},
  {platform:'xiaohongshu',author_id:'pending',display_name:'Pending',identity_verified:true,subscribed:true,subscription_confirmation_required:true,enabled:true}
];
run(`ui.workspace={subscriptions:${JSON.stringify(selectable)}};renderSubscriptionTags(ui.workspace.subscriptions);renderSubscriptions(ui.workspace.subscriptions)`);
let checks=walk(elements['subscription-list']).filter(x=>x.tagName==='input'&&x.type==='checkbox');
assert.equal(checks.length,2,'Cancelled and pending authors cannot enter a selected batch');
checks[0].checked=true;checks[0].handlers.change();
elements['subscription-tag'].value='B';elements['subscription-tag'].onchange();
checks=walk(elements['subscription-list']).filter(x=>x.tagName==='input'&&x.type==='checkbox');
assert.equal(checks.length,1);
checks[0].checked=true;checks[0].handlers.change();
assert.match(elements['selected-archive-count'].textContent,/已选 2 位/);
elements['subscription-tag'].value='A';elements['subscription-tag'].onchange();
assert.equal(run('ui.selectedAuthors.size'),2,'Filtering never replaces explicit selection');
run('globalThis.selectedCalls=[];api=async(path,body)=>{selectedCalls.push({path,body});return path.endsWith("preview")?{members:body.selected_authors.map(x=>({...x,display_name:x.author_id,paused:x.author_id==="second",platform_available:true}))}:{batch_id:42,message:"所选范围已固定"};};refresh=async()=>{}');
await run('previewSelectedArchive()');
assert.equal(elements['selected-archive-dialog'].open,true);
assert.match(flattenText(elements['selected-archive-members']),/first.*second.*已暂停/);
assert.deepEqual(JSON.parse(run('JSON.stringify(selectedCalls[0].body.selected_authors)')),
  [{platform:'xiaohongshu',author_id:'first'},{platform:'xiaohongshu',author_id:'second'}]);
await run('startSelectedArchive()');
assert.deepEqual(JSON.parse(run('JSON.stringify(selectedCalls[1].body)')),
  {mode:'selected_archive',selected_authors:[{platform:'xiaohongshu',author_id:'first'},{platform:'xiaohongshu',author_id:'second'}]});
assert.equal(elements['selected-archive-dialog'].open,false);
await run('exportSelectedLocal()');
assert.deepEqual(JSON.parse(run('JSON.stringify(selectedCalls.at(-1).body)')),
  {mode:'archive',selected_authors:[{platform:'xiaohongshu',author_id:'first'},{platform:'xiaohongshu',author_id:'second'}]});
await run('refreshSelectedSources()');
assert.deepEqual(JSON.parse(run('JSON.stringify(selectedCalls.at(-1).body)')),
  {mode:'source_refresh',selected_authors:[{platform:'xiaohongshu',author_id:'first'},{platform:'xiaohongshu',author_id:'second'}]},'Selected source refresh retains stable author IDs across filters');
elements['subscription-tag'].value='';
console.log('Selected archive UI checks passed: stable IDs, cross-filter selection, paused author, preview and fixed submit payload.');
run('renderSubscriptions([{platform:"wechat",author_id:"pending-wx",identity_verified:false,subscription_confirmation_required:true,source_connected:false}])');
await walk(elements['subscription-list']).find(x=>x.textContent==='配置后台来源').handlers.click();
assert.equal(elements['source-dialog'].open,true);
assert.equal(elements['source-url'].value,'','Source URL is never prefilled from workspace status');
run('globalThis.sourceCalls=[];api=async(path,body)=>{sourceCalls.push({path,body});return {message:"synthetic source response"}};refresh=async()=>{}');
const privateSource='http://127.0.0.1:18765/feed/pending-wx?token=SYNTHETIC_ONLY';
elements['source-url'].value=privateSource;
await elements['save-source'].onclick();
assert.deepEqual(JSON.parse(run('JSON.stringify(sourceCalls)')),[{path:'/api/sources',body:{platform:'wechat',author_id:'pending-wx',url:privateSource}}]);
assert.equal(elements['source-url'].value,'','Saved URL is cleared from the editor');
assert.equal(elements['source-dialog'].open,false);
assert.doesNotMatch(flattenText(elements['subscription-list'])+' '+elements.feedback.textContent,/SYNTHETIC_ONLY|127\.0\.0\.1:18765/,'Source URL does not enter visible status');
run('renderSubscriptions([{platform:"wechat",author_id:"pending-wx",identity_verified:false,subscription_confirmation_required:true,source_connected:true,source_kind:"feed_http"}])');
assert.match(flattenText(elements['subscription-list']),/身份待验证.*后台 Feed 已配置，待核验/);
assert.equal(walk(elements['subscription-list']).some(x=>x.tagName==='button'&&x.textContent==='确认订阅'),false,'Unverified author cannot be confirmed before source verification');
assert.doesNotMatch(flattenText(elements['subscription-list']),/刷新此作者来源/);
run('sourceCalls=[]');
await run('startJob("source_refresh")');
assert.deepEqual(JSON.parse(run('JSON.stringify(sourceCalls.at(-1).body)')),{mode:'source_refresh'});
run('renderJobs([{id:51,mode:"source_refresh",state:"partial",target_count:4,item_count:3,body_saved_count:3,registered_media:{image:5,video:1},media_observed_complete_count:2,media_partial_item_count:1,media_unknown_item_count:1,message:"failed http://127.0.0.1:18765/feed?token=SYNTHETIC_ONLY",next_step:"token=SYNTHETIC_ONLY"}])');
assert.match(flattenText(elements['job-list']),/已发现 4 个待处理作品，完成 3 个.*正文 3.*已登记图片 5 \/ 视频 1.*观察范围媒体已完成 2.*待补 1.*范围未知 1/);
assert.match(flattenText(elements['job-list']),/后台来源刷新.*本次刷新窗口不等于原站全历史/);
assert.doesNotMatch(flattenText(elements['job-list']),/SYNTHETIC_ONLY|127\.0\.0\.1:18765/,'Source jobs do not render source URLs or credentials');
console.log('Source UI checks passed: pending configuration, private URL handling, selected/global refresh scope and honest job status.');
run('api=async(path,body)=>{pageCalls.push({path,body});return body?{job_id:32}:{total:0,items:[]};}');
run('renderArchiveBatches([{id:7,state:"partial",total:2,complete:0,unfinished:1,blocked:1,members:[{platform:"wechat",author_id:"wx",job_id:null,reason:"wechat_blocked",pages:0,item_count:0,target_count:0},{platform:"xiaohongshu",author_id:"author",job_id:8,state:"partial",reason:"item_unavailable",reused_existing_job:true,pages:3,item_count:2,target_count:3,can_resume:true,scan_url:"/api/jobs/8/scan",scan_manifest_url:"/archive/xiaohongshu/author/scan-run-1.json"}]}])');
assert.match(flattenText(elements['archive-batch-list']),/固定作者 2 位.*公众号未接入 1.*任务 #8 · 沿用已有检查点（未自动重试）.*正文媒体 2 \/ 3 篇.*继续批次未完成作者/);
assert.match(flattenText(elements['archive-batch-list']),/查看当前扫描范围.*查看本轮扫描清单/);
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

// A poll between pointerdown and click must not replace the resume button.
run(`refresh=realRefresh;ui.refreshPromise=null;ui.runsSignature=null;ui.itemsSignature='';
  globalThis.resumeCalls=[];globalThis.mockState='partial';globalThis.mockFailures=1;
  api=async(path,body)=>{
    if(path==='/api/workspace')return {subscriptions:[],archive_batches:[],platforms:[],
      stats:{subscriptions:1,items:6,details:5,assets:5,metric_snapshots:0},
      runs:[{id:72,platform:'xiaohongshu',author_id:'synthetic',display_name:'模拟博主',
        mode:'full',state:mockState,reason:'author_archive_partial',can_resume:mockState==='partial',
        pages:3,listed_count:6,target_count:6,item_count:5,failed_count:mockFailures,
        coverage:'complete_for_accessible_scope'}]};
    if(path.startsWith('/api/items?'))return {total:0,items:[]};
    if(path==='/api/jobs/72/resume'){resumeCalls.push({path,body});mockState='queued';return {state:'queued'};}
    throw new Error('Unexpected mock request: '+path);
  };`);
await run('refresh(false,true)');
let resumeButton=walk(elements['job-list']).find(x=>x.tagName==='button'&&x.textContent==='从检查点继续');
assert.ok(resumeButton,'A partial native history task exposes the resume action');
resumeButton.handlers.click();
await new Promise(setImmediate);
assert.equal(run('resumeCalls.length'),1,'An ordinary click calls the original job resume API once');
assert.match(elements.feedback.textContent,/已请求继续任务/);

run("mockState='partial';mockFailures=1");
await run('refresh(false,true)');
resumeButton=walk(elements['job-list']).find(x=>x.tagName==='button'&&x.textContent==='从检查点继续');
elements['job-list'].handlers.pointerdown({target:resumeButton});
run('mockFailures=2');
await run('refresh(false,true)');
assert.ok(walk(elements['job-list']).includes(resumeButton),'The held button survives an intervening poll');
document.handlers.pointerup({target:null}); // release can occur outside the button
resumeButton.handlers.click(); // browser click is dispatched before the release timer
assert.equal(run('resumeCalls.length'),2,'The held button still resumes the job');
flushTimers();
await new Promise(setImmediate);
assert.equal(run('ui.jobPointerActive'),false);

run("mockState='partial';mockFailures=3");
await run('refresh(false,true)');
resumeButton=walk(elements['job-list']).find(x=>x.tagName==='button'&&x.textContent==='从检查点继续');
elements['job-list'].handlers.pointerdown({target:resumeButton});
document.handlers.pointercancel();
flushTimers();
assert.equal(run('ui.jobPointerActive'),false,'Pointer cancellation releases the refresh guard');
elements['job-list'].handlers.pointerdown({target:resumeButton});
globalHandlers.blur();
flushTimers();
assert.equal(run('ui.jobPointerActive'),false,'Window blur releases the refresh guard');
console.log('Resume UI checks passed: ordinary click, poll during press, outside release, cancellation and blur.');

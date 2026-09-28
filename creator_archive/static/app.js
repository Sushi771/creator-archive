"use strict";
const $ = (id) => document.getElementById(id);
const platformName = {wechat:"公众号",xiaohongshu:"小红书"};
const stateName = {queued:"排队中",running:"执行中",succeeded:"任务完成",complete:"已完成",partial:"部分完成",needs_login:"需要登录",rate_limited:"等待冷却",failed:"未完成",blocked:"接入受限",interrupted:"已中断",pending:"待验证",unavailable:"暂不可用"};
const coverageName = {complete_for_accessible_scope:"已观察到可获取列表末页",scanning:"历史扫描进行中",blocked:"历史扫描受阻",complete:"历史列表已至末页",complete_observed:"历史列表已至末页",partial:"历史列表不完整",unknown:"历史覆盖未知",not_started:"尚未扫描"};
const modeName = {full:"全部历史",latest:"检查更新",archive:"本地归档",all_archive:"全订阅逐作者归档（实验）",page_archive:"两页采集并归档（实验）",author_archive:"单作者全历史归档（实验）",demo_archive:"前10篇测试并归档",content:"保存正文与媒体",metrics:"刷新互动指标",import:"导入验证资料"};
const kindName = {profile:"博主主页",item:"作品链接",short_link:"分享短链",collection:"合集链接"};
const reasonName = {timeout:"请求超时",page_budget_reached:"达到本次页数上限",repeated_cursor:"分页游标重复",empty_nonterminal_page:"返回空页但仍有下一页",missing_terminal_evidence:"缺少可信末页依据",identity_mismatch:"作者身份不一致",adapter_version_changed:"组件版本变化",unexpected_adapter_error:"组件异常",transport_unavailable:"采集通道尚未接通",needs_login:"当前会话需要登录",rate_limited:"平台要求冷却后重试",identity_unverified:"作者身份尚待核验"};
const ui = {filters:{sort:"published_at",order:"desc"},detailRequest:0,workspace:null,offset:0,limit:20,total:0,filter:"",itemRequest:0,refreshPromise:null,busy:false,mutationEpoch:0,pendingRefresh:false,subscriptionsSignature:null,runsSignature:null,platformsSignature:null,itemsSignature:"",confirmAuthor:null,tagAuthor:null,selectedAuthors:new Set(),previewAuthors:null};
const num = (v) => Number(v || 0).toLocaleString("zh-CN");
const nameOf = (p) => platformName[p] || p || "未知平台";
function node(tag,text,className) {const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=String(text);if(className)n.className=className;return n;}
function badge(text,tone="") {return node("span",text,`badge ${tone}`);}
function empty(target,title,description="") {const n=node("div",null,"empty");n.append(node("strong",title));if(description)n.append(node("span",description));target.replaceChildren(n);}
function notify(text,error=false) {$("feedback").textContent=text;$("feedback").className=`feedback${error?" error":""}`;$("feedback").hidden=false;if($("item-dialog").open){$("detail-feedback").textContent=text;$("detail-feedback").className=`feedback${error?" error":""}`;$("detail-feedback").hidden=false;}}
function formatTime(v) {if(!v)return "";const date=new Date(typeof v==="number"?v*1000:v);return Number.isNaN(date.getTime())?"":date.toLocaleString("zh-CN",{hour12:false});}
function safeLink(url,label) {if(typeof url!=="string")return null;try{const parsed=new URL(url,location.origin);if(!["http:","https:"].includes(parsed.protocol))return null;const a=node("a",label);a.href=parsed.href;a.target="_blank";a.rel="noopener noreferrer";return a;}catch{return null;}}
async function api(path,body) {let response;try{response=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-Creator-Archive":"local-validation"},body:JSON.stringify(body)});}catch{throw new Error("本机服务连接失败，原因尚未确定。已有资料与任务检查点保留；请双击 start.cmd 启动服务后刷新页面。");}let data;try{data=await response.json();}catch{throw new Error("本机服务未返回可读取的结果，请检查启动窗口后刷新。");}if(!response.ok){let message=typeof data.detail==="string"?data.detail:data.message;if(!message&&Array.isArray(data.detail))message=data.detail.map(x=>x.msg).join("；");throw new Error(message||`操作未完成（HTTP ${response.status}），已保存资料仍保留，请检查服务后重试。`);}return data;}
function actionButton(label,action,small=true) {const b=node("button",label,`secondary${small?" small":""}`);b.type="button";b.addEventListener("click",()=>perform(b,action));return b;}
async function perform(button, action) {
  if (ui.busy) {
    notify("上一操作处理中，请等待结果后再操作。已有资料与进度会保留。");
    return;
  }
  ui.busy = true;
  ui.mutationEpoch += 1;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "处理中…";
  try {
    await action();
  } catch (error) {
    notify(error.message, true);
  } finally {
    ui.busy = false;
    button.disabled = false;
    button.textContent = original;
    if (ui.pendingRefresh) {
      ui.pendingRefresh = false;
      refresh(true).catch(error => notify(error.message, true));
    }
  }
}
function authorBy(platform,id) {return ui.workspace?.subscriptions.find(x=>x.platform===platform&&x.author_id===id);}
function displayAuthor(platform,id) {return authorBy(platform,id)?.display_name||id||"全部作者";}
function renderSubscriptionTags(subscriptions) {
  const select=$("subscription-tag"), selected=select.value;
  const tags=[...new Set(subscriptions.flatMap(sub=>sub.tags||[]))].sort((a,b)=>a.localeCompare(b,"zh-CN"));
  select.replaceChildren(new Option("全部标签",""),...tags.map(tag=>new Option(tag,tag)));
  select.value=tags.includes(selected)?selected:"";
}
function authorKey(author) {return JSON.stringify([author.platform,author.author_id]);}
function selectedAuthorPayload() {return [...ui.selectedAuthors].map(key=>{const [platform,author_id]=JSON.parse(key);return {platform,author_id};}).sort((a,b)=>authorKey(a).localeCompare(authorKey(b)));}
function selectedArchiveCount() {$("selected-archive-count").textContent=`已选 ${ui.selectedAuthors.size} 位作者。筛选只改变显示，已选范围保留；取消或待确认作者不可选择。`;}
function renderSubscriptions(subscriptions) {
  const eligible=new Set(subscriptions.filter(sub=>sub.identity_verified&&sub.subscribed!==false&&!sub.subscription_confirmation_required).map(authorKey));
  for(const key of ui.selectedAuthors)if(!eligible.has(key))ui.selectedAuthors.delete(key);
  selectedArchiveCount();
  $("nav-count").textContent=num(subscriptions.length);
  const list=$("subscription-list");list.replaceChildren();
  if(!subscriptions.length){empty(list,"从第一位作者开始","粘贴链接添加订阅，或在「数据与接入」导入已有真实验证资料。");return;}
  const visible=subscriptions.filter(sub=>(!$("subscription-platform").value||sub.platform===$("subscription-platform").value)&&(!$("subscription-tag").value||(sub.tags||[]).includes($("subscription-tag").value)));
  $("subscription-filter-count").textContent=`显示 ${visible.length} / ${subscriptions.length} 位作者；筛选只影响列表显示，不改变批次范围；已选作者跨筛选保留。`;
  if(!visible.length){empty(list,"没有匹配的作者","请调整平台或标签筛选；订阅及归档仍保留。");return;}
  for(const sub of visible){
    const row=node("article",null,"subscription-row"),info=node("div",null,"author-info"),text=node("div");
    info.append(node("div",(sub.display_name||sub.author_id||"?").slice(0,1),"avatar"));
    text.append(node("h3",sub.display_name||sub.author_id),node("div",`${nameOf(sub.platform)} · ${sub.author_id}`,"meta"));
    const tags=node("div",null,"badges");
    tags.append(badge(sub.subscribed===false?"已取消订阅":sub.subscription_confirmation_required?"待确认订阅":sub.enabled?"订阅中":"已暂停",sub.subscribed===false?"neutral":sub.subscription_confirmation_required?"warning":sub.enabled?"":"neutral"),
                badge(sub.identity_verified?"身份已核验":"身份待验证",sub.identity_verified?"":"warning"),
                badge(`${num(sub.item_count)} 作品 · ${num(sub.detail_count)} 正文`,"neutral"));
    for(const tag of sub.tags||[])tags.append(badge(tag,"neutral"));
    text.append(tags);
    if(sub.subscribed===false)text.append(node("p","新批次不再包含这位作者；旧归档、手工资料、任务及固定批次检查点保留。","hint"));
    else if(sub.subscription_confirmation_required)text.append(node("p","作品作者已核验；确认前不进入全部订阅批次，也不会自动扫描历史。","hint"));
    else if(sub.message)text.append(node("p",sub.message,"hint"));
    info.append(text);
    const actions=node("div",null,"author-actions");
    if(eligible.has(authorKey(sub))){
      const label=node("label",null,"select-author"),check=node("input");check.type="checkbox";check.checked=ui.selectedAuthors.has(authorKey(sub));
      check.setAttribute("aria-label",`选择 ${nameOf(sub.platform)} ${sub.display_name||sub.author_id} 进行手动全量归档`);
      check.addEventListener("change",()=>{if(check.checked)ui.selectedAuthors.add(authorKey(sub));else ui.selectedAuthors.delete(authorKey(sub));selectedArchiveCount();});
      label.append(check,document.createTextNode("选入归档"));actions.append(label);
    }
    actions.append(actionButton("查看作品",async()=>{ui.filter=`${sub.platform}|${sub.author_id}`;$("filter-author").value=ui.filter;ui.offset=0;await loadItems();$("library").scrollIntoView();}));
    actions.append(actionButton("编辑标签",()=>openTagEditor(sub)));
    if(sub.subscribed===false){
      actions.append(actionButton("重新订阅",async()=>{
        const result=await api("/api/subscriptions/resubscribe",{platform:sub.platform,author_id:sub.author_id});
        notify(result.message);await refresh(true);
      }));
      actions.append(actionButton("归档已有资料",()=>startJob("archive",sub)));
    } else if(sub.subscription_confirmation_required){
      actions.append(actionButton("确认订阅",()=>openSubscriptionConfirmation(sub)));
    } else {
      const advanced=node("details",null,"archive-experiments"),advancedActions=node("div",null,"actions");
      advanced.append(node("summary","完整归档实验（超出本轮10篇测试）"));
      if(sub.identity_verified&&sub.platform==="xiaohongshu"){
        actions.append(actionButton("测试前10篇并归档",()=>startJob("demo_archive",sub)));
        text.append(node("p","本轮仅测试列表前10篇唯一作品及对应正文、媒体和互动数据，成功后停止；已有成功资源会复用，不代表新下载或历史完整。","hint"));
        advancedActions.append(actionButton("单作者全历史归档（实验）",()=>startJob("author_archive",sub)),actionButton("两页采集并归档（实验）",()=>startJob("page_archive",sub)));
      }
      advancedActions.append(actionButton("全部历史",()=>startJob("full",sub)),actionButton("检查更新",()=>startJob("latest",sub)),
        actionButton("保存全部已收录内容",()=>startJob("content",sub)),actionButton("刷新全部指标",()=>startJob("metrics",sub)),
        actionButton("归档已有资料",()=>startJob("archive",sub)));
      advanced.append(advancedActions);actions.append(advanced);
      actions.append(actionButton(sub.enabled?"暂停":"启用",async()=>{
        await api("/api/subscriptions/toggle",{platform:sub.platform,author_id:sub.author_id,enabled:!sub.enabled});
        notify(`${sub.display_name||sub.author_id}：已${sub.enabled?"暂停":"启用"}订阅。已有资料保留。`);await refresh(true);
      }));
    }
    if(sub.subscribed!==false)actions.append(actionButton("取消订阅（保留归档）",async()=>{
      const result=await api("/api/subscriptions/cancel",{platform:sub.platform,author_id:sub.author_id});
      notify(`${sub.display_name||sub.author_id}：${result.message}`);await refresh(true);
    }));
    if(!sub.identity_verified&&sub.platform==="xiaohongshu")actions.append(actionButton("核验作者",async()=>{
      const result=await api("/api/subscriptions/verify",{platform:sub.platform,author_id:sub.author_id});
      notify(result.message||"作者核验已返回，请查看身份状态。");await refresh(true);
    }));
    if(sub.archive_url){const link=safeLink(sub.archive_url,"打开归档");if(link)actions.append(link);}
    row.append(info,actions);list.append(row);
  }
}
function openTagEditor(sub) {
  ui.tagAuthor={platform:sub.platform,author_id:sub.author_id};
  $("tag-author").textContent=`${sub.display_name||sub.author_id} · ${nameOf(sub.platform)} · ${sub.author_id}`;
  $("tag-input").value=(sub.tags||[]).join("，");
  $("tag-error").hidden=true;
  $("tag-dialog").showModal();
}
async function saveTags() {
  const author=ui.tagAuthor;
  if(!author)return;
  const raw=$("tag-input").value.trim();
  const tags=raw?raw.split(/[,，]/).map(tag=>tag.trim()):[];
  let result;
  try {
    result=await api("/api/subscriptions/tags",{...author,tags});
  } catch(error) {
    $("tag-error").textContent=`${error.message} 原标签及归档仍保留，可修改后重试。`;
    $("tag-error").hidden=false;
    return;
  }
  $("tag-dialog").close();notify(result.message);
  try {await refresh(true);} catch {notify("标签已保存，但页面刷新失败；请刷新工作区查看。归档和任务进度保留。",true);}
}
function isPagePipeline(run) {return ["page_archive","author_archive","demo_archive"].includes(run.mode);}
function renderArchiveBatches(batches) {
  const list=$("archive-batch-list");list.replaceChildren();
  for(const batch of batches){
    const card=node("article",null,"job-card"),top=node("div",null,"job-top");
    top.append(node("h3",`${batch.mode==="selected_archive"?"所选作者":"全订阅"}逐作者归档（实验） · 批次 #${batch.id}`),badge(batch.state==="running"?"进行中":batch.state==="succeeded"?"已完成":"部分完成",batch.state==="partial"?"warning":""));
    card.append(top,node("p",`固定作者 ${num(batch.total)} 位 · 完成 ${num(batch.complete)} · 进行/待恢复 ${num(batch.pending)} · 部分 ${num(batch.partial)} · 失败/受限 ${num(batch.failed)} · 公众号未接入 ${num(batch.blocked)}。正文媒体成功 ${num(batch.items_complete)} / 已纳入 ${num(batch.items_target)} 篇，已尝试未完成 ${num(batch.items_failed)}，待处理 ${num(batch.items_pending)}。完成仅指对应作者已观察到的可获取范围；公众号及双平台G1仍未通过。`,"job-progress"));
    for(const member of batch.members){
      const label=displayAuthor(member.platform,member.author_id);
      card.append(node("p",`${nameOf(member.platform)} · ${label} · ${member.job_id?`任务 #${member.job_id}`:"无采集任务"}${member.reused_existing_job?" · 沿用已有检查点（未自动重试）":""} · ${member.job_id?(stateName[member.state]||member.state):"接入未完成"} · 列表 ${num(member.pages)} 页 · 正文媒体 ${num(member.item_count)} / ${num(member.target_count)} 篇 · ${member.list_finished?"可信末页已观察":"末页未核验"}${member.reason?` · ${reasonName[member.reason]||member.reason}`:""}`,"meta"));
      if(member.scan_url){const link=safeLink(member.scan_url,"查看当前扫描范围");if(link)card.append(link);}
      if(member.scan_manifest_url){const link=safeLink(member.scan_manifest_url,"查看本轮扫描清单（导出时状态）");if(link)card.append(link);}
      if(member.state!=="succeeded")card.append(node("p",`${member.message||"进度已保留"} ${member.next_step||"请查看对应作者任务。"}`,"hint"));
    }
    if(batch.members.some(m=>m.can_resume))card.append(actionButton("继续批次未完成作者",async()=>{const result=await api(`/api/archive-batches/${encodeURIComponent(batch.id)}/resume`,{});notify(`${result.message}${result.waiting?.length?` ${result.waiting.length} 位作者需查看各任务提示。`:""}`);await refresh(true);}));
    list.append(card);
  }
}
function pipelineProgress(run) {
  if(run.mode==="demo_archive")return `本轮固定上限 ${num(run.item_limit||10)} 篇 · 已纳入 ${num(run.target_count)} 篇 · 正文媒体成功 ${num(run.item_count)} / ${num(run.target_count)} 篇 · 已尝试未完成 ${num(run.failed_count)} · 待处理 ${num(run.pending_count)}；成功数包含已有资源复用，不代表本轮新下载；${run.list_finished?"本次列表已到可信末页，内容结果另看成功数":"本轮测试不代表历史完整"}`;
  const list=run.mode==="author_archive"
    ? `列表已保存 ${num(run.pages)} 页 · ${run.list_finished?"已核验可信末页":"尚未核验末页"}`
    : `列表 ${num(run.pages)} / ${num(run.page_limit||2)} 页`;
  return `${list} · 正文媒体 ${num(run.item_count)} / ${num(run.target_count)} 篇 · 已尝试未完成 ${num(run.failed_count)} · 待处理 ${num(run.pending_count)}；${coverageName[run.coverage]||"历史覆盖未知"}；成功资源和已保存进度保留`;
}
function renderJobs(runs) {const list=$("job-list");list.replaceChildren();if(!runs.length){empty(list,"还没有任务","添加作者后获取历史，或将已有作品按作者归档。任务进度会保存在本机。");return;}for(const run of [...runs].sort((a,b)=>(b.updated_at||0)-(a.updated_at||0))){const card=node("article",null,"job-card"),top=node("div",null,"job-top"),title=node("div");title.append(node("h3",`${run.display_name||displayAuthor(run.platform,run.author_id)} · ${modeName[run.mode]||run.mode||"采集任务"}`),node("div",`${nameOf(run.platform)} · 任务 #${run.id}${run.parent_job_id?` · 属于按页归档任务 #${run.parent_job_id}，请从该任务恢复`:""}`,"meta"));const tone=["failed","blocked","needs_login","partial","rate_limited","interrupted"].includes(run.state)?"warning":"";top.append(title,badge(run.reason==="validation_import"?"历史观察已导入":stateName[run.state]||run.state||"待处理",tone));const progress=isPagePipeline(run)?pipelineProgress(run):["content","metrics"].includes(run.mode)?`本次已成功 ${num(run.item_count)} / ${num(run.target_count)} 个作品 · 已尝试未完成 ${num(run.failed_count)} 个 · 待处理 ${num(run.pending_count)} 个；成功资源和有效指标已保留`:run.mode==="archive"?`本次已导出 ${num(run.item_count)} 个作品记录${run.library_item_count!==undefined?`（资料库 ${num(run.library_item_count)} 个）`:""}`:`本次已保存 ${num(run.pages)} 页 · ${num(run.item_count)} 个 ID${run.library_item_count!==undefined?`（资料库 ${num(run.library_item_count)} 个）`:""} · ${coverageName[run.coverage]||"历史覆盖未知"}`;card.append(top,node("p",progress,"job-progress"));if(run.message||run.reason||run.next_step){const issue=node("div",null,"job-issue");issue.append(node("p",run.message||reasonName[run.reason]||`原因：${run.reason||"尚未确定"}`));if(run.next_step)issue.append(node("p",`下一步：${run.next_step}`));card.append(issue);}const bottom=node("div",null,"job-bottom");bottom.append(node("span",formatTime(run.updated_at)?`更新于 ${formatTime(run.updated_at)}`:"进度已持久保存"));const actions=node("div",null,"actions");if(run.can_resume)actions.append(actionButton("从检查点继续",async()=>{await api(`/api/jobs/${encodeURIComponent(run.id)}/resume`,{});notify("已请求继续任务，成功页和已有资料会复用。请查看任务状态。");await refresh(true);}));if(!run.parent_job_id&&["content","metrics","page_archive","author_archive","demo_archive"].includes(run.mode))actions.append(actionButton(isPagePipeline(run)?"查看未完成":"查看未完成 / 补充链接",()=>openRecovery(run)));if(run.scan_url){const link=safeLink(run.scan_url,"查看当前扫描范围");if(link)actions.append(link);}const archiveUrl=run.archive_url||run.export?.archive_url;if(archiveUrl){const link=safeLink(archiveUrl,"打开归档");if(link)actions.append(link);}for(const author of run.export?.authors||[]){const label=displayAuthor(author.platform,author.author_id);for(const [url,title] of [[author.scan_manifest_url,`${label} · 本轮扫描清单（导出时状态）`],[author.manifest_url,`${label} · 全库清单`],[author.corpus_url,`${label} · 正文语料`]]){const link=safeLink(url,title);if(link)actions.append(link);}}bottom.append(actions);card.append(bottom);list.append(card);}}
function renderPlatforms(platforms) {const list=$("platform-list");list.replaceChildren();for(const p of platforms){const card=node("article",null,"platform-card"),heading=node("h3",nameOf(p.platform));heading.append(badge(p.status==="experimental"?"实验接入":p.available?"接入可用":"接入未完成",p.status==="experimental"||!p.available?"warning":""));card.append(heading,node("p",p.message||"尚无可验证的接入状态。"));if(p.platform==="xiaohongshu"){const actions=node("div",null,"actions");actions.append(actionButton("打开专用登录浏览器",async()=>{const result=await api("/api/platforms/xiaohongshu/login",{});notify(result.message||"已请求打开专用浏览器，请在浏览器中自行登录，再从检查点继续任务。");await refresh();}));card.append(actions);}list.append(card);}if(!platforms.length)empty(list,"暂无平台状态","请刷新本机服务。");}
function renderFilters(subscriptions) {const selected=ui.filter;$("filter-author").replaceChildren(new Option("全部作者",""));for(const sub of subscriptions)$("filter-author").append(new Option(`${sub.display_name||sub.author_id} · ${nameOf(sub.platform)}`,`${sub.platform}|${sub.author_id}`));$("filter-author").value=selected;if($("filter-author").value!==selected){ui.filter="";ui.offset=0;}}
function dataSignature(value) {
  // Timestamp-only changes must not replace focused or hovered action buttons.
  return JSON.stringify(value, (key, data) => key === "updated_at" ? undefined : data);
}
async function refresh(forceItems = false, background = false) {
  if (ui.refreshPromise) {
    if (background) return ui.refreshPromise;
    // A user action needs a snapshot captured after the mutation, not an older poll.
    try { await ui.refreshPromise; } catch {}
    return refresh(forceItems, background);
  }
  if (background && ui.busy) {
    ui.pendingRefresh = true;
    return;
  }
  const epoch = ui.mutationEpoch;
  const request = (async () => {
    try {
      const w = await api("/api/workspace");
      if (epoch !== ui.mutationEpoch || (background && ui.busy)) {
        ui.pendingRefresh = true;
        return;
      }
      w.subscriptions = w.subscriptions || [];
      w.runs = w.runs || [];
      w.archive_batches = w.archive_batches || [];
      ui.workspace = w;
      $("stat-subscriptions").textContent = num(w.stats?.subscriptions ?? w.subscriptions.length);
      $("stat-items").textContent = num(w.stats?.items);
      $("stat-details").textContent = num(w.stats?.details);
      $("stat-attention").textContent = num(w.runs.filter(r => ["failed", "blocked", "needs_login", "rate_limited", "partial", "interrupted"].includes(r.state)).length);
      const subscriptionsSignature = dataSignature(w.subscriptions);
      const runsSignature = dataSignature([w.runs,w.archive_batches]);
      const platformsSignature = dataSignature(w.platforms || []);
      if (subscriptionsSignature !== ui.subscriptionsSignature) {
        renderSubscriptionTags(w.subscriptions);
        renderSubscriptions(w.subscriptions);
        renderFilters(w.subscriptions);
        ui.subscriptionsSignature = subscriptionsSignature;
      }
      if (runsSignature !== ui.runsSignature) {
        renderArchiveBatches(w.archive_batches);
        renderJobs(w.runs);
        ui.runsSignature = runsSignature;
      }
      if (platformsSignature !== ui.platformsSignature) {
        renderPlatforms(w.platforms || []);
        ui.platformsSignature = platformsSignature;
      }
      $("data-dir").textContent = w.data_dir || "未提供目录";
      $("archive-dir").textContent = w.archive_dir || "首次归档后生成";
      $("connection").textContent = "本机服务已连接 · 数据已持久保存";
      $("connection-dot").classList.remove("offline");
      $("updated").textContent = `更新 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
      const itemSignature = JSON.stringify([w.stats?.items, w.stats?.details, w.stats?.assets, w.stats?.metric_snapshots, w.runs.filter(run => ["content", "metrics"].includes(run.mode)), w.subscriptions.map(sub => [sub.platform, sub.author_id])]);
      if (forceItems || itemSignature !== ui.itemsSignature) {
        const loaded = await loadItems(() => epoch === ui.mutationEpoch && (!background || !ui.busy));
        if (loaded !== false) ui.itemsSignature = itemSignature;
      }
      // A fresh foreground snapshot also fulfils a poll deferred during this action.
      ui.pendingRefresh = epoch !== ui.mutationEpoch || (background && ui.busy);
    } catch (error) {
      $("connection").textContent = "连接已中断 · 请检查启动窗口，然后刷新";
      $("connection-dot").classList.add("offline");
      throw error;
    }
  })();
  ui.refreshPromise = request;
  try { await request; }
  finally { if (ui.refreshPromise === request) ui.refreshPromise = null; }
}
async function startJob(mode,sub,sourceUrl="") {const body={mode};if(sub){body.platform=sub.platform;body.author_id=sub.author_id;if(sub.item_id)body.item_id=sub.item_id;}if(sub?.item_id&&["content","metrics"].includes(mode)&&sourceUrl.trim())body.source_url=sourceUrl.trim();const result=await api("/api/jobs",body);notify(result.message||`已创建${sub?`「${sub.display_name||sub.author_id}」`:mode==="demo_archive"?"已核验并确认的小红书作者（含暂停项）":"全部订阅"}的${modeName[mode]}任务。请查看实际进度与接入提示。`);await refresh(true);if($("item-dialog").open)$("item-dialog").close();$("jobs").scrollIntoView();}
async function previewSelectedArchive() {
  const selected_authors=selectedAuthorPayload();
  if(!selected_authors.length)throw new Error("请先在作者卡片勾选至少一位已核验、已确认且仍订阅的作者。未创建任务。");
  const preview=await api("/api/archive-batches/preview",{selected_authors});
  ui.previewAuthors=selected_authors;
  const list=$("selected-archive-members");list.replaceChildren();
  for(const author of preview.members)list.append(node("p",`${nameOf(author.platform)} · ${author.display_name} · ${author.author_id}${author.paused?" · 已暂停（本次手动纳入）":""}${author.platform_available?"":" · 当前未接入采集"}`,"meta"));
  $("selected-archive-error").hidden=true;$("selected-archive-error").textContent="";
  $("selected-archive-dialog").showModal();
}
async function startSelectedArchive() {
  if(!ui.previewAuthors)throw new Error("请先预览所选作者范围。未创建任务。");
  try {
    const result=await api("/api/jobs",{mode:"selected_archive",selected_authors:ui.previewAuthors});
    $("selected-archive-dialog").close();
    notify(result.message);await refresh(true);$("jobs").scrollIntoView();
  } catch(error) {
    $("selected-archive-error").textContent=`${error.message}；预览后若订阅状态变化，请返回刷新并重新选择。`;
    $("selected-archive-error").hidden=false;
  }
}
function openSubscriptionConfirmation(sub) {
  if(!sub.subscription_confirmation_required)return;
  ui.confirmAuthor={platform:sub.platform,author_id:sub.author_id};
  $("confirm-subscription-author").textContent=`${sub.display_name||sub.author_id} · ${sub.author_id}`;
  $("confirm-subscription-error").textContent="";
  $("confirm-subscription-dialog").showModal();
}
async function confirmSubscription() {
  const target=ui.confirmAuthor;
  if(!target)return;
  let result;
  try {
    result=await api("/api/subscriptions/confirm",target);
  } catch(error) {
    $("confirm-subscription-error").textContent=`订阅确认未完成；已核验作品与旧任务保留。${error.message}`;
    throw error;
  }
  $("confirm-subscription-dialog").close();
  notify(result.message);
  try { await refresh(true); }
  catch(error) { notify(`订阅已确认，但页面刷新失败；请刷新工作区核对状态。${error.message}`,true); }
  $("subscriptions").scrollIntoView();
}
async function resolveAndSaveItem() {
  const input=$("resolve-item-link"),status=$("resolve-item-result");
  if(!input.reportValidity())return;
  const text=input.value.trim();
  let item;
  try {item=await api("/api/items/resolve",{text});}
  catch(error){status.textContent=`作品核验未完成，旧资料保留。${error.message}`;throw error;}
  status.textContent="目标作品与作者已核验收录；正在创建仅含这篇作品的保存任务…";
  const sourceUrl=item.resolved_from_short_link?undefined:text.match(/https?:\/\/[^\s<>"'，。；）]+/g)?.[0];
  try {
    const job=await api("/api/jobs",{mode:"content",platform:item.platform,author_id:item.author_id,item_id:item.item_id,source_url:sourceUrl});
    status.textContent=`目标作品已核验收录；单篇保存任务 #${job.job_id} 已创建。请到“历史与任务”查看正文与媒体结果。${item.subscription_confirmation_required?"如需订阅这位作者，请在随后弹窗或作者卡片明确确认；确认本身不会扫描历史。":""}`;
    notify(status.textContent);
  } catch(error) {
    const message=`作品已核验收录，但保存任务未创建：${error.message}请在“作品资料库”找到这篇作品，待当前任务结束后点击“保存正文与缺失媒体”。`;
    status.textContent=message;
    throw new Error(message);
  }
  input.value="";
  await refresh(true);
  if(item.subscription_confirmation_required){
    const sub=ui.workspace?.subscriptions.find(s=>s.platform===item.platform&&s.author_id===item.author_id);
    if(sub?.subscription_confirmation_required)openSubscriptionConfirmation(sub);
  }
  $("jobs").scrollIntoView();
}
const metricNames = {likes:"点赞",collects:"收藏",comments:"评论"};
const contentTypeNames = {image:"图文",video:"视频",unknown:"类型未知"};
const hasMetricValue = metric => metric?.value !== null && metric?.value !== undefined && metric.quality !== "unknown";
function metricValue(metric) {
  if (!hasMetricValue(metric)) return "未知";
  const prefix = metric.quality === "approximate" ? "约 " : metric.quality === "lower_bound" ? "≥ " : "";
  return prefix + num(metric.value);
}
function metricEvidence(metric) {
  const parts = [];
  if (metric?.raw === "") parts.push("原始字段：空字符串（没有数字，不能当作 0）");
  else if (metric?.raw !== null && metric?.raw !== undefined) parts.push(`原始显示：${metric.raw}`);
  parts.push(`来源：${metric?.source || "未采集"}`);
  parts.push(`有效值时间：${hasMetricValue(metric) ? formatTime(metric.collected_at) || "未知" : "未知"}`);
  if (["missing", "failed"].includes(metric?.last_attempt_status)) {
    parts.push(`最近尝试 ${formatTime(metric.last_attempt_at) || "时间未知"}：${metric.last_attempt_status === "failed" ? "更新失败" : "未取得有效数字，原因未确认"}；${hasMetricValue(metric) ? "已有有效值及时间保留" : "仍无有效值"}`);
  }
  return parts.join(" · ");
}
function appendMetrics(container, metrics, detailed = false) {
  const list = node("div", null, detailed ? "metric-grid" : "metric-strip");
  for (const [key, label] of Object.entries(metricNames)) {
    const metric = metrics?.[key];
    const entry = node("div", null, "metric-value");
    entry.append(node("span", label), node("strong", metricValue(metric)));
    entry.title = metricEvidence(metric);
    if (detailed) entry.append(node("small", metricEvidence(metric)));
    else {
      const stamp = hasMetricValue(metric) ? formatTime(metric.collected_at) : "";
      entry.append(node("small", stamp ? `采于 ${stamp}` : "尚无有效采集时间"));
      if (["missing", "failed"].includes(metric?.last_attempt_status)) entry.append(node("small", "最近更新未取得有效值", "metric-stale"));
    }
    list.append(entry);
  }
  container.append(list);
}
function itemParams() {
  const params = new URLSearchParams({offset:String(ui.offset),limit:String(ui.limit),...ui.filters});
  if (ui.filter) {
    const [platform, author_id] = ui.filter.split("|");
    params.set("platform",platform);params.set("author_id",author_id);
  }
  return params;
}
async function applyFilters() {
  if (!$("library-filters").reportValidity()) return;
  const from = $("filter-date-from").value, to = $("filter-date-to").value;
  if (from && to && from > to) {
    notify("起始日期不能晚于结束日期，请调整后重新应用筛选。", true);
    return;
  }
  ui.filters = {};
  for (const [id,key] of [["filter-type","content_type"],["filter-date-from","date_from"],["filter-date-to","date_to"],["filter-sort","sort"],["filter-order","order"],["filter-min-likes","min_likes"],["filter-min-collects","min_collects"],["filter-min-comments","min_comments"],["filter-missing","missing_metric"]]) {
    if ($(id).value !== "") ui.filters[key] = $(id).value;
  }
  if ($("filter-assets").checked) ui.filters.has_assets = "true";
  ui.filter = $("filter-author").value;
  ui.offset = 0;
  await loadItems();
}
async function loadItems(canApply = () => true) {
  const request=++ui.itemRequest;
  const params=itemParams();
  const data=await api(`/api/items?${params}`);
  if(request!==ui.itemRequest||!canApply())return false;
  ui.total=data.total||0;
  // A background update may reduce the matching set; return to a valid page.
  if(ui.offset && ui.offset>=ui.total) {ui.offset=Math.max(0,Math.ceil(ui.total/ui.limit)-1)*ui.limit;return loadItems(canApply);}
  $("item-count").textContent=`全库匹配 ${num(ui.total)} 个作品`;
  const sortLabel=metricNames[ui.filters.sort]||"发布日期";
  $("filter-summary").textContent=`全库匹配 ${num(ui.total)} 条 · ${sortLabel}${ui.filters.order==="asc"?"升序":"降序"} · 未知值在最后。筛选仅查询本地，不会自动采集平台。`;
  $("page-info").textContent=`第 ${Math.floor(ui.offset/ui.limit)+1} / ${Math.max(1,Math.ceil(ui.total/ui.limit))} 页`;
  $("prev-page").disabled=ui.offset===0;
  $("next-page").disabled=ui.offset+ui.limit>=ui.total;
  const list=$("item-list");list.replaceChildren();
  if(!data.items?.length){empty(list,"没有符合条件的作品","可重置筛选查看已有资料；未知指标不会满足最低数量条件，可使用「缺失指标」查找后单独刷新。");return;}
  for(const [index,item] of data.items.entries()) {
    const row=node("article",null,"item-row"),main=node("div",null,"item-main"),title=node("button",item.title||item.item_id,"item-title");
    title.type="button";
    title.addEventListener("click",()=>openItem(item).catch(e=>notify(e.message,true)));
    main.append(title,node("div",`${displayAuthor(item.platform,item.author_id)} · ${nameOf(item.platform)} · ${contentTypeNames[item.content_type]||"类型未知"}${item.published_at?` · ${formatTime(item.published_at)||item.published_at}`:" · 发布日期未知"}`,"meta"));
    appendMetrics(main,item.metrics);
    row.append(node("span",num(ui.offset+index+1),"item-number"),main,badge(item.detail_state==="complete"?"正文已保存":item.asset_count?`${num(item.asset_count)} 个附件 · 缺正文`:"仅列表记录",item.detail_state==="complete"?"":"warning"),actionButton("查看",()=>openItem(item)));
    list.append(row);
  }
}

function appendLocalMedia(container, asset) {
  if (asset.state !== "complete" || (!asset.url && !asset.archive_url)) return;
  let url;
  try {
    url = new URL(asset.url || asset.archive_url, location.origin);
    if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) return;
  } catch { return; }
  if (!["image", "video"].includes(asset.kind)) return;
  const figure = node("figure", null, "local-media");
  const media = node(asset.kind === "video" ? "video" : "img");
  const caption = node("figcaption", asset.kind === "video" ? "本机已保存视频 · 点击播放" : "本机已保存图片");
  if (asset.kind === "video") {
    media.controls = true;
    media.preload = "metadata";
    media.playsInline = true;
    media.setAttribute("aria-label", `本地视频 ${asset.asset_id || ""}`);
  } else {
    media.alt = `本地归档图片 ${asset.asset_id || ""}`;
    media.loading = "lazy";
  }
  media.addEventListener("error", () => {
    caption.textContent = "此附件无法预览，可能是文件不可用或浏览器不支持该格式。请使用上方「打开附件」检查；已有记录保留。";
    caption.classList.add("media-error");
  }, {once:true});
  media.src = url.href;
  figure.append(media, caption);
  container.append(figure);
}

async function startDetailJob(mode,item) {
  const source=$("detail-source-url");
  if (!source.reportValidity()) return;
  await startJob(mode,item,source.value.trim());
}
async function openItem(item) {
  const request=++ui.detailRequest;
  const result=await api(`/api/items/${encodeURIComponent(item.platform)}/${encodeURIComponent(item.item_id)}`);
  if(request!==ui.detailRequest)return;
  const data=result.item||result;
  const target={...item,...data};
  $("detail-feedback").hidden=true;
  $("detail-source-url").value="";
  $("detail-source").hidden=target.platform!=="xiaohongshu";
  $("detail-title").textContent=data.title||data.item_id||item.item_id;
  const content=$("detail-content");
  content.replaceChildren(node("p",`${displayAuthor(target.platform,target.author_id)} · ${nameOf(target.platform)} · ${contentTypeNames[data.content_type]||"类型未知"}`,"meta"));
  const tags=node("div",null,"badges");
  tags.append(badge(data.detail_state==="complete"?"正文已保存":"正文尚未保存",data.detail_state==="complete"?"":"warning"),badge(`本地附件 ${num(data.assets?.filter(asset=>asset.state==="complete").length)} 个`,"neutral"),badge(data.media_state==="complete_for_observed_detail"?"本次详情中已知媒体已保存":"媒体完整性未确认",data.media_state==="complete_for_observed_detail"?"":"warning"));
  content.append(tags);
  const actions=node("div",null,"detail-actions");
  actions.append(actionButton("保存正文与缺失媒体",()=>startDetailJob("content",target)),actionButton("仅刷新指标",()=>startDetailJob("metrics",target)),actionButton("刷新本地详情",()=>openItem(target)));
  content.append(actions,node("p","保存和刷新由持久任务执行。成功媒体复用；仅刷新指标不会重新下载媒体。任务失败时保留已有进度，可到「历史与任务」继续。","hint"));
  content.append(node("h3","互动指标"));
  appendMetrics(content,data.metrics,true);
  content.append(node("p","0 表示真实零值，未知表示尚无有效数据；约为近似值，≥ 为下界。各字段以自己的有效采集时间为准。原20篇固定样本中的35项未知，经同页诊断均为原始字段空串、详情互动条无数字；这只解释该次观察，不能推断其他作品、平台原因或永久可用性。查看历史不会触发采集。","hint"));
  if(data.metric_snapshots?.length) {
    const history=node("details",null,"metric-history");
    history.append(node("summary",`查看历史观察（${num(data.snapshot_total??data.metric_snapshots.length)} 次）`));
    for(const snapshot of data.metric_snapshots) {
      const entry=node("article",null,"snapshot-entry");
      entry.append(node("p",`${formatTime(snapshot.collected_at)||"时间未知"} · ${snapshot.source||"来源未知"} · ${snapshot.status==="ok"?"已观察":snapshot.status==="failed"?"观察失败":snapshot.status==="partial"?"部分指标缺失":snapshot.status||"状态未知"}`,"meta"));
      appendMetrics(entry,Object.fromEntries(Object.entries(snapshot.metrics||{}).map(([key,metric])=>[key,{...metric,source:metric.source||snapshot.source,collected_at:hasMetricValue(metric)?metric.collected_at??snapshot.collected_at:null}])));
      if(snapshot.reason) entry.append(node("p",snapshot.reason,"hint"));
      history.append(entry);
    }
    content.append(history);
  } else content.append(node("p","暂无互动指标历史快照。","hint"));
  if(data.detail_state==="complete"&&data.detail_text)content.append(node("pre",data.detail_text));
  else content.append(node("div","正文尚未保存；已有附件（若有）可在下方查看。历史列表覆盖与正文、媒体完整性分开计算。","detail-warning"));
  const links=node("div",null,"detail-links");
  for(const [url,label]of [[data.source_url,"查看原始作品"],[data.archive_url,"打开本地归档"]]){const link=safeLink(url,label);if(link)links.append(link);}
  content.append(links);
  if(data.assets?.length){
    content.append(node("h3","已登记附件"));
    for(const asset of data.assets){
      const entry=node("p",`${asset.kind==="video"?"视频":"图片"} · ${asset.asset_id||"附件"} · ${asset.state==="complete"?"本地文件已验证":"本地文件缺失或校验失败；登记记录已保留，请保存正文与缺失媒体后刷新详情"}`,"meta");
      const link=asset.state==="complete"?safeLink(asset.url||asset.archive_url,"打开附件"):null;
      if(link)entry.append(document.createTextNode(" · "),link);
      content.append(entry);appendLocalMedia(content,asset);
    }
  }
  if(!$("item-dialog").open)$("item-dialog").showModal();
}

$("subscribe-form").addEventListener("submit",(event)=>{event.preventDefault();const b=event.submitter;perform(b,async()=>{const result=await api("/api/subscriptions",{text:$("link").value,display_name:$("display-name").value||undefined});$("link-result").textContent=result.message||"订阅已保存。作者核验与采集能力请查看订阅和平台状态。";notify($("link-result").textContent);await refresh(true);});});
$("resolve-item-form").addEventListener("submit",event=>{event.preventDefault();perform(event.submitter,resolveAndSaveItem);});
$("confirm-subscription").onclick=()=>perform($("confirm-subscription"),confirmSubscription);
$("cancel-subscription").onclick=()=>$("confirm-subscription-dialog").close();
$("confirm-subscription-dialog").addEventListener("close",()=>{ui.confirmAuthor=null;});
$("subscription-platform").onchange=()=>renderSubscriptions(ui.workspace?.subscriptions||[]);
$("subscription-tag").onchange=()=>renderSubscriptions(ui.workspace?.subscriptions||[]);
$("preview-selected-archive").onclick=()=>perform($("preview-selected-archive"),previewSelectedArchive);
$("start-selected-archive").onclick=()=>perform($("start-selected-archive"),startSelectedArchive);
$("close-selected-archive").onclick=()=>$("selected-archive-dialog").close();
$("selected-archive-dialog").addEventListener("close",()=>{ui.previewAuthors=null;});
$("save-tags").onclick=()=>perform($("save-tags"),saveTags);
$("close-tags").onclick=()=>$('tag-dialog').close();
$("tag-dialog").addEventListener("close",()=>{ui.tagAuthor=null;});
$("classify").onclick=()=>perform($("classify"),async()=>{if(!$("link").reportValidity())return;const r=await api("/api/links/classify",{text:$("link").value});$("link-result").textContent=`${nameOf(r.platform)} · ${kindName[r.kind]||r.kind}。${r.message||""}${r.candidate_author_id?` 候选作者 ID：${r.candidate_author_id}`:""}`;});
for(const b of document.querySelectorAll("[data-job]"))b.onclick=()=>perform(b,()=>startJob(b.dataset.job));
$("refresh").onclick=()=>perform($("refresh"),async()=>{await refresh(true);notify("已刷新本机持久状态。");});
$("import-validation").onclick=()=>perform($("import-validation"),async()=>{const r=await api("/api/import/validation",{});notify(r.message||"已有验证资料已导入工作区，原件保留。请查看作者、作品以及正文缺失标记。");await refresh(true);$("subscriptions").scrollIntoView();});
$("library-filters").addEventListener("submit",event=>{event.preventDefault();applyFilters().catch(e=>notify(e.message,true));});
$("reset-filters").onclick=()=>{$("library-filters").reset();applyFilters().catch(e=>notify(e.message,true));};
$("prev-page").onclick=()=>{ui.offset=Math.max(0,ui.offset-ui.limit);loadItems().catch(e=>notify(e.message,true));};
$("next-page").onclick=()=>{if(ui.offset+ui.limit>=ui.total)return;ui.offset+=ui.limit;loadItems().catch(e=>notify(e.message,true));};
$("close-detail").onclick=()=>$("item-dialog").close();
$("item-dialog").addEventListener("close",()=>{ui.detailRequest+=1;$("detail-source-url").value="";for(const video of $("detail-content").querySelectorAll("video"))video.pause();});
$("item-dialog").addEventListener("click",event=>{if(event.target===$("item-dialog")){const r=$("item-dialog").getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$("item-dialog").close();}});
async function runDemo(inject) {const result=await api("/api/demo/run",{inject_failures:inject});$("demo-result").textContent="模拟结果，仅用于离线流程验证，不计入真实资料与 G1。冷却任务到期后可继续。";const list=$("demo-runs");list.replaceChildren();for(const r of result.runs||[])list.append(node("div",`${nameOf(r.platform)} / ${r.author_id} · ${num(r.pages)} 页 · ${num(r.item_count)} ID · ${stateName[r.state]||r.state}${r.reason?` · ${reasonName[r.reason]||r.reason}`:""}`,"demo-result-row"));}
$("run-demo").onclick=()=>perform($("run-demo"),()=>runDemo(true));$("resume-demo").onclick=()=>perform($("resume-demo"),()=>runDemo(false));
try{$("theme").value=localStorage.getItem("creator-archive-theme")||"system";}catch{}document.documentElement.dataset.theme=$("theme").value;$("theme").onchange=()=>{document.documentElement.dataset.theme=$("theme").value;try{localStorage.setItem("creator-archive-theme",$("theme").value);}catch{}};
for(const a of document.querySelectorAll(".sidebar nav a"))a.addEventListener("click",()=>{for(const other of document.querySelectorAll(".sidebar nav a"))other.classList.toggle("active",other===a);});
const recovery = {job:null,offset:0,total:0,request:0};
async function openRecovery(run) {
  recovery.job=run;recovery.offset=0;$("recovery-links").value="";$("recovery-error").textContent="";
  $("recovery-title").textContent=isPagePipeline(run)?"按页任务未完成作品":"查看未完成作品与补充链接";$("recovery-link-fields").hidden=isPagePipeline(run)||!!run.parent_job_id;$("recovery-resume").textContent=run.mode==="demo_archive"?"继续原前10篇测试":run.mode==="author_archive"?"继续原单作者任务":run.mode==="page_archive"?"继续原两页任务":"补充链接并继续原任务";$("recovery-resume").disabled=!run.can_resume;$("recovery-dialog").showModal();await loadRecovery();
}
async function loadRecovery() {
  const request=++recovery.request, job=recovery.job.id;
  const data=await api(`/api/jobs/${job}/failures?offset=${recovery.offset}&limit=20`);
  if(request!==recovery.request||job!==recovery.job?.id)return;
  recovery.total=data.total;const list=$("recovery-list");list.replaceChildren();
  for(const item of data.items){const row=node("article",null,"job-issue");row.append(node("strong",item.item_id),node("p",item.message),node("p",item.next_step));list.append(row);}
  if(!data.items.length)list.append(node("p","当前没有已尝试失败的作品；待处理项仍保存在原任务中。"));
  $("recovery-page").textContent=`${data.total} 个未完成作品 · 第 ${Math.floor(recovery.offset/20)+1} 页`;
  $("recovery-prev").disabled=recovery.offset===0;$("recovery-next").disabled=recovery.offset+20>=data.total;
}
$("close-recovery").onclick=()=>{$("recovery-links").value="";$("recovery-dialog").close();};
$("recovery-dialog").addEventListener("close",()=>{$("recovery-links").value="";});
$("recovery-prev").onclick=()=>perform($("recovery-prev"),async()=>{recovery.offset=Math.max(0,recovery.offset-20);await loadRecovery();});
$("recovery-next").onclick=()=>perform($("recovery-next"),async()=>{recovery.offset+=20;await loadRecovery();});
$("recovery-resume").onclick=()=>perform($("recovery-resume"),async()=>{
  try {const links=$("recovery-links").value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
    if(links.length>200)throw new Error("每次最多200条，可分批补充；尚未提交。");
    await api(`/api/jobs/${recovery.job.id}/resume`,links.length?{source_urls:links}:{});
    $("recovery-links").value="";$("recovery-dialog").close();notify("原任务已继续，成功项与资源会复用。");await refresh(true);
  } catch(error){$("recovery-error").textContent=error.message;}
});
refresh(true).catch(e=>notify(e.message,true));setInterval(()=>{if(!document.hidden&&!ui.busy)refresh(false,true).catch(()=>{});},4000);

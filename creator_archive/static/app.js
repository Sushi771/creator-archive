const $ = (id) => document.getElementById(id);
const platformName = {wechat: "公众号", xiaohongshu: "小红书"};
const stateName = {queued:"待处理",running:"扫描中",succeeded:"模拟历史已遍历",partial:"未完成",needs_login:"需模拟重新登录",rate_limited:"模拟限流冷却",failed:"失败"};
const kindName = {profile:"博主主页",item:"作品链接",short_link:"分享短链",collection:"合集链接"};
const reasonName = {timeout:"请求超时",page_budget_reached:"达到本次页数上限",repeated_cursor:"分页游标重复",empty_nonterminal_page:"返回空页但仍有下一页",missing_terminal_evidence:"缺少可信末页依据",identity_mismatch:"作者身份不一致",adapter_version_changed:"组件版本变化",unexpected_adapter_error:"组件异常"};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json","X-Creator-Archive":"local-validation"}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求格式无效");
  return data;
}
async function refresh() {
  const data = await api("/api/status");
  $("runs").replaceChildren();
  for (const run of data.runs) {
    const row = document.createElement("tr");
    for (const value of [`${platformName[run.platform]} / ${run.author_id}`, `${run.pages} 页`, run.item_count,
      `${stateName[run.state] || "未完成"}${reasonName[run.reason] ? ` · ${reasonName[run.reason]}` : ""}`, "未实测 / 未实测"]) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    $("runs").append(row);
  }
  if (!data.runs.length) {const row=document.createElement("tr");const cell=document.createElement("td");cell.colSpan=5;cell.textContent="尚无模拟任务。运行验证后将在这里显示检查点。";row.append(cell);$("runs").append(row);}
}
$("link-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {const r=await api("/api/links/classify",{text:$("link").value});$("link-result").textContent=`${platformName[r.platform]} · ${kindName[r.kind]}。${r.message}${r.candidate_author_id ? `。候选 ID：${r.candidate_author_id}` : ""}`;}
  catch(error){$("link-result").textContent=error.message;}
});
async function runDemo(inject) {
  $("run-demo").disabled=$("resume").disabled=true;
  try {await api("/api/demo/run",{inject_failures:inject});await refresh();$("run-result").textContent="模拟执行已返回。请查看逐博主状态；冷却中的任务需到期后再次继续。所有结果均不计入真实 G1。";}
  catch(error){$("run-result").textContent=error.message;}
  finally{$("run-demo").disabled=$("resume").disabled=false;}
}
$("run-demo").onclick=()=>runDemo(true);
$("resume").onclick=()=>runDemo(false);
$("refresh").onclick=()=>refresh().catch(e=>$("run-result").textContent=e.message);
$("theme").onchange=()=>{document.documentElement.dataset.theme=$("theme").value;localStorage.setItem("creator-archive-theme",$("theme").value);};
$("theme").value=localStorage.getItem("creator-archive-theme") || "system";
document.documentElement.dataset.theme=$("theme").value;
refresh().catch(e=>$("run-result").textContent=e.message);

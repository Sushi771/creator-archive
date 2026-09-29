# 小红书 v3 总控记录（2026-09-30）

追加独立缺陷复核：本机 rc5 页面“从检查点继续”曾在点击时无请求，集成 B 在隔离临时 SQLite、模拟 HTTP 与无账号浏览器中证明，后台刷新夹在按下/释放之间会替换按钮并丢失点击。`814d825` 前端补丁在按下期间暂缓任务卡片刷新、点击或取消后补刷新；普通及交错点击均发出一次恢复 POST 并显示反馈，取消和失焦不会永久停刷新。此复核没有访问平台或生产资料。rc6 已从 rc5 升级到 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc6-windows`，源码提交 `26f9b83ba3bd8fba3859f606b1d5542f21aa0655`；升级前备份 `backups/before-release-20260930-015819-3a3ca871` 与当前两库 `quick_check=ok`、所有业务表逐行摘要一致，私有来源与目录配置哈希一致。ZIP SHA-256 为 `6e54463f93eecf93c009b2bd671b0c37e05df81afedf4979a5ad02afa3830079`。未在生产再次点击恢复，以免未知来源码条件下重复请求。

本轮用户请求优先于冲突的 v2 约定：只交付小红书真实后台来源、博主链接订阅、全部可获取历史、正文与图片视频、增量刷新、恢复和按博主离线导出。公众号保留入口和数据，开发与验收暂缓。五篇是正常同步结果的原文匹配抽查，不是采集限额；五篇匹配和历史末页分别验收。

## 实际任务与写入边界

| 负责人 | 真实任务 ID | 工作树 / 分支 | 文件所有权 | 当前交付 |
| --- | --- | --- | --- | --- |
| 总控 | `01a0edcf-8801-7161-8a1d-a5c105d1c850` | `C:\Users\ss\OneDrive\Desktop\creator-archive` / `codex/first-release` | `AGENTS.md`、`PROJECT_STATUS.md`、`CHANGELOG.md`、本记录、共享审查、发布脚本与本机部署 | 已安装 rc6、保留真实任务 #72 与导出 #73；来源返回业务码 -100 后停止新请求 |
| 来源 A（原任务复用） | `01a0eb2d-e0bc-7c00-8ee6-94445e1f5c0f` | `C:\Users\ss\.codex\worktrees\xhs-source\creator-archive` / `codex/xhs-source` | `creator_archive/adapters/xhs_http.py`、专用测试、来源研究记录 | 来源 `a6d606b` 与单篇 404/410 分流 `0a6192a` 已整合；原任务空闲，真实请求权已交总控 |
| 集成 B（本任务子 agent） | `/root/xhs_integration` | `C:\Users\ss\.codex\worktrees\xhs-integration\creator-archive` / `codex/xhs-integration` | `creator_archive/service.py`、`app.py`、`static/`、专用测试 | 初版 `94eb593`、恢复修复 `2edcf6b` 与按钮稳定性 `814d825` 已整合；任务结束，未访问真实平台或生产资料 |

两名执行者未写生产资料、未部署、未推送。真实平台连接最初仅由来源 A 做一次列表健康检查；A 已停止，总控接管后续唯一连接。总控在旧服务停止时备份实际工作区 SQLite 与配置，核验并复用已授权会话于仓库外私有目录，未输出 Cookie；已配置新来源并升级 rc6。现有独立 WeWe 项目不在本轮范围。

## 冻结的最小接口

`creator_archive.adapters.xhs_http.XhsHttpTransport(config, platform, author_id)` 接入现有传输合同：`verify_author`、`page(author_id, cursor)`、`detail(author_id, item_id, source_url)`、`poll_latest`、`download_media`、`close` 和固定 `version`。页面必须保留真实响应的游标及明确末页证据；失败、异常空页、预算耗尽和缺游标均不算完成。新来源失败不得回退旧浏览器滚动采集。来源凭据仅在本机私有配置，不能进入 Git 或公开 CI。

## 现场进展与下一动作

接手基线本地及远端 `codex/first-release` 为 `fd440d3b2e27bfeee9f70bb51a8e97a8c8c8e377`、`0.6.0rc3`。真实工作区经运行接口确认为 `C:\Users\ss\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CreatorArchive\workspace`。升级前备份为其 `backups/before-xhs-v3-20260930-001320`，rc4 升级备份为 `backups/before-release-20260930-002750-4af54af8`。rc4 安装在 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc4-windows`，运行版与提交 `39f4bbc36fc03b3f70ff15bc3c98908e12f045bc` 匹配，仅监听 `127.0.0.1:8765`；定时刷新默认关闭。

页面粘贴作者 `660684f1000000000b00ff99` 官方主页链接后得到后台身份核验；随后正常历史任务 #72 / run #31 读取 7 页，列出 210 个稳定作品 ID，196 个任务项成功、1 个详情失败、13 个待处理。失败项 `6a6989ac000000000f0094a5` 在 rc4 归类为 `unavailable`，当次详情 HTTP 状态未知，不能称它已删除。rc5 沿原检查点恢复一次时，补取第 7 页引用的后台 `user_posted` 返回 HTTP 200、业务 `success=false, code=-100`；定向诊断重复得到相同业务码，未推进游标。该码无可信解释，未继续发平台请求。run 覆盖为 `partial`，无末页证据。另有第二位作者近期刷新任务 #74 在升级前被创建并因 `unavailable` 阻塞，触发者未知；定时刷新配置文件不存在、当前接口显示关闭，没有运行中的平台任务。

页面独立发起本机导出任务 #73 成功，导出该作者 1247 条本地已存作品、277 行正文语料，970 项缺口列在 `failures.json`；不代表本轮全历史完成。停服务后只读核对离线索引的 888 个本地引用均存在；内置浏览器因安全策略不允许访问 `file://`，故没有本轮离线页面人工阅读证据，随后服务已重启。与升级前备份比，196 个成功项中补上 8 个原缺正文、增加 18 个附件登记；原有非空正文未覆盖、943 个旧附件文件哈希相同。新来源五篇原文匹配仍为 0/5。

来源细节：固定 `xhshow==0.2.0` 仅用于本机授权会话的后台 `user_posted` 请求签名，作品详情由官方网页 SSR 解析，媒体由现有下载器保存。正式采集不滚动主页或逐篇操控浏览器；本人授权入口可打开官方登录页面。`history_complete=true` 不被用作原站历史证明；后续须以真实页链和可信末页另验。rc5 从干净提交 `c2b0f65bc933c8b3b0f2371a8ec4adfc526e36b9` 构建并安装在 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc5-windows`，备份为 `backups/before-release-20260930-005816-fed81f9c`；Python 272 项通过（4 项跳过），Node 前端合同通过。当前下一动作依赖获准且可用的来源恢复：可由账号持有人核对官方会话与访问状态，或有维护中的后台来源明确支持此作者历史分页。此前不能把业务码 -100 当可信末页，不能循环重试；旧 421 篇正文和旧导出不得算新版抽查。

已持久记录的正常同步计数为 #72 的 7 个列表页、196 次成功的 `xhs_http_ssr` 详情快照和 1 个失败项；18 条新附件登记不能等同于精确网络请求量。额外验收请求为 rc5 一次恢复补页和一次只输出状态/业务码的定向诊断；初始来源 A 与页面订阅身份核验各一次列表请求。应用未持久记录所有媒体 HTTP 请求数，不能从附件行数反推。预览 ZIP `CreatorArchive-0.6.0rc5-windows.zip` 的 SHA-256 为 `9856deb42c3b0bf4cfd0e578752c6044f5c2297c07694c2d61dfe0d5a33005ad`，不含私有资料。

预览包已上传至 [v0.6.0-rc.5](https://github.com/Sushi771/creator-archive/releases/tag/v0.6.0-rc.5)，远端附件 digest 与本机 ZIP 一致，标签指向 `c2b0f65bc933c8b3b0f2371a8ec4adfc526e36b9`。[Windows CI #24](https://github.com/Sushi771/creator-archive/actions/runs/36605642901) 全部通过；[PR #1](https://github.com/Sushi771/creator-archive/pull/1) 仍为面向 `main` 的草稿，未合并。测试路径长短名断言修复 `3bf681d` 只改测试文件，不改变已安装包。来源 A 和集成 B 均已结束；没有实际后继任务，平台后续恢复须等官方会话/访问条件得到核对。

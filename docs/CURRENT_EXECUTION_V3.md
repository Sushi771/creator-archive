# 小红书 v3 总控记录（2026-09-30）

本轮用户请求优先于冲突的 v2 约定：只交付小红书真实后台来源、博主链接订阅、全部可获取历史、正文与图片视频、增量刷新、恢复和按博主离线导出。公众号保留入口和数据，开发与验收暂缓。五篇是正常同步结果的原文匹配抽查，不是采集限额；五篇匹配和历史末页分别验收。

## 实际任务与写入边界

| 负责人 | 真实任务 ID | 工作树 / 分支 | 文件所有权 | 当前交付 |
| --- | --- | --- | --- | --- |
| 总控 | `01a0edcf-8801-7161-8a1d-a5c105d1c850` | `C:\Users\ss\OneDrive\Desktop\creator-archive` / `codex/first-release` | `AGENTS.md`、`PROJECT_STATUS.md`、`CHANGELOG.md`、本记录、共享审查、发布脚本与本机部署 | A/B 提交已整合；主分支完整回归与旧库副本迁移通过；待真实闭环 |
| 来源 A（原任务复用） | `01a0eb2d-e0bc-7c00-8ee6-94445e1f5c0f` | `C:\Users\ss\.codex\worktrees\xhs-source\creator-archive` / `codex/xhs-source` | `creator_archive/adapters/xhs_http.py`、专用测试、来源研究记录 | `a6d606b` 已提交，原任务空闲；1 次真实列表首屏成功，真实请求权已交总控 |
| 集成 B（本任务子 agent） | `/root/xhs_integration` | `C:\Users\ss\.codex\worktrees\xhs-integration\creator-archive` / `codex/xhs-integration` | `creator_archive/service.py`、`app.py`、`static/`、专用测试 | `94eb593` 已提交，任务结束；隔离模拟闭环通过，未访问真实平台 |

两名执行者未写生产资料、未部署、未推送。真实平台连接最初仅由来源 A 做一次列表健康检查；A 已明确停止，总控接管后续唯一连接。总控已在服务停止时对实际工作区备份 SQLite 与配置，并把隔离本人会话逐字节复制到仓库外的私有目录，未输出 Cookie；尚未配置正式来源或升级。现有独立 WeWe 项目不在本轮范围。

## 冻结的最小接口

`creator_archive.adapters.xhs_http.XhsHttpTransport(config, platform, author_id)` 接入现有传输合同：`verify_author`、`page(author_id, cursor)`、`detail(author_id, item_id, source_url)`、`poll_latest`、`download_media`、`close` 和固定 `version`。页面必须保留真实响应的游标及明确末页证据；失败、异常空页、预算耗尽和缺游标均不算完成。新来源失败不得回退旧浏览器滚动采集。来源凭据仅在本机私有配置，不能进入 Git 或公开 CI。

## 当前基线与下一动作

基线本地及远端 `codex/first-release` 均为 `fd440d3b2e27bfeee9f70bb51a8e97a8c8c8e377`，原主工作树干净；版本 `0.6.0rc3`。旧安装状态指向本机 `127.0.0.1:8765`，本次核查时端口未监听；启动状态文件指向 `C:\Users\ss\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CreatorArchive\workspace`。该目录旧库只读 `quick_check=ok`，订阅 3、作品 1409、附件 943；未发现 `sources.json`。这不是新来源验证结果。

当前工程证据：A 的 `user_posted` 真请求对作者 `660684f1000000000b00ff99` 返回 30 篇、`has_more=true`，逐篇稳定作者 ID 一致；未取详情或媒体。B 的隔离模拟 3 页 6 篇跨页正文、图片、视频与导出及暂停恢复通过。整合主分支 Python 265 项通过、4 项跳过，Node 前端合同通过；旧库副本初始化新增暂停表后，订阅 3、作品 1409、附件 943、分页 115、历史运行 30、任务 70 行及逐行摘要未变。备份在实际工作区 `backups/before-xhs-v3-20260930-001320`，正式旧版仍停止且未迁移。

下一动作：总控从干净提交构建预览包，升级并核对运行版本、资料目录与备份；用已确认作者沿一条新来源任务实际验证正文/媒体、正常历史页链、可追溯的五篇原文匹配及按作者导出。若登录、限流、验证或来源格式阻断，保留检查点并单独报告；旧 421 篇正文和旧导出不得算作新版真实同步或抽查。

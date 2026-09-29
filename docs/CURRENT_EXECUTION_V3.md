# 小红书 v3 总控记录（2026-09-30）

本轮用户请求优先于冲突的 v2 约定：只交付小红书真实后台来源、博主链接订阅、全部可获取历史、正文与图片视频、增量刷新、恢复和按博主离线导出。公众号保留入口和数据，开发与验收暂缓。五篇是正常同步结果的原文匹配抽查，不是采集限额；五篇匹配和历史末页分别验收。

## 实际任务与写入边界

| 负责人 | 真实任务 ID | 工作树 / 分支 | 文件所有权 | 当前交付 |
| --- | --- | --- | --- | --- |
| 总控 | `01a0edcf-8801-7161-8a1d-a5c105d1c850` | `C:\Users\ss\OneDrive\Desktop\creator-archive` / `codex/first-release` | `AGENTS.md`、`PROJECT_STATUS.md`、`CHANGELOG.md`、本记录、共享审查、发布脚本与本机部署 | 已安装 rc4、执行真实任务 #72 与导出 #73；整合恢复修复并待升级 rc5 |
| 来源 A（原任务复用） | `01a0eb2d-e0bc-7c00-8ee6-94445e1f5c0f` | `C:\Users\ss\.codex\worktrees\xhs-source\creator-archive` / `codex/xhs-source` | `creator_archive/adapters/xhs_http.py`、专用测试、来源研究记录 | 来源 `a6d606b` 与单篇 404/410 分流 `0a6192a` 已整合；原任务空闲，真实请求权已交总控 |
| 集成 B（本任务子 agent） | `/root/xhs_integration` | `C:\Users\ss\.codex\worktrees\xhs-integration\creator-archive` / `codex/xhs-integration` | `creator_archive/service.py`、`app.py`、`static/`、专用测试 | 初版 `94eb593` 与恢复修复 `2edcf6b` 已整合；任务结束，未访问真实平台或生产资料 |

两名执行者未写生产资料、未部署、未推送。真实平台连接最初仅由来源 A 做一次列表健康检查；A 已停止，总控接管后续唯一连接。总控在旧服务停止时备份实际工作区 SQLite 与配置，核验并复用已授权会话于仓库外私有目录，未输出 Cookie；已配置新来源并升级 rc4。现有独立 WeWe 项目不在本轮范围。

## 冻结的最小接口

`creator_archive.adapters.xhs_http.XhsHttpTransport(config, platform, author_id)` 接入现有传输合同：`verify_author`、`page(author_id, cursor)`、`detail(author_id, item_id, source_url)`、`poll_latest`、`download_media`、`close` 和固定 `version`。页面必须保留真实响应的游标及明确末页证据；失败、异常空页、预算耗尽和缺游标均不算完成。新来源失败不得回退旧浏览器滚动采集。来源凭据仅在本机私有配置，不能进入 Git 或公开 CI。

## 现场进展与下一动作

接手基线本地及远端 `codex/first-release` 为 `fd440d3b2e27bfeee9f70bb51a8e97a8c8c8e377`、`0.6.0rc3`。真实工作区经运行接口确认为 `C:\Users\ss\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CreatorArchive\workspace`。升级前备份为其 `backups/before-xhs-v3-20260930-001320`，rc4 升级备份为 `backups/before-release-20260930-002750-4af54af8`。rc4 安装在 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc4-windows`，运行版与提交 `39f4bbc36fc03b3f70ff15bc3c98908e12f045bc` 匹配，仅监听 `127.0.0.1:8765`；定时刷新默认关闭。

页面粘贴作者 `660684f1000000000b00ff99` 官方主页链接后得到后台身份核验；随后正常历史任务 #72 / run #31 读取 7 页，列出 210 个稳定作品 ID，196 个任务项成功、1 个详情失败、13 个待处理。失败项 `6a6989ac000000000f0094a5` 在旧版归类为 `unavailable`，具体 HTTP 状态未知；不能称它已删除。run 覆盖为 `partial`，无末页证据，未重发相同请求。页面独立发起本机导出任务 #73 成功，导出该作者 1247 条本地已存作品，缺口在 `failures.json`；不代表本轮全历史完成。与升级前备份比，196 个成功项中补上 8 个原缺正文、增加 18 个附件登记；原有非空正文未覆盖、943 个旧附件文件哈希相同。新来源五篇原文匹配仍为 0/5。

来源细节：固定 `xhshow==0.2.0` 仅用于本机授权会话的后台 `user_posted` 请求签名，作品详情由官方网页 SSR 解析，媒体由现有下载器保存。正式采集不滚动主页或逐篇操控浏览器；本人授权入口可打开官方登录页面。`history_complete=true` 不被用作原站历史证明；后续须以真实页链和可信末页另验。rc5 整合后 Python 272 项通过（4 项跳过）及 Node 前端合同通过，下一步从干净提交构建并升级，再只恢复 #72 一次。若仍是未知来源故障、登录/验证/限流或页链变化，停在原检查点，保留成功资源并说明条件；旧 421 篇正文和旧导出不得算新版抽查。

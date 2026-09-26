# 微信公众号项目复用核查（2026-09-26）

本次为 GitHub README、许可证、源码和 GitHub API 静态核查；没有登录平台、运行采集、安装项目，也未复现用户实际刷新失败。代码具备相关分支不代表当前线上稳定。日期均为 GitHub 返回时间；涉及平台变化的说法注明为上游文档陈述。

## 1. 版本与许可快照

| 项目 | 本次核查 commit / 最近提交 | 最新 Release | 状态与许可 | 建议 |
|---|---|---|---|---|
| [Sushi771/wewe-rss-ss](https://github.com/Sushi771/wewe-rss-ss) | `4cd64f1a50cfd222b2c065e456528b200aac9370` / 2026-08-18 | `v2.6.1` / 2026-08-18 | 未归档；MIT；用户已有部署 | 优先保留订阅资产和导出经验；补齐任务、历史、按博主归档；解析后端需验证 |
| [cooderl/wewe-rss](https://github.com/cooderl/wewe-rss) | `e751c64294080d83deb1610d2667bed3cfa4b393` / 2026-03-20 | 页面显示 v2.6.1，实际 tag `release-20241215130232` / 2024-12-15 | 2026-05-11 已归档；MIT | 仅作为用户 fork 上游沿革与迁移参考，不作为仍受维护的默认引擎 |
| [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) | `126993c81a00466e9a6bbab041eef34ab27abe9c` / 2026-09-24 | `v1.5.3` / 2026-08-13 | 未归档；LICENSE 原文 MIT（GitHub API 自动标识 Other） | 有订阅、调度、API、多格式导出，优先做公众号替代引擎验证；必须区分采集模式 |
| [yeximm/Access_wechat_article](https://github.com/yeximm/Access_wechat_article) | `412b4a6d2f5005f01f70b20ad1c8530849eaafd3` / 2026-08-23 | `v2.2.0` / 2026-08-23 | 未归档；CC BY-NC-SA 4.0 | Windows 离线保版、媒体清单与归档交互参考；非商业限制未解决前不把代码并入计划用于公司业务的产品 |

证据：各仓库 `/commits?per_page=1`、`/releases/latest` 的 GitHub REST API；本地克隆 `git log -1`；[cooderl Releases（含 archived 横幅）](https://github.com/cooderl/wewe-rss/releases)。星数仅作规模补充：核查时 cooderl 9663、we-mp-rss 4738、Access 667；不以星数推定采集成功率。

## 2. 用户现有 fork：能保留什么，实际缺什么

源文件固定版本：

- [LICENSE](https://github.com/Sushi771/wewe-rss-ss/blob/4cd64f1a50cfd222b2c065e456528b200aac9370/LICENSE)
- [配置](https://github.com/Sushi771/wewe-rss-ss/blob/4cd64f1a50cfd222b2c065e456528b200aac9370/apps/server/src/configuration.ts)
- [订阅与刷新服务](https://github.com/Sushi771/wewe-rss-ss/blob/4cd64f1a50cfd222b2c065e456528b200aac9370/apps/server/src/trpc/trpc.service.ts)
- [API 与 Markdown/图片导出](https://github.com/Sushi771/wewe-rss-ss/blob/4cd64f1a50cfd222b2c065e456528b200aac9370/apps/server/src/trpc/trpc.router.ts)
- [前端批量导出](https://github.com/Sushi771/wewe-rss-ss/blob/4cd64f1a50cfd222b2c065e456528b200aac9370/apps/web/src/pages/feeds/index.tsx)

已由源码确认：

1. 有从文章链接提取公众号、订阅源、分页历史、微信读书账号管理、RSS/Atom/JSON、Obsidian Markdown 和图片本地化。
2. 订阅列表来自外部中转：默认 `PLATFORM_URL=https://weread.111965.xyz`，`/api/v2/platform/mps/{id}/articles`；请求携带读书 token。应用本地部署并不等于所有采集环节均在本地，也不能据此保证外部中转长期可用。
3. `refreshAllMpArticlesAndUpdateFeed()` 逐公众号调用默认第 1 页，单公众号异常仅写日志后继续。它不是“所有历史全部更新”，调用结束也不意味着每源成功。
4. 历史回补使用 `getHistoryMpArticles()`；根据数据库文章数计算起始页，以返回条数少于 `defaultCount` 置 `hasHistory=0`，最多循环 1000 次。内存只有一个 `inProgressHistoryMp`，不是持久化的按博主任务游标。
5. 401 写库禁用账号，429 将账号加入当天内存禁用集合，部分错误重试；这些是刷新失败候选原因，尚不能断言用户故障是哪一种。
6. Obsidian 路径按导出日期/标题写文件，附件在根目录 `attachments/`；未按博主分组。相同导出日、相同标题可能覆盖，路径还要测试 Obsidian 与普通 Markdown 阅读器差异。
7. 图片失败后可回退在线代理链接；空正文可输出提示文本；所以“有 .md”不等于“完整离线归档”。普通 Markdown 导出分支也可能只写代理图片 URL。未看到对应全视频离线下载实现。
8. 前端批量导出处理选中文章，遇单篇失败即中断；不等于“一键下载所有已订阅博主”。

设计结论（建议，不是现有能力）：把原订阅 ID/来源 URL/博主名称/历史文章记录迁移到统一模型；沿用 MIT 的解析与导出模块需保留版权。另建持久任务队列、按源结果、显式历史扫描游标、暂停续传、正文与媒体独立状态；全局按钮可启动“补齐历史+更新最新+下载缺失媒体”，失败后仅重试缺失项。文件命名需包含稳定内容 ID，禁止以标题作为唯一键。

## 3. we-mp-rss：应优先验证，但不能误读“微信读书模式”

- [固定版本 README](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/README.zh-CN.md)：Python/FastAPI、Vue、SQLite/MySQL；订阅与定时任务、授权过期提醒、API/Webhook、MD/DOCX/PDF/JSON 导出。
- [LICENSE](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/LICENSE)：MIT。不能仅因 API 返回 Other 就写“无许可证”。
- [采集器工厂](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/core/wx/base.py)：`free_publish`、`playwright`、`web/app/api`、`weread_mp` 等模式；后台模式使用公众号平台登录 Cookie/token。
- [新版发布列表模式](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/core/wx/model/free_publish.py)：有分页与多个端点降级实现。是否能对用户指定公众号补齐历史仍需授权样本运行；README 默认 `MAX_PAGE=5`，不能默认宣称全历史。
- [微信读书模式说明](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/docs/weread-mp.md)：上游陈述旧文章列表接口已废弃，当前 `/api/mp/cover` 只返回最新一篇，无法回补历史；使用用户本地 Cookie 直接访问 weread.qq.com，不经过第三方中转。正文失败记录可能空正文入库且下一轮跳过，这与本产品重试要求不一致。
- [批量导出](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/core/exporter.py) 与 [HTML 转 Markdown](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/tools/mdtools/html2doc.py)：提供批量格式转换；本次所读 Markdown 转换未见全媒体下载保障，不能把 Markdown/PDF 导出等同所有图片视频已离线。

建议使用方式：先作为独立本地服务/适配器与现有 WeWe 对照；保留能验证通过的列表获取、正文、调度/API能力。新产品自己的任务状态与归档规范不直接依赖其 UI 状态。若只可用 `weread_mp`，界面必须显示“仅最近单篇增量，历史暂不可用”，不得把它装作全量订阅能力交付。

## 4. Access_wechat_article：离线归档可参考，部署与许可有差异

- [功能说明](https://github.com/yeximm/Access_wechat_article/blob/412b4a6d2f5005f01f70b20ad1c8530849eaafd3/doc/features.md)：当前 v2 是 Windows 10/11 桌面工作台；登录微信 PC 并打开公众号主页，由 UI 自动点击文章、MITM 捕获。需要本地代理与 CA 证书管理，不是单纯把 URL 交给 HTTP API 就拥有无人值守订阅。
- [README](https://github.com/yeximm/Access_wechat_article/blob/412b4a6d2f5005f01f70b20ad1c8530849eaafd3/README.md)：公开文章元数据、任务/异常状态、数据档案、Windows WebView2 + FastAPI。
- [媒体下载](https://github.com/yeximm/Access_wechat_article/blob/412b4a6d2f5005f01f70b20ad1c8530849eaafd3/src/modules/archive/offline_media_downloader.py)：有媒体下载、临时文件与结果状态；明确返回“暂不支持 m3u8/HLS 媒体归档”。
- [离线 HTML 重写](https://github.com/yeximm/Access_wechat_article/blob/412b4a6d2f5005f01f70b20ad1c8530849eaafd3/src/modules/archive/offline_html_rewriter.py)：普通 video/audio、微信 iframe 的离线改写；视频号卡片也有处理分支，但不等于所有嵌入视频可以下载或离线播放。
- [许可](https://github.com/yeximm/Access_wechat_article/blob/412b4a6d2f5005f01f70b20ad1c8530849eaafd3/LICENSE)：CC BY-NC-SA 4.0。若未来用途包含公司竞争内容整理，不在许可范围未确认时把源代码复制合入；可研究公开架构和独立设计，或先取得兼容用途授权。接口封装不会自动消除许可限制。

建议参考：`storages/公众号名/发布时间 标题/`、`index.html + assets + article_detail.json`、采集状态/缓存状态分离、代理退出恢复。不要把它当默认后台订阅引擎；不启动代理、不安装证书，直到用户实际选择并明确配置。

## 5. 必须进入产品验收的结论

1. “全量”应定义为当前账号、当前渠道可访问的全部内容；可观察到最后页/明确终止证据才标记完成。未知历史范围、被删内容、权限限制须显示，禁止空结果即判全部完成。
2. 新增订阅必须识别稳定博主 ID，由用户确认博主名称；后续每个平台按该 ID 拉取列表、分页、去重、增量更新；单条下载只是入口能力。
3. 默认全局操作必须针对所有已订阅博主，不依赖列表当前页或勾选当前可见文章。
4. 按 `平台/博主名_博主ID/日期_标题_内容ID/` 写正文与媒体；同时保留原始排版 HTML、阅读 Markdown、结构化 metadata/manifest，AI导出另存JSONL且不混入登录信息。
5. 列表获取成功、正文成功、图片成功、视频成功、导出成功是独立状态；缺一不可标“完整归档”。替代引擎切换要复用统一 ID/URL 映射，避免同文复制。
6. “训练文风”在当前阶段交付可追溯语料和文风分析输入；真实模型训练/微调不因下载完成自动成立，单独立项和选择样本范围。
7. 优先验证两个公众号（一个近期多发，一个历史较长）与含图片/普通视频/HLS/视频号卡片各样本；断网后离线打开检查，再检查同标题、防重复、单源失败、过期登录恢复、断点续传。

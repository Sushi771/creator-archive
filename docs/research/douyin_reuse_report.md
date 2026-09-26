# 抖音及多平台开源复用研究

核查日期：2026-09-26（Asia/Shanghai）。研究方式：GitHub 官方仓库主页、README、LICENSE、GitHub API 元数据和浅克隆固定提交的源码静态检查；没有安装运行项目，没有验证任何真实平台账号或下载成功率。以下“支持”指仓库声明/源码存在对应实现，不代表本环境已实测成功。

## 决策摘要

首版仍只验收公众号、小红书。抖音作为后续平台扩展设计，优先验证 `Evil0ctal/Douyin_TikTok_Download_API` 的 API 适配；希望较轻的 Python 集成时再比较 F2。不要因有“作者批量下载”就认定现成项目完整满足“订阅所有博主、全历史更新、一键按博主归档、Obsidian图文排版、AI资料导出”的产品需求。统一订阅表、全历史作业、游标持久化、缺失清单和导出格式应由本产品掌握。

## 候选及核查证据

| 项目 | 核查固定提交 / 维护信号 | 作者及媒体能力 | 本地与API | 许可与采用意见 |
|---|---|---|---|---|
| [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | `737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f`；提交 2026-09-22 PDT（北京时间9月23日）；代码版本5.1.1；GitHub API star约20,338 | 抖音/TikTok作者列表分页、视频图集、作者定时监控、历史回溯、按作者下载、去重、部分失败状态、下载哈希 | Docker Compose；Python/FastAPI、PostgreSQL、Redis、可选浏览器和Go下载sidecar；REST、streamable-http MCP、CLI、Web控制台 | Apache-2.0；README明确可商业/闭源使用并要求保留声明。优先验证，不能直接称“全量订阅现成即用” |
| [F2](https://github.com/Johnserf-Seed/f2) | `2b49fc76e3622ccc8d684272f440bc9c7a7d71b2`；2026-09-25 PDT（北京时间9月26日）；默认分支是开发版`v0.0.1.8-pw3`；star约2,660 | Douyin作者列表异步分页、图文视频、单作品/合集、用户目录、下载断点续传 | Python库和CLI；README仍把WebAPI列为计划，不应描述为现成HTTP服务 | LICENSE Apache-2.0；README另有学习研究声明，并明确商业化需标注项目仓库地址/保留版权，应完整登记，不忽略附加表述。作为较轻适配候选 |
| [TikTokDownloader / DouK-Downloader](https://github.com/JoeanAmier/TikTokDownloader) | `473c90ff70c663cfb69310fff2b8d5192f200661`；2026-09-16；版本5.8；star约16,310；仓库9月22日仍有推送 | 作者作品、多账号批量、增量下载、记录作品ID、CSV/XLSX/SQLite、媒体Range续传、作品筛选 | Windows程序/CLI、Docker、WebAPI；README明确WebUI代码未更新、计划6.0重构 | GPL-3.0；**README明确加密参数算法不再维护**，需要自备生成代码。可参考文件归档/下载状态设计，不作为默认可用采集核心；发布修改版须评估GPL义务 |
| [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | `380b426000aac3d612837ed72c99808347dc94c9`；2026-09-19；star约65,719；最近提交修复抖音请求头 | 小红书/抖音等作者主页分页；媒体支持视频图文封面；登录态保存 | Playwright/CDP、Python CLI、WebUI；当前README推荐Chrome144+现有浏览器调试模式 | **NON-COMMERCIAL LEARNING LICENSE 1.1**，非商业学习研究许可。可研究架构；公司用途或商用整合未经作者书面许可不能默认引入。开源版不应冒用Pro断点续爬等能力 |

Stars 为2026-09-26 GitHub API快照，仅表社区规模，不证明当前接口成功率；各项目均非 archived。

## 重要源码边界

### Douyin_TikTok_Download_API：最贴近，但仍需补全需求

- `src/dtk/platforms/douyin/parser.py::parse_author_posts` 返回统一 `Page(items,cursor,has_more)`，游标源为抖音 `max_cursor`；`params.py::author_posts_params`支持传入游标。可供本产品的分页协议适配。
- `src/dtk/services/watchlist.py::task_for`与`src/dtk/worker/watcher.py`具备作者监控、作业入队和失败退避。**当前作者定时监控每次只查最新20条**；条目虽保存`pages`，运行未据此深翻。平台高频作者可能在两次刷新间超过20条，直接复用会漏内容。
- `src/dtk/worker/ops/archive_sweep.py::backfill`是独立历史任务；默认5页、硬上限20页×20条，从空游标重新起步，逐页归档提交。没有将末尾cursor作为作业检查点持久化/返回用于下一段继续。因此不能用它承诺不受页数限制的历史回溯。
- `documents/zh/08-downloads-and-library.md`明确“作者最新作品”下载最多40页，深历史走单独回溯任务。产品必须显示“已取到的最早日期/条数/是否触达末页/是否受上限终止”。
- 下载器支持作品层去重、逐文件SHA-256、`partial`状态；**明确没有媒体字节断点续传**。`docker/downloader/download.go`每次以`O_TRUNC`重写`.part`，完成后rename，过期链接重新解析后重下。不得误报为断点续传。
- 项目 v5 为新重写，不沿用v4代码，历史星标不能直接等同v5长期成熟。优先从现有服务HTTP/MCP适配，固定版本隔离更新，避免把整个数据库/队列硬拷入Windows首版。

源码链接：
- https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/services/watchlist.py
- https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/src/dtk/worker/ops/archive_sweep.py
- https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/documents/zh/08-downloads-and-library.md
- https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/737bf3dfe9de1dbff57990c0ec4c9e02c75c3d0f/LICENSE

### F2：可组合的采集/下载部件

- `f2/apps/douyin/handler.py::fetch_user_post_videos(sec_user_id,min_cursor,max_cursor,page_counts,max_counts)`已有异步分页、起始游标、日期区间和数量控制。
- `f2/dl/base_downloader.py`实现Range及响应校验：服务端忽略Range回200时重写已有片段；比只“追加文件”更值得复用。
- 以上不等于订阅作业跨重启的断点续爬。我们仍需保存作业cursor、最后成功页、已发现ID集合/唯一索引，并处理分页游标重复、置顶作品、空页但has_more等边界。
- 默认分支是开发版；验证选定发布版或固定commit，不能每次自动跟随最新分支。

源码链接：
- https://github.com/Johnserf-Seed/f2/blob/2b49fc76e3622ccc8d684272f440bc9c7a7d71b2/f2/apps/douyin/handler.py
- https://github.com/Johnserf-Seed/f2/blob/2b49fc76e3622ccc8d684272f440bc9c7a7d71b2/f2/dl/base_downloader.py
- https://github.com/Johnserf-Seed/f2/blob/2b49fc76e3622ccc8d684272f440bc9c7a7d71b2/README.md

### 另两项不宜成为默认生产依赖

TikTokDownloader：`src/interface/account.py`和`template.py`有cursor/has_more迭代；`src/downloader/download.py`有作品ID记录和Range处理。下载工程值得参考，但签名不再维护是实际维护风险；WebUI的“功能清单已支持”与当前说明“未更新、待重构”须以后者为准。

MediaCrawler：`media_platform/douyin/client.py::get_all_user_aweme_posts`有max_cursor循环，但从空游标开始、游标只在当前调用内；不得把Pro的断点续爬能力写成开源版已具备。媒体下载默认关闭，失败日志不终止爬取、现有文件跳过；本产品验收需要逐媒体完整性状态，不能把“爬取成功”当“附件全部成功”。

- https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/README.md
- https://github.com/JoeanAmier/TikTokDownloader/blob/473c90ff70c663cfb69310fff2b8d5192f200661/src/downloader/download.py
- https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/LICENSE
- https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/media_platform/douyin/client.py

## 写入本产品技术设计的复用原则

1. 核心产品维护 `resolve_source`、`resolve_author`、`list_author_contents(cursor)`、`get_content`、`download_assets`能力协议；平台模块实现协议，订阅和导出不依赖某仓库私有表结构。
2. 分离“更新内容索引/正文”与“本地媒体下载”；用户点“全部下载”时按当前所有订阅作者创建可追踪批任务，不靠列表当前页生成下载范围。
3. 全历史首次任务、日常增量、补缺任务独立；逐页检查点入库，重新运行幂等，异常空页不视为已到历史尽头；以author_id稳定归档，改昵称不另建博主重复库。
4. 输出 `平台/作者名__作者ID/日期__内容ID/`，正文Markdown保留媒体相对引用，另有metadata.json、原始可下载媒体、source_url、采集时间、缺失状态与manifest；视频转写独立文件标明机器生成，可选OCR和关键帧供AI理解。训练文风目前是可供后续分析的语料整理，不擅自等同模型微调服务。
5. 现成MCP可以参考，但本产品仍需跨平台只读 `list_authors/list_contents/read_content/get_assets`，本地绝对路径不能直接让云端ChatGPT读到文件，必须经已配置的可达连接和资源返回机制。
6. 定稿前后续实测：单作者超过3页历史；置顶/重复/删除；登录过期；中途重启后不从头丢进度；CDN URL过期；一个媒体失败；批量所有作者分目录导出；正文Markdown离线渲染；ChatGPT端实际读取媒体；长期接口兼容。当前仅调研，不宣称这些均通过。

## Web检索引用定位（供主agent复核后引用）

GitHub主页工具ref：F2 `turn15view0`；Douyin API `turn15view1`、`turn16view0`；TikTokDownloader `turn15view2`、`turn16view1`；MediaCrawler `turn15view3`、`turn16view2`。引用前主agent按系统要求自行open对应URL。

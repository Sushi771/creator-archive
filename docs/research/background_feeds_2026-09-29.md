# 后台订阅来源定向核查（2026-09-29）

本页是源码审阅，不是当前账号的真实平台成功记录。正式默认来源只通过后台 HTTP 访问本人配置的 Feed；旧 `XhsBrowserTransport` 保留作历史代码，不自动回退。

| 候选 | 实际取数 | 历史与内容结论 |
| --- | --- | --- |
| [RSSHub 小红书用户路由](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaohongshu/user.ts)、[工具函数](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaohongshu/util.ts) | Cookie 分支后台 HTTP 读主页和逐篇正文；异常无条件回退到 Playwright。 | 只处理主页初始 `user.notes`，没有完整历史分页；全文逐篇 `Promise.all` 无界并发，输出媒体 URL 但不离线下载。当前仓库 [AGPL-3.0](https://github.com/DIYgod/RSSHub/blob/master/LICENSE)，只参考字段和流程，未复制代码。 |
| [RSSWorker 小红书用户路由](https://github.com/yllhwa/RSSWorker/blob/main/src/lib/xiaohongshu/user.js) | 后台 fetch 主页 SSR。 | 标题、封面摘要；标题作 GUID，缺正文、视频和历史分页。不能作为全文或稳定作品源。 |
| [MediaCrawler 作者列表](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py) | 后台 HTTP `user_posted`，有 `user_id/cursor/num` 与 `has_more`；项目整体有浏览器会话/签名依赖。 | 可参考分页响应合同，但其数量配置上限、异常 break、无本项目检查点和[非商业许可](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)均不能直接沿用。未复制代码或使用其账号通路。 |
| [WeWe 当前交接](https://github.com/Sushi771/wewe-rss-ss/blob/main/docs/DEVELOPMENT_HANDOFF.md) | Wechat2RSS Provider 当前默认关闭，尚无授权实例真实文章验证；`/add/:id` 只受理异步刷新，`/feed/<id>.json` 读缓存。 | [上游说明](https://wechat2rss.xlab.app/deploy/qa)只抓最新约 20 篇群发内容；旧缓存保留不等于补齐订阅前历史。WeWe `?update=true` HTTP 200 不能证明刷新已完成；`mode=fulltext` 某些缓存未命中会再访问原站。此仓只读，未改配置、SQLite 或服务。 |

本项目此次实现 RSS/Atom/JSON Feed 后台读取、作者范围和稳定作品链接校验、正文/媒体离线保存。小红书的非作者范围作品链接还必须有逐篇官方作者主页字段；缺少逐篇归属证据的 Feed 会被拒绝，不从频道标题推断作者。RSS/Atom 仅作为当前 Feed 窗口增量。JSON Feed 只有在同源受控 `next_url` 页链与最后一页明确 `history_complete=true` 时才允许作者历史任务记末页；这是本项目接受的扩展合同，当前尚无真实来源满足并通过目标作者验证。一次 HTTP 200、空页、缺游标或页数预算均不算完成。来源配置和潜在访问参数只留私有 `sources.json`，不进入仓库、输出或公共 CI。

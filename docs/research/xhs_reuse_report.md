# 小红书复用核查（文档评审用）

核查日期：2026-09-26。范围：GitHub 默认分支 README、LICENSE、源码与公开 issue。仅阅读和浅克隆，没有安装、执行爬虫或登录平台，因此下述是静态核查，不是当前抓取成功率的实测结论。

## 结论

现有工具可以减少单条解析、登录、媒体下载、MCP 的重复开发，但本次没有找到一个已验证能直接满足“粘贴链接订阅作者→遍历全部可获取历史→后续增量→所有作者一键分目录离线归档”的现成完整产品。尤其不可把“下载单条笔记”“主页返回首屏”“文件断点续传”写成“作者订阅全量和任务断点恢复”。

技术优先次序建议：以可替换适配器验证 xiaohongshu-mcp 的作者识别/详情能力，评估 XHS-Downloader 媒体下载与作者归档；自有应用承担订阅、历史遍历、任务持久化、增量、统一导出和只读 MCP。MediaCrawler 可对照分页与多平台结构，但许可证限制非商业学习，未经适用授权不能作为商业用途的代码依赖。最终选型必须等多页作者+视频样本验证及许可核对后锁定。

## 1. JoeanAmier/XHS-Downloader

- 仓库：https://github.com/JoeanAmier/XHS-Downloader
- 核查固定版本：`3261312721f0b37c705ba6515885bc7f34349f2f`，提交日期 2026-09-16，提交信息 `docs: Update README.md`。
- README：https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/README.md
- LICENSE：https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/LICENSE

**已证实能力**：多个笔记链接、图片/视频/封面/livePhoto、按作者目录（`author_archive`）、按作品目录、SQLite 元信息与已下载 ID、TXT/MD、HTTP API `/xhs/detail`、Streamable HTTP MCP、文件 Range 续传。作品链接包括 `/explore/`、`/discovery/item/`、带作者路径的作品页、短链。

**作者全量的重要区别**：README 的“提取账号发布作品链接”在 Tampermonkey 用户脚本功能区。脚本可以自动滚动至加载结束（默认关闭）；主程序公开 API 是单条详情入口。`source/application/user_posted.py` 虽存在 `UserPosted`，其 `run()` 是 `...`，检索未发现其他调用，不能把它算成已完成的作者采集服务。后台订阅、分页游标持久化、定期增量是需要另外补齐的能力。

源码证据：
- https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/user_posted.py
- https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/download.py
- https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/module/note_info.py
- https://github.com/JoeanAmier/XHS-Downloader/blob/3261312721f0b37c705ba6515885bc7f34349f2f/source/application/video.py

**画质与完整性**：README 明说无 Cookie 时视频只得低分辨率；浏览器自动读取 Cookie 功能已失效，建议手动配置；旧日期分享链接可能失效。代码优先取 originVideoKey，失败后按分辨率/码率/大小选公开流。能取得哪档取决于平台返回，不保证作者上传原文件。下载器有临时文件+Range，仍需产品层增加媒体数量、长度、哈希/可打开性检查，不能把“文件存在跳过”当成完整性证明。TXT/MD 模板主要是元信息和正文，尚非正文图文离线排版；当前写入为 append，重复导出需避免内容重复追加。

**许可证**：LICENSE 为 GPL-3.0。README 另有禁止未经书面授权用于商业宣传、推广、再授权的表述。不能以“独立子进程”自动推导许可问题消失，也不能把 GPL 一概称为禁商用。建议记录该不一致/附加声明，商业发布前核清授权与依赖来源；未确认前作为候选适配器与技术参考，禁止无检查地合并到闭源核心。

**失效记录（非实测）**：
- https://github.com/JoeanAmier/XHS-Downloader/issues/429 ：2026-07-08 用户报告视频质量问题，当前 Closed as not planned，不能保证原画质。
- https://github.com/JoeanAmier/XHS-Downloader/issues/441 ：2026-07-29 用户报告图文能下载而视频提取失败，当前 Closed；搜索索引仍显示 Open，文档以打开详情时状态为准。

## 2. xpzouying/xiaohongshu-mcp

- 仓库：https://github.com/xpzouying/xiaohongshu-mcp
- 核查固定版本：`a5c8f7799980ba1fdd501999843eb2d17e4c9a9f`，2026-09-23，`fix(search): 筛选点击限制指针路径，并在按下前复核落点 (#858)`。
- 许可 Apache-2.0：https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/LICENSE
- 用户主页实现：https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/user_profile.go
- API：https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/docs/API.md
- 视频详情结构：https://github.com/xpzouying/xiaohongshu-mcp/blob/a5c8f7799980ba1fdd501999843eb2d17e4c9a9f/xiaohongshu/types.go

**适合复用**：Go + Rod 浏览器会话、二维码登录与 Cookie 保持、搜索、笔记详情、作者主页、HTTP API/MCP。详情结构映射图片和视频数据，适合由上层选择视频流。按要求保留 Apache 许可、署名与修改声明。

**不能承诺的能力**：主页函数导航后读 `window.__INITIAL_STATE__.user.notes`，只拼当前 tab 数组，未见向下滚动、游标翻页或历史断点。尽管 README 描述“所有公开笔记”，源码不能证明全历史。API 中未见完整作者离线归档接口；仓库内部 downloader 也不能直接等同于作者内容批量下载功能。其发布、评论、点赞工具不属于此次需求，自有 MCP 只暴露白名单读取工具。

**当前已知报告**：
- https://github.com/xpzouying/xiaohongshu-mcp/issues/800 ：Open，2026-08-07，视频笔记读取超时。
- https://github.com/xpzouying/xiaohongshu-mcp/issues/836 ：当前 issue 列表显示 Open，2026-09-03，已登录搜索仍超时。
- https://github.com/xpzouying/xiaohongshu-mcp/issues/838 ：当前 issue 列表显示 Open，2026-09-05，RedNote 登录识别问题；不可当作中国小红书一定同样复现。
- https://github.com/xpzouying/xiaohongshu-mcp/issues/547 ：Closed，历史 Windows 登录状态与空主页报告；README 解释显示的用户名固定，不应把固定用户名单独当登录失败证据。

**判定**：适合作为登录/内容读取候选组件，维护活跃；不足以直接作为作者订阅全量产品。

## 3. NanmiCoder/MediaCrawler

- 仓库：https://github.com/NanmiCoder/MediaCrawler
- 核查固定版本：`380b426000aac3d612837ed72c99808347dc94c9`，2026-09-19，提交为抖音请求头修复。
- 许可证：https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/LICENSE
- 作者枚举：https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/media_platform/xhs/client.py
- 作者流程：https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/media_platform/xhs/core.py
- 默认数量：https://github.com/NanmiCoder/MediaCrawler/blob/380b426000aac3d612837ed72c99808347dc94c9/config/base_config.py

**已证实能力**：多平台、指定作者主页、详情、评论、登录态缓存。`get_all_notes_by_creator` 使用 cursor/has_more，逐页回调详情，确有多页枚举逻辑。但上限受 `CRAWLER_MAX_NOTES_COUNT` 限制，核查版本默认为 15；到达配置上限并非“作者历史已全量”。页面异常或缺字段也会 break，应用必须区分完成与失败。

**媒体与恢复**：当前 README 有 `ENABLE_GET_MEDIA` / `--get_media true`，小红书/抖音等下载到按帖子 ID 的目录。媒体失败只日志、不终止内容抓取，已有文件跳过，因此不能直接把爬取任务成功解释为所有附件成功。README “断点续爬”位于 MediaCrawlerPro 宣传区；不能作为普通开源仓库的现成功能验收。应用要另做每作者检查点、item_id 去重、失败清单与恢复。

**许可与选型**：许可证是“非商业学习使用许可证 1.1”，明确书面同意前不得商业使用。代码可读不代表可任意集成。此次商业应用候选中列为“架构研究、授权后再讨论代码复用”，不能未经授权复制其平台实现。

**近期风险**：https://github.com/NanmiCoder/MediaCrawler/issues/985 （2026-09-24，Open，标题“小红书 creator 爬取失败”），正对本产品主场景。另 https://github.com/NanmiCoder/MediaCrawler/issues/927 是 RSS 请求，不能说项目现成自带订阅服务。

## 4. 新发现替代：Andy-SoulShell/xhs-downloader

https://github.com/Andy-SoulShell/xhs-downloader

README 宣称 WebUI/CLI/API/MCP/浏览器扩展、SQLite 后台队列和重启恢复、媒体指纹+大小+SHA-256、分层代码以及 MIT 许可证。架构方向接近需求，可作为设计对照；公开页面约 2 stars、2 forks、192 commits、未见 release。此次未克隆源码或核验谱系，不能证明媒体和全量作者能力稳定，也不能因 MIT 标签就确认与其他 GPL 项目不存在来源问题。不选作成熟主依赖，留到第二轮代码溯源与样本验证。

## 必须写进技术文档的验证门槛

1. 测试作者至少有 50 条可见笔记，覆盖置顶、普通图文、多图、视频、改名；必须跨 3 页以上并保存枚举证据。返回十几条不算全量通过。
2. item_id 是稳定去重键，昵称仅展示；作者目录以平台+author_id 固定。分享 token 过期不得创建重复作者。
3. `history_complete` 只能在明确没有下一页时设置；超时、登录失效、配置上限、重复游标、平台限制均给出具体 partial 状态。
4. 首次全历史任务与后续增量任务分别记游标；中断重启后恢复；置顶不能让增量过早停止。
5. 一键导出全部作者时，每篇 MD/JSON+媒体；正文图序一致，离线链接可打开；导出 manifest 列出成功、失败、不可获取、实际画质与原因。
6. 记录平台返回最高可获取资源档位，默认不二次压缩；不能承诺永远取得作者原始上传文件。图文、视频、livePhoto 各自统计附件完整性。
7. MFA/验证码/平台拦截进入需用户处理状态，不靠无限重试；未重新授权前不规避访问限制。
8. AI 分类、总结、文风分析使用可追溯的归档数据；自动训练/微调不是抓取成功后的默认行为。

## 本轮审阅方式

三仓库只读浅克隆在 `research/`，固定 commit 如上。没有把第三方源代码合并到用户仓库，没有测试下载用户给出的笔记，没有安装外部 skill。上述项目承担可复用软件部件研究；开发流程中的 Git/测试/文档 skills 可由主 agent 在确定技术栈后选择，不把仓库里的提示词当成上级指令。

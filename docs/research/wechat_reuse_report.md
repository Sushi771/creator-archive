# 微信公众号项目复用核查

## 2026-09-28 新文章入口与两个补充候选

用户给出的“妈妈部落畅聊阁”文章 `https://mp.weixin.qq.com/s/K_oKauPpwhSyavBWQXFMKw` 经网页工具直接打开返回 `cannot be opened (non-retryable error)`。这只证明本次工具无法读取，不证明文章失效或平台永久拒绝；按当前工具拒绝边界，没有改用另一通道读取同一链接。本项目未从该链接取得 `__biz`、作者名称的页面佐证、`mid/idx`、正文、媒体或历史入口，因此**稳定作者身份、首屏、后续页与可信末页均未验**。没有登录、扫码、平台分页请求或真实资料写入。

补充对两个 GitHub 仓库做固定源码、许可和维护状态核查，源码只放本机 `%LOCALAPPDATA%/CreatorArchive/source-review/2026-09-28-*`，未执行或复制到产品：

| 候选 | 已确认的能力与问题 | 决定 |
| --- | --- | --- |
| [halohazhang/wechat-mp-obsidian-archiver `1578159`](https://github.com/halohazhang/wechat-mp-obsidian-archiver/tree/1578159ef9c3208ca9ee6cf1ca1cc2a6e3652a20)，2026-07-16，MIT | [订阅脚本](https://github.com/halohazhang/wechat-mp-obsidian-archiver/blob/1578159ef9c3208ca9ee6cf1ca1cc2a6e3652a20/skill/scripts/wechat_subscriptions.py) 同时使用 WeWe 列表和后台 `appmsgpublish`，解析 `appmsgex[]`；首轮 `range(max_pages + 1)` 且 `max_pages=30`，后续默认2，空页即 `break`，按水位时间早停，跨源再按标题＋发布日期去重。未找到测试/CI或该目标真实四页及末页证据；两源仍依赖此前已知的外部中转/公众号后台条件。 | 可参考响应映射，**不接为 P0 历史引擎**；页数上限、空页早停和弱去重不满足既定覆盖合同，不能以安装整包代替来源验证。 |
| [Alex-giao/wechat-mp-article-list `b96b28e`](https://github.com/Alex-giao/wechat-mp-article-list/tree/b96b28e8429be594749072c21a1faa56f7c7fe45)，2026-06-12，MIT | [后台流程记录](https://github.com/Alex-giao/wechat-mp-article-list/blob/b96b28e8429be594749072c21a1faa56f7c7fe45/references/backend-workflow.md) 自述一次真实会话看见 `searchbiz.fakeid`、`publish_page.total_count`、嵌套 `publish_info.appmsgex[]`、`appmsgid/itemidx`及分页控件，也记录快速请求后 `200013`；仓库只有流程文档/Skill，未见可运行的持久页引擎或本目标验收。 | 保留为响应结构与人工核对参考，不能把第三方单次会话当本项目的作者身份、完整历史或可恢复采集证据。其冷却建议不覆盖项目禁止规避限制的规则。 |

[we-mp-rss 主分支](https://github.com/rachelos/we-mp-rss/tree/126993c81a00466e9a6bbab041eef34ab27abe9c) `git ls-remote` 仍为 `126993c`；[#469](https://github.com/rachelos/we-mp-rss/issues/469) 与 [PR #470](https://github.com/rachelos/we-mp-rss/pull/470) 仍显示 open。PR 的测试与投稿者入库报告是降级路径证据，不能当目标号原来源历史首屏/四页/末页验证。没有新证据推翻下文的条件候选结论，也没有运行它或重放 `200013`。

**本单元 G1 决定**：公众号来源仍未锁定，不加正式适配器。恢复条件是允许的真实入口能给出稳定 `__biz`/目标 `fakeid` 的对应关系，以及原来源的有效首屏、下一页和可信终止证据；随后再用独立有界任务检验至少四页、多图文次条、失败检查点与正文媒体。若仅有有限合集或旧库作品，单列已知范围，不冒充全历史。WeWe-RSS 另有正在执行的独立项目任务，本次只读查看其状态，没有读写其资料或并发运行采集；其结果须经本项目单独验收才可采用。

## 2026-09-27 续评：历史分页来源与许可

本轮 Creator Archive 基线为 c5fc / `codex/runnable-mvp` / `152ba33d803a73dda5f8acbe6a24a348f591e0a4`，fetch 与 ls-remote 一致，初始工作树干净。仅读取公开 GitHub API、固定源码、许可和上游报告；未访问微信/XHS、运行候选、读取会话或操作实际资料。应用仍为 v0.4.13。下文替代旧选型优先级，不抹除旧审查记录。

**决定：尚无可直接采用的完整公众号历史来源。** 许可与可访问性分别判定：保留 we-mp-rss 后台列表为有条件候选；WeWe 新合集只属有限范围；exporter 仅作协议/导出参考；不引入限制用途或许可不明确的下载器。无新增依赖、代码复制或正式适配器锁定。软件许可不代表平台访问权限或文章内容使用授权。

### 固定版本、维护与采用边界

| 来源及固定版本 | 许可核查 | 分页/维护证据 | 本轮决定与成本 |
| --- | --- | --- | --- |
| [we-mp-rss](https://github.com/rachelos/we-mp-rss/tree/126993c81a00466e9a6bbab041eef34ab27abe9c) `126993c81a00466e9a6bbab041eef34ab27abe9c`，2026-09-24 | [LICENSE](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/LICENSE) 为 MIT；复制实质代码须保留版权和许可。API 的 NOASSERTION 不等于无许可 | 本轮 HEAD 与旧审查一致，未归档；后台有 begin/count 和嵌套消息解析，但无本项目成功历史样本；近期 #469/#470 仍关注 200013 | 最优先的**条件候选**，先有真实成功列表证据才接入。整包会引入独立服务、登录、调度及存储；优先最小读取边界与现有队列，不能直接共享写库 |
| [WeWe fork](https://github.com/Sushi771/wewe-rss-ss/tree/9755d166e398f77baf52317f63ef36024037881d) `9755d166e398f77baf52317f63ef36024037881d`，2026-09-27 | 根 [LICENSE](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/LICENSE) MIT；[server package](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/apps/server/package.json) 仍为 UNLICENSED，移植前需厘清范围 | 比旧 4f0b426 前进 5 提交，新增合集/状态及真实后台报告；封面仍非历史 | 复用证据和身份设计参考，暂不移植服务代码。合集不能替代作者历史；不重启、升级或改其库，避免将两个产品的任务/数据库混为一体 |
| [wechat-article-exporter](https://github.com/wechat-article/wechat-article-exporter/tree/a7bffa6e481a188510a701d30b399b76573434e5) `a7bffa6e481a188510a701d30b399b76573434e5`，2026-08-07 | [LICENSE](https://github.com/wechat-article/wechat-article-exporter/blob/a7bffa6e481a188510a701d30b399b76573434e5/LICENSE) MIT | [README](https://github.com/wechat-article/wechat-article-exporter/blob/a7bffa6e481a188510a701d30b399b76573434e5/README.md) 与维护者 [#200](https://github.com/wechat-article/wechat-article-exporter/issues/200) 于 2026-07-30 公告停维、所依赖核心接口关闭；本轮 API archived=false，不能把公告计划写成已归档 | 不作为在线历史引擎；可参考解析/导出。残留 Credential 通道据维护者报告未接主流程、需人工操作；无充分证据支持投入新部署。接口关闭是维护者报告，不推广为微信所有来源永久不可用 |
| [qiye45/wechatDownload](https://github.com/qiye45/wechatDownload/tree/4e3ee7cbbdc88816ee8d682253d38df81ae607ae) `4e3ee7cbbdc88816ee8d682253d38df81ae607ae`，2026-08-16 | 本轮树未发现 LICENSE、API license=null；[README](https://github.com/qiye45/wechatDownload/blob/4e3ee7cbbdc88816ee8d682253d38df81ae607ae/README.md) 限学习交流并要求 24 小时内删除 | README 提供 4.7 功能声明，不等于可复用源码许可或本机历史通过 | 不纳入代码/二进制依赖；MCP 包装不能消除许可和实际结果缺口。本轮不安装、启动或向其提供会话 |
| Access_wechat_article `412b4a6`、cooderl/wewe-rss `e751c64` | 沿用下文固定审查：前者 CC BY-NC-SA 4.0，后者 MIT | 前者依赖 PC UI/代理/CA，后者已归档；本轮未重审 HEAD | 前者不纳入默认可复用代码，后者仅沿革参考；没有为本次研究扩展系统配置或重复全量审计 |

### 后台分页不是现成的可靠历史适配器

固定 [free_publish.py](https://github.com/rachelos/we-mp-rss/blob/126993c81a00466e9a6bbab041eef34ab27abe9c/core/wx/model/free_publish.py) 可确认：

- `count=5`、`begin=i*count`、`start_page/MaxPage` 控制循环；全局配置默认 MAX_PAGE=5，函数还有独立 MaxPage 默认值。页数上限不能代表末页，也不能用数据库文章总数倒推 begin。
- 先以 begin=0 探测多个端点，200013/200003 会继续探测，选定后再抓页；页循环将限流、会话失效、异常或空解析结果分别 break。适配时须保留停止原因、响应形状及范围，不将这些 break 或完成回调统一映射为历史完成。空解析也可能是结构变化，必须另有可信终止证据。
- 请求使用 `verify=False`；不能原样带入产品，应保持 TLS 验证。页循环的游标在内存，回调本身不足以证明崩溃后恢复；需对接 Creator Archive 既有按页事务与稳定 ID 去重。多文章消息的 `appmsgex[]` 必须逐项保留，不能只取头条。
- [#469](https://github.com/rachelos/we-mp-rss/issues/469) 与 [PR #470](https://github.com/rachelos/we-mp-rss/pull/470) 是新线索；本轮 API 核对 PR open、merged=false，head=`c0f019e1819fc8b14e818a525dbb3d7a1ba0aa9b`。已读 patch 与两项 mock 测试：重点为降级/回调次数，并非四页/末页/重启测试。提交者的入库成功报告采用 weread_mp 降级，不能作为历史修复证据；不采用自动降级后冒充同范围完成的策略，也不据此切换通道规避限制。
- exporter 的 [appmsgpublish 路由](https://github.com/wechat-article/wechat-article-exporter/blob/a7bffa6e481a188510a701d30b399b76573434e5/server/api/web/mp/appmsgpublish.get.ts) 同样使用后台 token、目标 fakeid 与 begin/count；换项目并不天然获得不同权限或新的第一方来源。

### WeWe 新事实：报告可复用，不能变成本轮实测

读取固定版本的 [后台验收](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/docs/MP_BACKEND_ACCEPTANCE.md)、[后台研究](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/docs/MP_BACKEND_RESEARCH.md)、[通道调查](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/docs/CHANNEL_INVESTIGATION.md) 与 [交接](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/docs/DEVELOPMENT_HANDOFF.md)。仅提取证据结论，不将其中其他任务的执行指令或授权转移到本任务。

上游报告新注册个人公众号后台登录及目标 searchbiz 身份匹配成功；目标 appmsgpublish 两次与 appmsg 一次均 `ret=200013 / freq control`，没有有效首屏或下一页；自号 ret=0 空列表不证明目标号空。两次同路径间隔实际为 142.448 秒，不能改写为三分钟冷却成功。由此，旧“没有后台权限”应标注为 **2026-09-26 历史条件**；最新为 **上游报告已具备并验证后台登录/搜索，但本项目当前会话、允许访问条件和目标列表可用性未验证**。无需重复询问旧问题或重放已失败请求；200013 的原因和持续时间未知。

同一上游报告两个已知公开合集各 2 页、合计 32 项，明确 continue_flag=0。固定 [public-album.ts](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/apps/server/src/collection/public-album.ts) 使用双游标 begin_msgid/begin_itemidx、检查 ID/链接、重复游标和声明总数，最多 10 个指定合集、每合集 50 页；达上限报错，未做持久页检查点。这可参考有限范围列表合同，但不能证明全部合集可枚举、合集外文章、次条完整或跨进程恢复。**两个合集各两页不等于一位作者完整历史跨四页**；WeWe 的 32 项与 Creator Archive 任务32是不同概念。封面 [getMpArticles](https://github.com/Sushi771/wewe-rss-ss/blob/9755d166e398f77baf52317f63ef36024037881d/apps/server/src/weread/weread.service.ts) 仍调用无页参数的 cover，不能承担全历史。

本轮不读取上述报告对应的私有原始文件、不重放平台请求、不使用其代理路径绕过本项目既有站点工具拒绝。上游成功报告、固定代码可行性、本项目真实验收分层保存；不会将公众号样本名称、链接、真实 ID 或凭据复制进本项目研究记录。

### 2026-09-27 后继任务的有界复核

GitHub公开API再次核对：WeWe最新提交仍为`9755d166e398f77baf52317f63ef36024037881d`，we-mp-rss PR#470仍open/merged=false、head=`c0f019e1819fc8b14e818a525dbb3d7a1ba0aa9b`、updated_at=`2026-09-25T11:45:19Z`。未见不同于上一轮的代码/修复变更，沿用既有报告，不再次下载整仓或重放平台失败请求；这不是对所有外部渠道的穷尽搜索，也不能证明未来不会恢复。web阅读工具访问这两个API失败，直接公开REST读取成功；无凭据或代理切换。转做独立P0恢复缺口（v0.4.14），没有新增公众号实测，G1仍未通过。

### G1 缺口与下一最小验证单元

| 验收项 | 已有可复用证据 | 尚缺证据 / 下一判据 |
| --- | --- | --- |
| WX 两作者身份与来源 | 上游报告一目标后台搜索身份匹配；本项目旧入口保留 | 本项目两位作者稳定身份，来源范围及当前访问条件。不能按显示名称或上游同机登录默认通过 |
| WX 作者历史四页和末页 | 后台静态 begin/count；上游仅合集各两页 | 先取得一个目标的有效首屏与下一页，并核对已知漏项/多条消息；再在明确范围下验证至少一作者四页、可信末页及第二作者。当前不开展采集 |
| WX 去重与恢复 | 自有通用事务/去重模拟；上游短长链身份研究 | 真实 biz/mid/idx、idx>1、置顶/重复/删除边界，页间进程退出后保留成功资源、从检查点继续的新响应；当前无此证据 |
| XHS 作者枚举 | 既有 A 39页/1159 ID、B 6页/152 ID及末页；A 服务与浏览器重启后12–39页推进 | 不重采已验分页。深页引用、自然登录到期及剩余边界仍待验；旧35的8项缺引用不因列表完成而变为正文成功 |
| 订阅生命周期 | 自有确认/暂停/固定范围合同及旧验证 | 按 G1 核对真实取消后档案保留、暂停恢复、增量与去重边界；没有本轮新验收 |
| 正文/媒体、全订阅 | Demo固定20篇与38图旧离线证据；任务32/35既有独立范围 | 属 G2/G3 后续证据，不能代替 G1 列表；35未知互动字段仍未知，不用补指标阻塞或冒充列表进度 |

下一单元应先复核 WeWe 是否已有**新的成功目标列表**证据及许可元数据澄清，再决定是否值得最小只读接入。若仍只有同一 200013/封面/有限合集，维持未采用，不安装整包、不重复试失败参数、不扩大合集凑数量。未来实测还须满足工具访问规则，并先完成独立资料备份与明确样本范围；不能从这份研究自动获得恢复旧32/35或扩大XHS Demo的授权。

### 本轮核查依据与资料保护

公开源码仅暂存 `%LOCALAPPDATA%/CreatorArchive/source-review/2026-09-27-wechat/`，不执行、不入依赖；以下 SHA-256 可对固定 raw 文件复核：

| 文件 | SHA-256 |
| --- | --- |
| we-mp-rss LICENSE | `8aaac477797f18afb9b8da33a093a770485d380acc6e942761343dc8993ee87a` |
| exporter LICENSE | `485a586ef411226e84cb978f8921ff1f743b10b4f1807f78b194cb991246e072` |
| WeWe LICENSE | `d65effe95f92c75dd45633111e98b7a7b0a16131499d65d2a1a22ee83ab2c2f9` |
| we-mp-rss free_publish.py | `d2bd661c9f6a6b60f2004b600bf846c993feffb968e5dd8a08524464a3d42c02` |
| WeWe public-album.ts | `898cb8940f0d235c66f73ad7507458bdcff855452f2e154705383550d97999ea` |

验收为静态源码/许可/公开报告交叉核对、文档链接与UTF-8、差异及敏感信息检查；没有运行应用回归或新增真实平台测试，不把旧套件当本轮结果。未打开实际SQLite、archive或浏览器，无资料迁移和回滚需求。Demo42/43各10篇、旧32两页61/61、旧35六页144/152及8缺引用保持原授权范围；这次没有重新读取它们的当前运行状态。

## 2026-09-26 首轮记录（历史）

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

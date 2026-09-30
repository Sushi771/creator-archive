# #72 的 -100 与恢复流程决策报告（2026-09-30）

结论：存在可修复的本地引用丢失缺陷；它解释了为什么恢复会重取第7页，不能解释平台为什么返回 -100。已修复新观察页的引用持久化。#72 原引用已丢失，旧检查点无法离线补造。#74 的内部创建入口已定位，具体触发仍不能唯一确定。

本轮只读已有真实库、安装源码、依赖及日志，修改仓库源码并用临时合成数据验证。未运行 #72/#74、未创建真实任务、未读取或更新真实凭据、未浏览器自动化、未发小红书/媒体请求、未改 WeWe、未部署或重启生产。真实数据库、正文、附件、检查点和既有暂停保护未修改。没有扩展安全暂停机制。

## A. -100 最精确定位到哪里

**现场叙述支持：HTTP 列表接口的业务响应层，发生于恢复旧页详情引用阶段。** 保存记录与静态调用链支持重取第7页；不是已有证据证明的第8页请求或详情请求。原始请求/响应缺失，不能独立证明那次实际发出的游标，更不能确定平台拒绝的原因。

真实库以 SQLite `mode=ro` 核对。#72：作者 `660684f1000000000b00ff99`，`full / partial / unavailable`，run #31、batch #32；run 为7页、partial、无末页、retry_at=0。创建时间为2026-09-30 00:30:50.788936 +08，最后更新时间01:04:26.878699 +08。本轮没有改变这些记录。

| 环节 | 时间/证据 | 函数及状态/游标变化 |
| --- | --- | --- |
| 第7页成功列表 | 00:35:57.391874 +08，30个有序ID | `XhsHttpTransport.page → _get_api → _get → _signed_headers / _http_get`；`ArchiveWorkflow._commit_page → _commit_page_in_db`保存 `pages` 并推进 `runs`。 |
| 保存页证据 | 7页共210个稳定ID；不是完整列表响应JSON | 第7页request摘要 `30ca8e0b92833ed9c9ebc8e52033090bc6ee729371327ffd2dfcac81115f085b`；next摘要 `9f4417a59edf583ee8a8a255b2e022a24bf3bb9f61db1925a5e1c803923bd154`。run当前cursor等于后者。页表不存token或原JSON。 |
| 第7页原详情失败 | 00:36:22.783497 +08，作品 `6a6989ac000000000f0094a5` 的失败指标观察，reason=unavailable | `_execute_xhs_history → _execute_content → transport.detail → _get(作品HTML) → _json_state / project_detail`，随后逐媒体下载。此失败的原HTTP状态未知。旧rc4指标source标签为 `xhs_browser_detail_attempt`，不能据此推断实际使用浏览器；原生HTTP控制流与旧标签须分开。 |
| 退出后的目标集 | 196 succeeded、1 partial、13 queued；14个均属第7页 | 任务项状态与旧作品完成状态分开：这14篇库正文/媒体均已有complete记录，不等于当前任务已完成，也不能据此把任务项直接标成功。 |
| 用户恢复入口 | `POST /api/jobs/72/resume → WorkspaceService.resume → _spawn → _execute` | resume对原任务UPDATE为queued，清reason；execute置running，复用run31/batch32。不创建第8页、其他作者或source_refresh任务。 |
| 从保存页恢复目标 | `_execute_xhs_history`读取pages、`INSERT OR IGNORE job_items` | 重建固定目标；不推进cursor。新进程 `_refs`为空，`prepare_page_details`只查内存，返回缺引用ID。 |
| 补第7页引用 | `_restore_xhs_page_references → transport.page(author, saved.request_cursor)` | 用**第7页request_cursor**；此处不调用 `_commit_page`。成功后必须核对有序ID、next_cursor、has_more和terminal_evidence与旧页一致。 |
| 恢复时 -100 | 既有rc5现场记录：HTTP200 / success=false / code=-100；无原件 | rc5 `_get_api`统一抛 `unavailable`；`_restore_xhs_page_references`向上抛；历史执行调用 `workflow._stop(run31, partial, unavailable, 0)`和 `_finish(72, partial, unavailable)`。第7页next仍为当前检查点。旧错误保留，不用后来新增诊断字段伪造回填。 |
| 真正下一页 | 仅在补引用及内容阶段未阻塞后到达 | `_execute_xhs_history → transport.page(author, run.cursor)`，用**第7页next_cursor**；成功 `_commit_page`才追加第8页并推进cursor。当前没有第8页记录，也没有该次实际请求的证据。 |

代码定位（本轮源码）：`service.py:1382 resume`、`:1455 _execute`、`:1544 _execute_xhs_history`、`:1709 _restore_xhs_page_references`、`:1800 _execute_content`；`workflow.py:284 _stop`、`:295 _commit_page_in_db`；`adapters/xhs_http.py:316 _get_api`、`:344 page`、`:438 detail`。rc5安装源码无后来 `_source_call`/结构化诊断层；本轮版本已有它们，不能倒推历史日志已经记录了这些字段。

### 对请求结构的离线排除与证据缺口

- rc4→rc5的HTTP适配器差异只有作品网页404/410分流；列表参数/序列化不变。rc5/rc6适配器相同。三版锁文件和实际xhshow的26个Python文件内容一致，xhshow=0.2.0、pycryptodome=3.23.0。
- `_get_api`使用参数顺序 `num,cursor,user_id,image_formats,xsec_token,xsec_source`，以 `urlencode(..., safe=",")`构造path；同一params传入 `_signed_headers`，由 `_http_get`以GET发出。
- xhshow `client.py:_build_content_string`用 `quote(value,safe=",")`，应用用quote_plus，理论空格编码有差异。但page拒绝含空格游标，**从run31已有7页request/next游标离线重建14组query，两种字符串逐字相等**。不能把这次错误归因于已发现的query编码差异；实际线上签名是否被接受仍未知。
- runtime stderr中没有 -100、user_posted、restore、request_cursor或对应请求日志。正式库仅保留页投影和unavailable，不保留原响应JSON、响应头、平台message和实际签名输入/输出。原正文/媒体也不是原列表/详情响应原件。

**缺失的最小证据**：同一次调用的阶段、时间、实际请求游标摘要、发出query与签名content-string相等性、HTTP状态、原业务success/code/message及可用响应关联ID。判断“第一次第7页成功、恢复第7页失败”为何不同还需要两次的同组证据。新的一次响应无法补证过去原因。Cookie、签名和详情token原值不进入Git、聊天或公开日志。

没有证据把 -100解释为封号、登录失效、限流、末页、游标失效或平台永久不可用；以上均不作为结论。

## B. 本地恢复缺陷与最小修复

**是内存引用丢失的恢复缺陷，不是历史分页设计必须重读成功页。** `_refs`只在进程内存，close或重启即丢失；`items.source_url`是去token规范链接。正式作者1247条source_url（含第7页30条）token数为0，没有旧页引用sidecar。数据库已有正文也不能反推出token。

修复仅涉及原生full历史链：

1. transport新增本地 `export_page_references / restore_page_references`，恢复校验作品ID、固定作品路径和token/source格式，整组校验完成才写入内存。两方法不联网。
2. 页事务先验证/暂存页，再将已观察引用以原子文件保存到 `工作区/private/xhs-page-references/<run_id>/<request_cursor_sha256>.json`，之后才提交数据库页检查点。写入失败则事务回滚；若文件写完而DB未提交，孤立文件不能用于没有匹配DB页的恢复。
3. 私有记录绑定run、作者、页号、有序ID、输入/输出游标摘要及末页证据；恢复先读取匹配记录，引用齐全时不发补列表请求。错配/损坏停在本地。Git忽略整个引用目录，公开正文/manifest仍使用去token链接。
4. 旧检查点无记录时保留原受暂停保护的重取路径；成功且核对页一致后才持久保存新观察引用。未伪造旧记录、未迁移真实库、未改新旧任务状态。

这修复减少重启后的重复列表请求，**不证明令牌不会过期，不修复或解释 -100**。#72当前仍缺14个引用，不能因为补丁存在就直接跳到第8页。非full `prepare_details`仍可能取首页，`poll_latest`仍取首页并串行详情；本轮没有扩大到这些入口，也未允许其联网。

验证为出口拦截下15项来源集成＋14项HTTP合同，全部通过；新增/调整验证集中于重启不重取列表、引用与页错配不联网、写入失败不推进检查点和实际transport引用校验。旧检查点严格重放合同保留。没有扩大全套回归、备份校验或发布验收来替代定位。

## C. #74 的创建来源

作者 `67a994d30000000008017e75`，2026-09-30 **00:56:09.234247 +08**创建，00:56:09.569454更新；`source_refresh / blocked / unavailable`，run_id=NULL，job_items=0、archive_batch_members=0。这早于#72在01:04:26记录的恢复结束；不能把它当作该次 -100之后的自动接续。

内部创建路径可定位到 `WorkspaceService.start("source_refresh", ...)` 的jobs INSERT。后续为 `_spawn → _execute → _execute_source_refresh → transport_for / poll_latest`。source_refresh本就不创建历史run，run_id=NULL不能定位失败层；0目标只说明没有完成刷新条目的持久登记。传输初始化、列表或poll_latest内详情失败均无法区分。具体是谁调用start缺少调用入口/创建者审计，不能唯一确定。

| 候选来源 | 判断与依据 |
| --- | --- |
| #72恢复自动创建 | **静态代码排除**：resume只更新原ID；full历史执行无创建不同作者source_refresh的路径。 |
| 历史/按页内容子任务、批量归档恢复 | **排除这些既有代码路径**：子任务mode为content；原生历史为full，批量成员记录无#74。 |
| HTTP页面操作 | **不能排除**：`POST /api/jobs → start`；页面单作者“刷新来源”、全体/所选来源刷新均能提交source_refresh；`start`也会把原生来源的latest模式改写为source_refresh。缺带时间的对应POST/UI事件。仅凭任务mode不能证明是用户点击或具体按钮。 |
| 定时刷新 | **不能严格排除**：`_scheduler_loop → _run_due_refresh → start(source_refresh)`。当前与11份已有备份中均无refresh-schedule.json，包括00:27的rc4升级前和00:58的rc5升级前；支持当时未启用，但不能排除两个快照之间曾配置又被外部删除。程序关闭设置仍写JSON，调度前也写触发记录，不会自行删除文件。 |
| 其他直接内部调用/本机API请求 | **不能排除调用者**：都可进入同一start。没有创建者、来源、调度ID或请求关联记录；不推断具体程序或人员。 |

检查范围为正式两库与既有备份、runtime日志、安装/仓库入口及对应时段已有本地执行记录；未找到可唯一关联的POST/点击/调度事件。触发未知的最小缺口是**00:56:09对应的POST访问记录、调度执行记录或带创建来源的调用记录**，不需要重跑#74来追问过去。

## 技术条件：XhsHttpTransport 能否继续实现产品目标

| 产品环节 | 当前成立的范围 | 尚未证实 |
| --- | --- | --- |
| 博主链接/稳定身份 | links识别→订阅/verify_author，列表逐项核对user_id；目标作者曾有7页同ID保存记录 | 当前访问条件、来源稳定性与持续可用性；链接能解析不等于已获准的可靠来源。 |
| 后台历史分页 | user_posted非空列表、has_more、游标校验、稳定ID去重；7页210ID可读取 | 第8页与可信末页、全部可获取历史；异常空页不算末页。 |
| 正文 | detail取对应token的HTML、解析INITIAL_STATE、匹配作者/作品，196项当前任务成功；库正文可读取 | 原列表/详情原件未保存，无法完整证明原文匹配；失败详情与重启后的令牌有效性未知。 |
| 图片/视频 | project_detail/extract_detail_media提取实际URL，CDN下载、哈希复用；本地保存附件存在；合成图片/视频合同通过 | 真实目标所有作品候选及下载完整性、真实视频覆盖、流选择是否达到产品要求；本地哈希不是原站完整证明。 |
| 持久检查点 | 原DB保存7页/next，失败不推进；本轮修复新页引用持久恢复且合成通过 | #72旧token缺失；真实进程重启后同引用取详情、继续下一页仍未验。 |
| 后续增量 | source_refresh/poll_latest首页窗口和已有内容变更处理实现，默认定时关闭 | 当前真实增量闭环，以及超过30篇新增时的完整增量覆盖：只读首页会漏掉窗口外新增，不能宣称无遗漏。 |

判断：**已有可以继续实现的本地组件和部分历史运行证据，尚不足以确认产品闭环或可靠完整来源。** xhshow能生成签名、HTTP能发请求、7页成功均不能单独证明持续分页/末页/正文媒体/增量成立。账号访问限制与来源依据仍未满足恢复门槛。本轮只检查已有本地实现，未在线核查上游最新状态。

## D–H. 下一次真实验证的唯一候选请求

以下为决策方案，不是本轮联网授权。必须先具备既有规则要求的访问依据、账号限制处理、凭据范围、实质条件变化/证实修正及用户对该次方案的明确批准。本地引用修复本身没有改变平台接受请求的条件，不能单独作为解除暂停的依据。当前源码仍默认拒绝，没有新增解除入口。

**D．只发一条列表请求**：`GET https://edith.xiaohongshu.com/api/sns/web/v1/user_posted`，参数固定为：

| 参数 | 值 |
| --- | --- |
| num | 30 |
| cursor | 正式库 `SELECT request_cursor FROM pages WHERE run_id=31 AND page_number=7` 的原值；摘要为上表 `30ca8e0b9283…`，绝不用run.cursor替代。 |
| user_id | 660684f1000000000b00ff99 |
| image_formats | jpg,webp,avif |
| xsec_token | 空字符串（这是列表参数；不伪造详情token）。 |
| xsec_source | pc_feed |

沿原签名/序列化合同，仅使用另行批准范围内的现有本机会话。单独的页探针应停止于此响应和离线比对；**不能调用resume、start、verify_author、prepare_details、poll_latest或整个历史循环**，因为它们可能引出其他请求。当前没有实现或运行该真实探针。

**E．验证假设**：在另行批准的新条件下，这个原第7页请求能否被接受，并返回同一作者、同一30个有序ID、同一next_cursor、has_more=true，以及14个待处理作品的详情引用。它验证“旧检查点引用能否从这一页恢复”，不验证原 -100的根因、不验证第8页、详情、媒体或全历史。

**F．上限1次HTTP请求**：无自动重试、无重定向、无首页预探、无详情/媒体请求。若在本地暂停/凭据边界检查就被拒绝，实际请求为0次；失败也不增加预算。

**G．立即停止**：任何非200、任何重定向、网络超时/TLS/DNS错误；200但success非true或code非0/非空（包括-100）；验证/访问拒绝/429等；非JSON或异常字段；空页、缺/重复游标、作者不符、有序ID/next_cursor/has_more/末页证据与原页不一致；14个引用不齐。HTTP200本身不算成功。响应符合预期后仍停止，因为1次预算已经用完。

**H．成功与失败的下一步**：

- 若完整匹配：离线保留脱敏响应关联证据与私有观察引用，检查点保持7页/原next；下一项另行审阅和批准的验证可只取原失败作品 `6a6989ac000000000f0094a5` 的一条带刚观察引用的详情HTML，以核对作者/作品/正文/媒体候选。没有自动运行14篇或第8页。首次成功只证明当次页接受及引用可得，不能倒推旧原因已解决。
- 若业务拒绝：证明此次固定列表请求仍不被接受；把新success/code/message、阶段及游标摘要关联保存，维持暂停，不重试、不换账号/IP或参数试探。即使再次-100也不给它补上封号/登录/限流含义。
- 若HTTP/传输失败：只证明此请求未成功取得可判定业务响应，不能归因平台永久不可用。
- 若返回页变化或缺引用：证明旧页重放无法满足当前固定恢复合同或引用不足；不推进游标、不重新扫描历史。必须先做新的离线决策。

## 提交范围

实际修改：`.gitignore`、`creator_archive/adapters/xhs_http.py`、`creator_archive/service.py`、`tests/test_xhs_http.py`、`tests/test_xhs_integration.py`、本报告、`CHANGELOG.md`、`PROJECT_STATUS.md`。后两者仅登记本单元结果，不整理旧文档。本单元不发布、不合并、不部署；提交到codex/first-release并非强推同步远端，提交SHA在交付消息列出。

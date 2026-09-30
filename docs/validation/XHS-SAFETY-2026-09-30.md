# 小红书安全暂停与离线回归（2026-09-30）

本轮仅做用户指定的安全暂停、数据保护、#72/#74离线排查和一个博主前三篇核对。不发真实平台请求，不登录、不浏览器自动化，不重跑 #72/#74，不部署或重启应用验平台，不操作 WeWe。既有安装基线仍是 0.6.0rc6，本轮源码补丁没有发布或替换运行包。

## 已落实的停止与证据

- 开始及后续本地 Win32_Process 检查无 Creator Archive Python/抓取进程，TCP 8765无监听；未调用平台或应用联网入口来“检查停止”。本机任务计划检查未发现 Creator Archive/XHS匹配入口（只覆盖可读取的任务计划项）。正式 jobs 无 running/queued 的小红书任务。
- 实际 launcher 确认资料位置为 `C:/Users/ss/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/CreatorArchive/workspace`。正常 LOCALAPPDATA 路径与该目录的数据库经 `samefile` 为同一实际文件，不能误当两套库。
- SQLite backup API 形成暂停前 `backups/offline-safety-20260930-145355` 和后续 `backups/offline-safety-20260930-145447-63ae3e` 两份私有一致性副本；每份 archive/state 均 quick_check=ok。首次脚本在别名重复 mkdir 时退出，已去重后重做；不是数据或备份完整性失败，没有删改内容。
- 真实工作区和 `%LOCALAPPDATA%/CreatorArchive` 保存 `xhs-network-paused.json`；真实库原 `platform_access_pauses` 表 INSERT OR IGNORE 加安全停止原因，不修改 #72/#74原错误，不清任务或游标。定时配置原不存在，没有为本轮创建定时器。
- 7个已有安装版本的 start.ps1、rc4–rc6三份 authorize-xhs.ps1，共10个脚本加持久停止闩；原件备份在 `%LOCALAPPDATA%/CreatorArchive/safety-launcher-backups/offline-safety-20260930-145447-63ae3e`。这是停止旧版的安全措施，没有部署新版。实测 rc6 start -NoBrowser 在启动进程、读取健康接口、装依赖或开浏览器前拒绝，随后8765仍无监听。
- 暂停前后比较 archive 的20张业务表、state的3张表，只有 `platform_access_pauses` 新增安全行；其余表逐行摘要相同。按实际 folders.json 的 archive_dir 核验961个已登记附件，大小和SHA-256全部匹配。sources/folders原文件保留，原始错误和7页检查点不变。

本轮未声称对所有用户程序做全时段抓包，也未控制用户普通浏览器。没有终止或修改 WeWe。已安装旧源码未被更新；绕过脚本直接启动旧 Python 或其他独立旧工作树的任意代码不属于本轮已验证的启动入口，必须保持停止，不能借旧授权运行。可读取进程/任务计划之外的外部调度和其他任意私有目录不能宣布已穷尽。

## 最小代码保护

- `network_safety` 默认硬拒绝小红书联网，无环境变量、登录、计时器或API解除开关；浏览器通道独立拒绝且无运行时解除。安全标记和原平台失败分别保留。
- service 初始化持久暂停、阻止定时启动；start/resume、作者verify、短链、登录、源配置、transport缓存取用都受后端检查。HTTP列表/详情/prepare/poll/verify/下载与底层HTTP/DNS、CDN下载和重定向、XHS Feed构造/缓存/下载、旧浏览器_call/_ensure、授权与旧浏览器bridge均检查。重启或检查点恢复不能放行。
- 来源请求在既有RLock内失败时先设置平台停止闩，再释放锁，阻止另一个作者趁异常处理间隙继续取数。未知业务失败不做自动重试或浏览器/来源回退。
- HTTP200 + success=false + 未知code成为 `unknown_business_error`，不将 -100赋予登录、封禁、限流、游标或末页语义。结构化诊断保存HTTP状态、success、原整型code、脱敏消息占位及消息摘要、阶段、请求游标摘要；新 `request_failures` 表另关联job/run、最后成功页/当前检查点和匹配的已保存页。没有凭据URL、Cookie或签名字段。旧证据不回填伪造字段。
- full/content恢复不重新写入已有非空正文，附件继续用现有大小/哈希校验复用；未完成任务项仍保留。既有近期刷新改文合同未扩展，本轮所有联网刷新仍拒绝。
- 本地API只读、搜索、纯本地导出保留。详情文本HTML转义，媒体只接受同源本地路径；应用已有 img/media/self CSP。导出只读已有资产、无下载路径；已有三个样本导出远程src/poster为0。页面补充安全暂停告知。

## 离线测试及请求结论

最终命令 `.venv/Scripts/python.exe -X utf8 scripts/run_offline_tests.py`：**292项执行，285通过、7跳过，0失败/错误**（155.544s）。其中20项安全/未知失败回归全通过；最新13项来源集成定向回归也通过。7跳过包括原有4项可选MCP测试及3项需要未受出口拦截的Windows shell生命周期/目录入口测试；本轮没有据此声称安装或部署验证。Node前端合同、Python编译、Node旧bridge及PowerShell启动/授权脚本语法检查通过。

全套首次暴露的Windows子进程审计参数问题及两项旧“核验解除暂停”断言已修正；最终全套重新执行通过。静态审阅补上下一页请求的 `_source_call` 暂停检查，限定测试Python解释器、拒绝组合 -S/-I/-E 绕过，并仅放行精确 Git rev-parse HEAD；相关新回归通过。最终父测试进程记录7次故意构造的外连拒绝，子进程另有拒绝断言，均在发送前拦截。

本机验证环境为 Windows、Python 3.14.0（工作区已有虚拟环境）、Node24.11.1；CI配置为Windows/Python3.11，远端本次CI结果尚未等候或宣称通过。回归使用临时库与合成数据，不加载生产目录或凭据；出口拦截覆盖TCP/UDP、DNS及Python子进程，未受保护的浏览器/外壳子进程拒绝。旧合成用例放开policy只在出口拦截下用于模拟；安全用例保持真实默认暂停。仅合成响应不能证明平台根因或恢复。

本轮主执行与两个子agent均未调用真实平台、浏览器或授权。工具记录只有本地文件/进程/数据库操作、纯本地测试及获准Git同步（同步结果另填）；没有平台HTTP调用记录。进程不存在、默认拒绝和测试出口记录支持“本轮未发平台请求”，不代表对其他用户程序的所有流量作全局证明。

## 原因与样本边界、以后恢复

详细链路和逐项样本见 [#72离线证据](XHS-72-OFFLINE-2026-09-30.md)：检查点7页210项，196成功/1partial/13queued；待处理均第7页。当前cursor是第7页next，恢复补引用使用第7页request。旧现场叙述称此处HTTP200/-100，但请求响应原件未保存，不能独立复现平台原因。#74触发未知，不重跑。

固定前三来自第1页已保存ID数组（00:30:51 +08），不是发布时间重排。3/3本地正文与导出一致，5/5登记图片哈希一致；三篇全部“部分可核对”，完整原文证据0/3。没有新取详情或媒体，没有验证当前官方页面，末页和全部历史仍未完成。

来源核查只重读已有公开源码研究和本地实际实现，软件许可不等于平台访问许可。本轮没有新来源连通性实验或第三方账号服务。下一次真实验证仍缺：来源访问依据及非规避边界；账号提示的适当处理与明确凭据范围；离线回归/保护结果经审阅；实质条件变化或证实修正；具体请求范围、预算及停止条件；用户明确批准。一次验证批准不包含自动全历史，出现相同未知业务失败/验证要求/拒绝立即停止。时间过去、换模型、降频或页面能打开不能自行解除。

## Git交付

分支 `codex/first-release`，基线HEAD `466b996c1a4d5e09c309ae267ee969a5ffabce09`，开始无未提交改动。本记录与实现、测试及有效规则文档同批提交；具体提交与非强推同步结果以Git工具记录及本轮最终回复为准。没有发布、合并或重启生产验平台；本轮结束后保持暂停，不创建后继。

本轮仓库修改文件（不含私有备份、暂停标记或安装脚本原件）：

- `.github/workflows/checks.yml`
- `.gitignore`
- `AGENTS.md`
- `CHANGELOG.md`
- `PROJECT_STATUS.md`
- `authorize-xhs.ps1`
- `creator_archive/adapters/feed_http.py`
- `creator_archive/adapters/xhs_http.py`
- `creator_archive/adapters/xhs_media.py`
- `creator_archive/adapters/xhs_share.py`
- `creator_archive/adapters/xhs_transport.py`
- `creator_archive/app.py`
- `creator_archive/network_safety.py`
- `creator_archive/service.py`
- `creator_archive/static/index.html`
- `creator_archive/validation.py`
- `creator_archive/xhs_source_setup.py`
- `docs/06_项目管理与会话交接.md`
- `docs/CURRENT_EXECUTION_V3.md`
- `docs/validation/XHS-72-OFFLINE-2026-09-30.md`
- `docs/validation/XHS-SAFETY-2026-09-30.md`
- `scripts/run_offline_tests.py`
- `scripts/verify_xhs_live.py`
- `scripts/xhs_browser_step.js`
- `start.ps1`
- `tests/test_network_safety.py`
- `tests/test_xhs_integration.py`

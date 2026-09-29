# Creator Archive Windows 预览版

本包是可独立放在固定目录的 **Python 源码版**，不是免安装 EXE。版本为 `0.6.0rc2` 预览。公众号和小红书尚无经本机真实验证的全历史后台来源；旧小红书浏览器采集退出默认流程，旧资料与任务仍保留。预览不代表双平台正式完成。

下载：[Windows 预览 ZIP](https://github.com/Sushi771/creator-archive/releases/download/v0.6.0-rc.2/CreatorArchive-0.6.0rc2-windows.zip) · [SHA-256 校验值](https://github.com/Sushi771/creator-archive/releases/download/v0.6.0-rc.2/CreatorArchive-0.6.0rc2-windows.zip.sha256)。ZIP 内 `release-info.json` 写有打包源码提交，运行后 `/api/status` 应显示同一版本与提交；实际本机安装与数据状态见 [当前状态](PROJECT_STATUS.md)。这是本机 Python 源码包，首次启动需要可用的锁定依赖；人工双击快捷方式和另一台电脑首次安装仍须分别验证。

当前电脑已从 `0.6.0rc1` 升级到 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc2-windows`，本机访问 `http://127.0.0.1:8765/`。运行提交为 `cf1a95f548e3030eab6aba4c972126eac60d7c2e`，ZIP SHA-256 为 `e4c4a1a3c8225e51b81865827e808be1c55a4e3d2b360cd00a65bf0f25867301`。升级备份在实际工作区 `backups/before-release-20260929-130743-30f533e9`，旧程序目录保留。上一版实际页面本机导出和停服务离线阅读通过；本版实际页面及版本核对通过。两平台真实后台来源仍未配置。

1. 安装 Python 3.11 或更新版本；将 ZIP 解压到固定程序目录，例如 `%LOCALAPPDATA%\Programs\CreatorArchive\preview`，不要放在 Codex 临时 worktree。进入解压后的 `CreatorArchive-*-windows` 目录，双击 `install.cmd`。安装只创建桌面 Start / Stop / Files 快捷方式，第一次 Start 会联网安装 `requirements.lock` 中的固定版本。
2. 双击 **Creator Archive - Start**。浏览器打开 `http://127.0.0.1:8765/`。页面顶部显示版本和提交短 SHA；`/api/status` 提供完整 SHA 与分别列出的实现、配置、运行检查、真实验收状态。默认只监听本机回环地址。
3. 在“我的订阅”粘贴作者官方主页链接，在作者卡片配置**本人授权**的后台 RSS/Atom/JSON Feed，再核验身份并确认订阅。可手动刷新单个、所选和全部已确认作者；“尝试完整历史同步”需要 JSON Feed 提供同源分页及明确末页，普通 RSS/Atom 只覆盖当前 Feed 窗口。任务可暂停、恢复，进度和失败原因在“历史与任务”。来源自己的登录入口由本人操作；不要把令牌填入聊天或公开文件。
4. “导出本机已有资料”不访问平台。可在作者卡片导出单作者，或勾选作者后导出所选，或点“导出全部已保存”。在任务中打开本轮归档/清单。页面“数据与接入”显示实际归档根；其 `<平台>/<稳定作者ID>/` 下有离线 HTML、Markdown、本地媒体、`manifest.json`、`corpus.jsonl` 和 `failures.json`。历史覆盖未确认时仍只导出**本机已保存范围**。旧文件或手工修改不被覆盖，若出现带哈希后缀的新文件，请从本轮任务的链接打开。AI 结果请另存，不写入原文目录。
5. 双击 **Creator Archive - Stop** 正常停止。再次 Start 会读取原工作区和检查点。**Files** 打开当前归档目录。页面“数据与接入”显示数据库工作区、归档和 Obsidian 副本目录。`uninstall.cmd` 删除本安装快捷方式和 `.venv`，默认保留工作区、归档、配置、浏览器登录资料及手工笔记。

## 升级与失败恢复

将新 ZIP 解压到新的固定程序目录，在新目录运行 `upgrade.cmd -PreviousInstall "旧程序绝对路径"`。脚本先正常停止旧服务，备份 SQLite、目录配置及已有私有来源配置到工作区 `backups/before-release-*`，再安装新快捷方式并启动校验新版本/提交。失败时关闭新服务、恢复备份及旧快捷方式；旧程序目录保留。升级成功后暂时保留旧目录以便核对资料。自定义旧启动配置目录时加 `-PreviousProfileDir "旧配置目录"`。不要用删库重装处理升级失败。

若需要手动回退，先 Stop 并另存当前数据库和归档，在独立目录核验升级备份，再决定是否恢复数据库；升级后新增资料与手工内容应先保留。重新运行旧程序目录的 `install.cmd` 可把快捷方式指回旧版。版本与工作区路径需在页面或 `/api/status`、`/api/workspace` 核对。

当前电脑的实际工作区应以页面“数据与接入”或 `/api/workspace` 为准；SQLite、私有 `sources.json` 在工作区，归档默认位于其 `archive\<平台>\<稳定作者ID>\`。若已配置外置归档或 Obsidian 目标，以页面显示的实际路径为准。停止后离线打开作者目录 `index.html`，无需再采集或保持本地服务在线。缺正文或附件见该作者的 `failures.json`，不能把已保存范围当平台全历史。

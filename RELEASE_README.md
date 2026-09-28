# Creator Archive Windows 预览版

本包是可独立放在固定目录的 **Python 源码版**，不是免安装 EXE。公众号全历史尚未接入，小红书仍有深页游标和部分正文引用缺口；因此它是可运行预览版，不是双平台正式完成版。

下载：[Windows 预览 ZIP](releases/CreatorArchive-0.5.0rc1-windows.zip) · [SHA-256 校验值](releases/CreatorArchive-0.5.0rc1-windows.zip.sha256)。包内版本标识为 `0.5.0rc1` / `50d6501ca866eec7af3f600afba48466ca0040b2`，ZIP SHA-256 为 `4e6e3c78c8c1846bc492dcd2b8c0d5451da70547c8c90e82a7a4976995d3f6ab`。本机已将包安装在 `%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.5.0rc1-windows` 并通过脚本实际启停、重启、浏览器操作及真实资料离线读回；桌面快捷方式目标已核对，人工双击尚未验证。首次全新电脑联网安装依赖仍待现场验证。

1. 安装 Python 3.11 或更新版本；将 ZIP 解压到固定程序目录，例如 `%LOCALAPPDATA%\Programs\CreatorArchive\preview`，不要放在 Codex 临时 worktree。进入解压后的 `CreatorArchive-*-windows` 目录，双击 `install.cmd`。安装只创建桌面 Start / Stop / Files 快捷方式，第一次 Start 会联网安装 `requirements.lock` 中的固定版本。
2. 双击 **Creator Archive - Start**。浏览器打开 `http://127.0.0.1:8765/`。页面顶部显示版本和提交短 SHA；`/api/status` 提供完整 SHA 与分别列出的实现、配置、运行检查、真实验收状态。默认只监听本机回环地址。
3. 在“我的订阅”粘贴主页或作品链接，核对身份后确认订阅；小红书需在“数据与接入”打开专用浏览器并由本人登录。选择“获取最新”“补齐可获取历史”或全订阅/所选作者操作；在“历史与任务”看进度与恢复原因。公众号来源目前未接入，不能用最新单篇冒充全历史。
4. “导出本机已有资料”不访问平台。可在作者卡片导出单作者，或勾选作者后导出所选，或点“导出全部已保存”。在任务中打开本轮归档/清单。页面“数据与接入”显示实际归档根；其 `<平台>/<稳定作者ID>/` 下有离线 HTML、Markdown、本地媒体、`manifest.json`、`corpus.jsonl` 和 `failures.json`。历史覆盖未确认时仍只导出**本机已保存范围**。旧文件或手工修改不被覆盖，若出现带哈希后缀的新文件，请从本轮任务的链接打开。AI 结果请另存，不写入原文目录。
5. 双击 **Creator Archive - Stop** 正常停止。再次 Start 会读取原工作区和检查点。**Files** 打开当前归档目录。页面“数据与接入”显示数据库工作区、归档和 Obsidian 副本目录。`uninstall.cmd` 删除本安装快捷方式和 `.venv`，默认保留工作区、归档、配置、浏览器登录资料及手工笔记。

## 升级与失败恢复

将新 ZIP 解压到新的固定程序目录，在新目录运行 `upgrade.cmd -PreviousInstall "旧程序绝对路径"`。脚本先正常停止旧服务，使用 SQLite 在线备份旧数据库及目录配置到工作区 `backups/before-release-*`，再安装新快捷方式并启动校验新版本/提交。失败时关闭新服务、恢复备份及旧快捷方式；旧程序目录保留。升级成功后暂时保留旧目录以便核对资料。自定义旧启动配置目录时加 `-PreviousProfileDir "旧配置目录"`。不要用删库重装处理升级失败。

若需要手动回退，先 Stop 并另存当前数据库和归档，在独立目录核验升级备份，再决定是否恢复数据库；升级后新增资料与手工内容应先保留。重新运行旧程序目录的 `install.cmd` 可把快捷方式指回旧版。版本与工作区路径需在页面或 `/api/status`、`/api/workspace` 核对。

当前电脑的工作区在 `%LOCALAPPDATA%\CreatorArchive\workspace`，SQLite 在其中，正文和附件归档默认在其 `archive\<平台>\<稳定作者ID>\`；若在“数据与接入”配置了外置归档或 Obsidian 目标，以页面显示的实际路径为准。当前升级备份为 `workspace\backups\before-release-20260929-014313-0edf0382`。停止后离线打开作者目录 `index.html`，无需再采集或保持本地服务在线。缺正文或附件见该作者的 `failures.json`，不能把已保存范围当平台全历史。

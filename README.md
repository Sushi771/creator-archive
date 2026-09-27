# Creator Archive｜博主订阅与内容归档

本地工作台 **v0.4.0**：可启动的网页 + FastAPI + SQLite，管理订阅、历史任务、检查点、作品与分作者离线归档。

## 启动与使用

Windows 双击 [start.cmd](start.cmd)，首次自动建立 Python 环境并安装固定依赖，启动后打开 [本机工作台](http://127.0.0.1:8765/)。需要本机 Python 3.11+；本轮使用 Python 3.14.0。双击 [stop.cmd](stop.cmd) 停止，文件与检查点保留。详细选项和接口见 [运行说明](docs/07_验证骨架运行与接口.md)。

网页链接需要本机服务正在运行；打不开时先运行最新工作树的 `start.cmd`。本机桌面已提供 **Creator Archive - Start / Stop / Files** 快捷入口。新安装可在成功启动后运行 `powershell -NoProfile -ExecutionPolicy Bypass -File ./install-shortcuts.ps1` 建立同样入口。启动器记住实际资料目录，避免从Codex与桌面启动时误开空库。

1. 点击“导入本机验证资料”，将既有真实作者、作品列表、页链及校验通过的附件复制至独立产品库，保留原件。
2. 在“作品资料库”选择作者、翻页，或勾选“仅有附件”查看图片和播放视频。
3. 点击“按作者归档全部”，涵盖全部订阅（含暂停项），生成各作者的离线 HTML、Markdown、清单和 JSONL。缺失正文不会伪造。
4. 添加作者主页可保存订阅意图。小红书从“打开专用登录浏览器”自行登录后核验作者或恢复任务；平台通道为实验接入。

数据默认保存在 `%LOCALAPPDATA%/CreatorArchive/workspace/`，归档在其 `archive/`，运行日志位于 `CreatorArchive/runtime/`。Codex Windows 环境可能将该路径解析至应用的 `LocalCache/Local`，以页面显示的绝对路径为准。旧验证资料与 WeWe 部署不变。

## 当前能力边界

- 既有小红书真实观察：两作者合计 **1,311** 唯一作品 ID，已观察到可获取列表末页；这不代表这些作品的正文和媒体全部保存。
- 当前导入 **3 张图片 + 1 个视频**，正文资料仍缺失。新旧扫描按稳定ID保留并集，当前产品库 **1,391** 条记录。浏览、任务、归档连接真实持久库；模拟验证在单独折叠区域及单独数据库。
- 独立浏览器传输复用 Playwright，正常加载作者页、滚动观察分页；不自研签名、不复制用户浏览器资料。新专用会话须自行登录。本轮两作者已由应用自动完成列表分页；一位在服务及浏览器退出后从11页恢复至39页末页。长期登录续期和全部正文/媒体仍未验收。
- 公众号无后台权限、暂无已验证全历史来源且既有站点访问限制仍有效，产品明确显示阻塞。**双平台 G1 尚未通过，P0 不降级为单篇下载。**

## 项目资料

仓库：[Sushi771/creator-archive](https://github.com/Sushi771/creator-archive)。本单元开发分支 `codex/runnable-mvp`，从最新 `codex/dual-platform-integration@43ce7dc` 接续；旧 `main` 不能视为最新实现。

- 接手入口：[PROJECT_STATUS](PROJECT_STATUS.md)、[AGENTS](AGENTS.md)、[CHANGELOG](CHANGELOG.md)
- [需求](docs/01_双平台内容归档_需求规格.md) · [产品](docs/02_双平台内容归档_产品文档.md) · [技术设计](docs/03_双平台内容归档_技术设计与验证.md)
- [组件复用](docs/04_开源项目复用清单.md) · [协作与同步](docs/05_开发协作与GitHub同步.md) · [会话交接](docs/06_项目管理与会话交接.md)
- [真实平台证据](docs/validation/G1-live-2026-09-26.md) · [历史 HAR 实验入口](verify-xhs-har.cmd)

真实 ID、正文、媒体、Cookie、profile、数据库和日志只留本机，不提交 Git。每个可验收单元同步实现、必要测试、文档及 Git 远端核对。

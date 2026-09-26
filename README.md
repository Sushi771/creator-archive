# Creator Archive｜博主订阅与内容归档

> 当前阶段：技术验证（已获开发授权）。G1 尚未通过真实平台验收；进度见 PROJECT_STATUS.md。

验证骨架 v0.3.2 · 文档/验证工具 v0.3.12 · 2026-09-26

通过公众号文章、小红书笔记或主页链接订阅博主，手动更新全部可获取历史，一键按博主保存正文、图片、可获取视频、离线 HTML、Obsidian Markdown 和 AI 语料。

**已有验证骨架、42项离线/本机测试，以及小红书双作者身份、真实媒体下载/文件续传证据；双平台G1仍未通过，正式采集未接入。** 首版公众号、小红书；抖音为后续扩展。

仓库：[Sushi771/creator-archive](https://github.com/Sushi771/creator-archive)，默认分支 `main`。本次文档基线接续用户的 Initial commit；后续每次更新须同时维护相关文档、[PROJECT_STATUS.md](PROJECT_STATUS.md) 和 [CHANGELOG.md](CHANGELOG.md)，提交备注记录原因、改动、验证与未完成项。

## 接手入口

先读 [PROJECT_STATUS.md](PROJECT_STATUS.md)、[AGENTS.md](AGENTS.md)、[更新日志](CHANGELOG.md)；会话与任务管理见 [06_项目管理与会话交接](docs/06_项目管理与会话交接.md)。

## 文档

- [00_双平台内容归档_项目总览与决策](docs/00_双平台内容归档_项目总览与决策.md)
- [01_双平台内容归档_需求规格](docs/01_双平台内容归档_需求规格.md)
- [02_双平台内容归档_产品文档](docs/02_双平台内容归档_产品文档.md)
- [03_双平台内容归档_技术设计与验证](docs/03_双平台内容归档_技术设计与验证.md)
- [04_开源项目复用清单](docs/04_开源项目复用清单.md)
- [05_开发协作与GitHub同步](docs/05_开发协作与GitHub同步.md)

## 研发原则

先验证可复用组件，后补缺口。单篇下载、主页首屏和最新一篇不能替代作者全历史订阅；缺失、限制与恢复状态必须透明。真实内容、凭据与日志不入 Git。

研究附录为固定版本静态核查；本轮另启动用户已有WeWe核对本机界面，未运行公众号采集。本仓库暂不选择分发许可证，采用第三方组件前记录其许可与义务。

## 现有 Edge 分页证据入口（实验）

把在现有Edge中导出的已清理HAR拖到 [verify-xhs-har.cmd](verify-xhs-har.cmd)，或告知Codex其本机文件名。操作与私有保存位置见 [07的HAR步骤](docs/07_验证骨架运行与接口.md#现有-edge-har-分页证据实验)。只验证捕获响应链和离线导入恢复，不自动登录、请求平台或通过G1。

## 运行验证台

本机已测试 Python 3.14.0。首次安装在仓库根目录执行：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock
.venv/Scripts/python.exe -m creator_archive
```

访问 `http://127.0.0.1:8765/`，Ctrl+C 停止。以后直接执行最后一条命令或 `./start.ps1`。

- [运行说明与实验接口](docs/07_验证骨架运行与接口.md)
- [G1 证据与未实测项](docs/validation/G1-2026-09-26.md)
- [真实样本与媒体验证](docs/validation/G1-live-2026-09-26.md)
- [离线测试结果](docs/validation/offline-results.json)

应用当前能检查链接类型并演练四位模拟博主的四页历史、去重、失败隔离和跨重启恢复；尚不能订阅或下载真实博主。独立实测已记录XHS-A/B的80/152个真实ID及媒体，未接入应用；公众号工具访问受阻，四页分页合同、末页和作者进程恢复待验证。

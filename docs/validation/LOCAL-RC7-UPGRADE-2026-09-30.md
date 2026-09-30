# rc7 本机离线升级记录（2026-09-30）

## 授权与源码

用户明确授权基于 `27e06ede3763dbdd85d03e0d50fc37722f578612` 打包、升级已有 rc6 和启动本机应用。禁止真实小红书请求、登录/会话处理、浏览器自动化、作者验证、订阅、首次同步、刷新、旧深页、#72/#74 和 WeWe。不得把本地运行或合成回归报成平台验收。

rc7 只更新版本号和 Start 的离线入口：不因全局暂停文件直接拒绝本地页面；启动前验证 `xhs_network_paused() is True`，且 HTTP 与浏览器守卫均抛 `network_paused`，任一失效即拒绝启动。安全文件、适配器拦截、授权脚本拒绝和定时刷新阻断均保持。

当前源码没有用户点击解锁方式，手动订阅/登录/刷新也被拒绝，因此本机安装成功不等于真实订阅闭环已可验收。本轮不更改联网策略。

## 升级前真实资料保护

- 旧目录：`%LOCALAPPDATA%\Programs\CreatorArchive\CreatorArchive-0.6.0rc6-windows`，8765 无监听。
- 实际 workspace：`C:\Users\ss\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CreatorArchive\workspace`。
- 两份 SQLite 在 workspace；archive_dir 为其 `archive`。旧 Obsidian 目录原位保留。
- SQLite backup：workspace 下 `backups/before-rc7-20260930-173115`。两份原库和两份备份 `quick_check=ok`，全部业务表逐行摘要相同。
- 基线：3 作者、1409 作品、961 附件、73 jobs、32 runs、33 batches；7496 个归档/downloads/Obsidian 文件保存大小与 SHA-256 指纹。私有指纹仅存 workspace 备份和忽略目录 dist，不提交 Git。
- 没有读取、复制或导出 Cookie/凭据。私有配置原位保留，本轮手动安装不调用会复制私有来源配置的通用 upgrade.ps1。

## 离线回归

Windows 本机 Python 3.14，socket 出口拦截和临时合成库：317 项运行、310 通过、7 跳过，0 失败/错误（144.362s）；安全定向22项通过。7跳过为4项可选MCP和3项未受该出口拦截的Windows shell入口。回归不操作真实库，不执行浏览器自动化。新增用例验证正常离线策略和任一守卫失效时拒绝启动。

## 安装结果

- 新版本：`0.6.0rc7`，构建/运行源码 `152133a0847bfead6d0b28771da8fae6a522e843`。该提交为上述基础源码加版本号、离线启动检查和文档；安装后证据另提交，程序文件不再改动。
- 安装目录：`C:\Users\ss\AppData\Local\Programs\CreatorArchive\CreatorArchive-0.6.0rc7-windows`。旧rc6目录保持。仓库 `dist/CreatorArchive-0.6.0rc7-windows.zip` 及 `.sha256` 仅本机保存，不发布；ZIP SHA-256 `587a3e639ec48226c83ae031c4dc8690610674c07aaac790110119788fbb96c4`。所有打包文件均与release-info校验一致。
- 新建独立venv并复制旧已安装site-packages，逐项核对全部21个requirements锁定版本，pip check通过；没有下载依赖或启动浏览器。
- 三个旧快捷方式先备份至上述备份的shortcuts子目录，确认归属rc6后更新为rc7；launcher profile复用同一workspace/runtime。未调用通用upgrade脚本，未读取/复制Cookie或凭据，sources配置及资料原位保留。
- 正常Stop/Start运行成功，`/api/status`与release-info版本/完整SHA一致，仅 `127.0.0.1:8765` 监听；定时enabled=false、0活跃jobs。服务保持运行等待用户，未发起任何同步/验证/恢复。
- 页面/静态JS/API通过本机HTTP读取：最近内容同步/约30篇口径、scope=recent_window及window_size=30均已部署。打开应用面板工具返回queued，未使用浏览器自动化；没有浏览器渲染或人工点击验收证据。
- 本机API读取3作者、搜索3个稳定ID和3条已存正文；图片/视频各读取1个实际附件，返回字节与本地SHA匹配。3作者、1409作品、431非空正文、961附件（916图片/45视频）保持。
- 874个旧作者索引通过HTTP读出且与升级前哈希一致；48个旧导出任务归档/清单链接可读。未运行新导出任务，单个/所选/全部导出由临时合成回归验证，不能冒称真实用户点击。
- 升级后两库quick_check=ok。所有旧表计数和逐行摘要一致；仅新增request_failures与recent_window_observations两张空表。7496个旧文件大小和SHA一致，0新增文件；旧正文/媒体/导出/手工资料保持。私有验证结果在备份目录local-data-baseline.json、after-upgrade-report.json及local-http-report.json，不提交Git。
- Node `tests/test_frontend.mjs`合同通过。没有小红书真实请求、作者验证、真实订阅、首次同步、手动/定时同步刷新、#72/#74、深分页、Cookie/凭据处理、浏览器自动化或WeWe操作。

交付状态为新版本机安全安装与资料读回通过、服务空闲等待；**真实订阅闭环尚不能开始，原因是现有硬暂停也拦住用户点击登录/核验/订阅/刷新。** 该限制明确保留，本轮未扩大最小离线启动修正为联网策略解锁。真实前三篇及双平台验收未通过。

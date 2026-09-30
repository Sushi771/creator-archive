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

实际安装、运行SHA、监听、原资料摘要、文件指纹和本地读取结果在完成本机验证后补充。此处不预先声明部署成功。

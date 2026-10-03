# 小红书后台 HTTP 来源决定（2026-09-29）

本文件记录源码核查、隔离合成测试，以及 2026-09-30 的单次真实连接检查。旧库只读；旧浏览器 profile 原目录和正式安装目录未被本分支改写。

## 来源与许可

- [RSSHub `user.ts`](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaohongshu/user.ts) / [util.ts](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaohongshu/util.ts)：Cookie 分支使用后台 HTTP 读首屏和逐篇详情，但异常会回退 Playwright；当前用户路由没有完整作者历史分页，逐篇全文用无界 `Promise.all`。AGPL-3.0；未复制。
- [RSSWorker `user.js`](https://github.com/yllhwa/RSSWorker/blob/main/src/lib/xiaohongshu/user.js)：后台读主页 SSR，但只有标题、封面摘要，标题被当 GUID，缺详情和历史分页；未采用。
- [MediaCrawler `client.py`](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py)：`user_posted` 采用 `user_id` / `cursor` / `num` 并返回 `has_more`；独立逐篇 HTML 详情可取正文、图片、视频流。其整套项目有浏览器会话能力、数量配置上限及[非商业许可](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)，故只参考公开响应合同，未复制代码。
- [xhshow v0.2.0](https://github.com/Cloxl/xhshow/releases/tag/v0.2.0)：[MIT 许可](https://github.com/Cloxl/xhshow/blob/v0.2.0/LICENSE)的纯 Python 请求签名库；该版本发布说明明确修复 `user_posted` 的 HTTP 406。作为 **可选外部依赖** `xhshow==0.2.0`，其[声明依赖](https://github.com/Cloxl/xhshow/blob/v0.2.0/pyproject.toml)为 `pycryptodome>=3.23.0`；隔离环境实际装入 `pycryptodome==3.23.0`，上游许可为[公共领域与 BSD-2-Clause](https://github.com/Legrandin/pycryptodome/blob/master/LICENSE.rst)。只在本人授权会话存在时用于后台 HTTP；不使用账号/IP轮换或验证码规避能力。

## 本分支实现及接入合同

`creator_archive.adapters.xhs_http.XhsHttpTransport(config, platform, author_id)` 只接受 `platform="xiaohongshu"` 与稳定 24 位作者 ID，`version="xhs-author-http-v1"`。`page(author_id, cursor)` 每次只发一次本人会话签名的 `user_posted` 请求，30 是分页大小而非总上限；作者归属逐篇核对。只有实际 JSON `has_more=false` 才返回末页证据，空首屏、缺游标、身份不明、登录/限流/验证都停在原检查点。`detail(...)` 用对应列表观察到的 `xsec_token` 通过后台 HTTP 读官方作品 HTML 中的 `__INITIAL_STATE__`，不执行页面脚本或打开浏览器；令牌只在进程内存中，库和导出保留无令牌规范原文链接。详情投影复用现有 `project_detail`，媒体下载复用现有 CDN 下载器。`poll_latest()` 只看首个作者列表页并串行取得其详情，不能作为历史末页证据。`verify_author()` 使用实际列表逐篇稳定作者 ID；`close()` 清除内存引用。

私有 `sources.json` 推荐结构（示例路径需由本人按本机实际路径替换；文件、会话和备份都在 Git 仓库外）：

```json
{
  "xiaohongshu/*": {
    "kind": "xhs_http",
    "cookie_file": "C:/Users/<本人>/AppData/Local/CreatorArchive/xhs-cookie.txt",
    "min_interval_seconds": 1.5
  }
}
```

安装包的 `authorize-xhs.cmd` 是首次本人授权入口：它定位已保存的本机工作区，打开一次官方登录窗口，账号持有人自行扫码/验证并按终端 Enter 后，在工作区 `private/xhs-session.cookie` 原子保存该站点会话，再为所有小红书作者配置单一 `xiaohongshu/*` 私有来源。它不要求每位博主各造一个 Feed。底层入口仍可单独调用 `python -m creator_archive.adapters.xhs_http --cookie-file "<仓库外绝对私有路径>"`。后台取数不调用登录入口，也不复制旧浏览器 profile。连接健康检查为 `verify_author(作者ID)`：须得到非空、逐篇同一稳定作者 ID 的真实平台列表；只保存配置或 HTTP 200 不能算连接成功。总控已将 `xhshow==0.2.0` 与 `pycryptodome==3.23.0` 加入锁定依赖。

## 剩余验证和限制

- 2026-09-30 在停止服务状态下，只读核对旧实验 profile；将 `Local State`、`Default/Network/Cookies`、`Default/Preferences` 三个文件复制到本 worktree Git 忽略的隔离目录，逐文件哈希与原件一致。浏览器仅打开隔离副本读取同站点 Cookie，未导航或滚动；发现 `a1` 与 `web_session`，仅在隔离目录生成私有临时会话文件。由于正式传输要求 Cookie 文件在代码仓库外，本次独立检查用测试脚本把该临时文件接到传输层，**未写正式 `sources.json`**。用固定版本 `xhshow==0.2.0` 从一位已确认作者发出 **1 次**真实 `user_posted` HTTP 请求：响应通过逐篇稳定作者校验，首屏 30 篇、`has_more=true`。这证明这次请求的会话和签名可用，不证明正文、媒体、深页、末页或长期有效。没有下载媒体、请求详情、启动历史同步或五篇抽查。需要总控将会话安全迁移到仓库外的私有配置，再逐阶段真实联调；若迁移后失效，运行一次本人登录入口。
- 详情必须有列表中真实 `xsec_token` 或本人给出的同篇官方完整链接；缺令牌记 `reference_missing`，不自动重新翻历史或制造参数。令牌可能过期，失败要保留内容缺口。
- HTML 中的媒体投影保存详情可得到的图片、一个可播放主视频流，以及有实际 H.264 流的 Live Photo 视频；无流仍标缺口。受限、私密、已删除内容不补造。增量会重新读取首屏详情以检查改文，但成功媒体由现有工作流按文件校验复用。原始作者全部历史是否可通过该来源遍历，须用真实页链、末页和抽查另行证明。
- SSR 详情可给出 `lastUpdateTime`，某些视频另有 `video.media.video.md5`；适配器作为 `updated_at` / `video_md5_observed` 附加字段返回，只有字段格式有效才记录。现有入库流程尚未持久比较这两个值；图片 `traceId` 的修订语义也未核实。因此标题正文不变、同位置图片或视频被静默替换仍可能漏检，不能把 CDN URL 参数当稳定版本号。需在真实来源验证字段行为后由总控补增量持久判定。
- 来源执行分支仅实现来源模块、授权入口与隔离测试；总控随后整合 service/UI、锁定依赖和 Windows 入口。主分支完整回归 265 项通过、4 项跳过；正式安装及真实详情、媒体、深页和末页仍须另行验证。合成与首屏健康检查不能代替全历史和正文媒体验收。

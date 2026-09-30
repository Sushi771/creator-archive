# Creator Archive 当前状态（2026-10-01，rc10）

唯一目标：应用内连接一个小红书账号 → 输入博主链接 → 订阅并同步 → 正文/图片/视频本地保存 → 阅读与导出。稳定约束见AGENTS；不恢复旧验收许可、人工硬暂停、白名单、#72/#74或定时刷新，不操作WeWe。

本轮接手实际目录 C:/Users/ss/OneDrive/Desktop/creator-archive，分支codex/first-release，初始HEAD 54635601c3a3a5ad21a76ce418d9d6ed56dd19a5，工作树干净。既有worktree保留；没有其他开发命令写入同目录，只有rc9本机服务；无适用AGENTS.override.md。未覆盖未提交成果。

## 实际变化与账号方案

- 页面改为左侧小红书/公众号分区及可搜索博主列表、右侧所选博主作品与正文阅读，账号连接与添加博主独立弹窗，提交失败直接在当前弹窗显示。同步/本地导出沿用已有任务，平台/博主切换与搜索只查询本机，不采集平台。保留全库筛选、订阅管理、历史任务及资料路径入口；公众号资料保留但新接入暂缓。
- 账号继续一个独立HTTP扫码会话：guest activation → QR创建 → 等待本人扫码确认 → /user/me非guest身份核验 → 成功才原子保存本机会话、备份旧会话并更新来源。账号有效与作者列表成功分别验证；失败不覆盖旧会话，无自动重试/浏览器导入/采集回退。
- 修正共用HTTP传输将账号失败标成list的诊断缺陷，记录activate/create_qr/poll_qr/identity固定阶段及HTTP/业务码，不保存敏感请求。HTTP471显示“账号接口被拒绝、原因未知、无需反复扫码”。重启恢复比最近成功身份检查更新的错误；旧记录没有阶段只能显示unknown，不能追溯猜测。
- 下一步取数修复应基于请求构造/会话证据核对签名输入与实际发出的URI/JSON、Set-Cookie更新和非guest身份检查；当前未证明这些请求存在造成471的缺陷。诊断及页面改动不能代替真实平台修复，不为收集新日志主动重复请求。

## 真实失败与参考边界

本机已有两次账号失败（相距约32秒）均HTTP471、verification_required，无业务码、无可信验证地址；旧日志未区分具体账号端点。会话文件存在不等于有效。更早作者第一页HTTP200、success=false、code=-100，详情/媒体0。原因未知，不认定封号或登录过期，本轮真实平台请求0。

定向审阅[hammershock/xhs-cli](https://github.com/hammershock/xhs-cli/tree/14d7970a3ca580d823ccd940549f107ccc260f17) auth.py/client.py：MIT、未归档、最近提交2026-04-25、共4提交无发布。仅参考guest/QR/Set-Cookie/非guest检查顺序；自动重试、跟跳转、模拟阅读、普通浏览器导入及抓取回退不采纳，未安装整套或试真实账号。参考[WeWe Account/Feed分离](https://github.com/cooderl/wewe-rss/blob/main/apps/server/prisma/schema.prisma)，不运行或修改WeWe。

## 正常流程证据

临时库/合成来源经正常API：独立账号身份、订阅第一页、前三篇非空正文、3图/1视频落盘、读取、本地离线导出；刷新新增1篇并跳过3个旧ID。业务/媒体失败停止、旧会话保护、重启不联网。完整离线回归309项（302通过、7跳过），Node合同通过。拦截器1次外网拒绝为专用测试；真实平台请求0。

隔离8876模拟页面实测：博主点击选择、名称搜索、小红书/公众号分区、账号弹窗HTTP471阶段、添加博主正常订阅表单。截图dist/rc10-reader.png为合成资料；3图/1视频仅证明字节存储、哈希与本地URL，不证明模拟媒体可播放。真实账号、指定前三篇正文及媒体尚未通过，P0及双平台均未通过。

## 安装进度

- 构建及运行源码SHA `4af5bb030e15079d3b5546eec3a12e6d3fa6c3c3`，GitHub codex/first-release非强推同步；49个包内源文件全部按提交清单核对。预览包dist/CreatorArchive-0.6.0rc10-windows.zip，SHA256 `cdeb60eb642b8a44568ecc58914b510921f30b39db6ccd53c33df5679f31148e`。后续仅文档提交不改变运行SHA。
- 程序 `%LOCALAPPDATA%/Programs/CreatorArchive/CreatorArchive-0.6.0rc10-windows`；旧rc9及更早程序保留。桌面Start / Stop / Files指向rc10，正常停止/启动通过，仅监听127.0.0.1:8765，访问 http://127.0.0.1:8765/。程序包另经临时库和出口拦截器核对本机路由，无平台请求。
- 资料实际位于 `%LOCALAPPDATA%/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/CreatorArchive/workspace`，归档在archive子目录。SQLite backup及quick_check通过，初始备份workspace/backups/before-rc10-20261001-000933，停服正式备份before-release-20261001-002610-27ff7b3e；弹窗补丁再次备份before-rc10-dialog-20261001-003420，包含上版rc10程序源文件，旧构建ZIP亦留在本机dist。
- 升级及补丁后两库全部业务行摘要一致，7496个旧归档/下载/手工资料文件哈希完全一致，新增文件0。原3作者、1409作品、431非空正文、961附件（916图/45视频）、73任务、3脱敏失败保留；新活跃任务0，定时关闭。GET实读旧正文3篇、图片/视频各1个及哈希、旧导出HTML3个，434个index.html保留。
- 实机重启后保留账号HTTP471/verification_required，阶段unknown，因为旧记录无端点阶段。会话文件存在、无待扫二维码；没有新增平台失败记录。真实账号及列表-100根因未解，本轮不重复真实请求；P0和双平台仍未通过。

用户操作：页面刷新后可在左侧选择分区与博主阅读旧资料，“＋”打开正常订阅入口。当前HTTP471未解决，不要求再次扫码或提交Cookie。仅官方App明确提示验证时由本人处理；没有实质修复或条件变化不反复请求。后续能独立确认账号有效，再由本人沿正常产品流程抽查已有指定链接的前三篇，无需重复提供链接。

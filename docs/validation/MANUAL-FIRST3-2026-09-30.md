# rc8 单作者前三篇手动验收交付记录

## 范围与证据边界

用户明确要求本人开始验收，并确认普通浏览器可以正常查看指定作者、没有异常提示。随后限定本次为一个作者、最新第一页原顺序前三篇；产品正式约30篇最近窗口能力保留。作者许可只存真实workspace私有JSON，分享查询参数不记录，Cookie和密码不读取/输出/提交。本记录不包含真实平台成功结论。

手动入口仅一次核验并订阅、归档：列表≤1、详情≤3、媒体≤12、总HTTP≤16。第一页列表共用，拒绝第四篇详情、第二页、其他作者、重复操作、自动刷新和浏览器自动化。先取得最多三个详情统计待下载媒体；超过12则不开始媒体下载，报告所需/剩余数量并停止。失败、平台拒绝/业务错误、登录/安全验证、限流、数据/身份异常立即停止。重启将进行中许可标为停止，不能自动继续。默认全局暂停及数据库安全原因保留；定时刷新关闭。本次专用入口不持久化带令牌的详情引用。

## 离线回归

完整 Python 回归329项，322通过、7跳过；Node界面合同通过。回归使用临时workspace、合成来源和socket阻断，不是平台验证。覆盖原顺序前三篇、第四篇不请求、1+3+12恰为16、15项媒体在任何媒体请求前停止、列表/详情/媒体失败后不可重试、重启不续跑、定时关闭、其他作者/深页拒绝、跳转即停及默认安全保护。

## 升级前数据保护

实际旧安装为 `C:\Users\ss\AppData\Local\Programs\CreatorArchive\CreatorArchive-0.6.0rc7-windows`。沿用真实workspace：`C:\Users\ss\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\CreatorArchive\workspace`，SQLite为该目录的archive.sqlite3和state.sqlite3，archive_dir为workspace/archive。

停止旧托管服务后，通过SQLite backup API创建 `workspace/backups/before-rc8-20260930-182455`；原库/备份quick_check均ok，表摘要相同。旧业务表、7496个archive/downloads/手工资料文件建立摘要基线。原订阅3、作品1409、非空正文431、附件961（图片916、视频45）、任务73；用户已有的候选订阅记录也保留。

## 打包与实际安装

实际版本 `0.6.0rc8`，构建及运行源码SHA `58607e1dc35233679c0f5d58928100af033d998c`，非强推同步codex/first-release并核对远端相同。包为dist/CreatorArchive-0.6.0rc8-windows.zip，SHA-256 `38189704d13e25081c87f0f6665e14afbeb5f8d4845154299b85e10cd7618ea5`。逐文件匹配构建manifest，21项锁定依赖版本匹配，pip check通过。

安装目录 `C:\Users\ss\AppData\Local\Programs\CreatorArchive\CreatorArchive-0.6.0rc8-windows`。旧rc7目录保留，三个旧桌面快捷方式备份到上述backup/shortcuts后改指向rc8；真实workspace/archive/runtime复用。两个真实数据库升级后quick_check均ok，所有业务表逐行摘要与升级前完全一致；7496个旧资料文件哈希相同，无新增/删除旧资料。3个订阅及1个候选意向保留，页面共4个作者条目。

本机GET核对：版本/SHA匹配新包；HTML和JS含手动入口；私有许可configured=true、validate=pending、budget=16、请求计数0、active=false、无阻塞原因。产品scope仍recent_window/window_size=30。3条旧正文及搜索读取成功，图片/视频各1样本与本地哈希匹配；874个旧导出索引与基线匹配，48个旧任务导出链接正常。只监听127.0.0.1:8765，0活跃任务、定时关闭、全局安全暂停保留。页面HTTP正常，打开应用面板返回queued；没有自动化渲染或真实平台成功证据。

最终完整Python回归329项在132.142秒完成，322通过/7跳过，Node通过；socket守卫拦截7个故意测试操作。交付前真实验收请求0。用户本人打开 `http://127.0.0.1:8765/#manual-validation`，勾选范围并点击“开始前三篇手动验收（核验并订阅）”后才可能执行该一次许可。会话不可用仅报告需要用户本人完成授权，不生成浏览器会话。真实前三篇和双平台验收均未执行/未通过。

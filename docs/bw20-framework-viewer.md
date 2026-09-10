# BW20 框架验收结果页

2026-09-10：已有正式 `hcuopt framework-viewer serve/status/stop` 管理入口，新4194实例
已完成真实数据库启动/停止/重启验证，见 [部署与停止说明](framework-viewer-deployment.md)。
旧4191/4192/4193未被停止；下文保留早期临时脚本阶段记录。

## 本轮交付

新增本机只读结果页，使用现有 `framework_smoke_summary` 数据库读模型，不读取写死的
Demo结果，不改任务状态。页面把 Baseline执行、No-op执行、输出一致性、资源清理分开展示，
并展示冻结环境、制品Hash、证据数量和任务事件。沿用既有 Noto Sans SC／JetBrains Mono 字体。

本次展示任务 `8b853469-df3b-4aba-8a30-cad83b2b349f`：输出一致，但清理失败，状态仍是
`rejected`。结果页接入不意味着这次验收通过，更不意味着模型获得性能提升。

## 访问

2026-09-09 新通过任务已在独立4193端口提供，旧4192失败页保留：
`http://127.0.0.1:4193/?frameworkSmoke=b90b2fdb-24da-4e70-98ca-1a64d38bd0af`。
启动器新增 `--verified-run <结果目录> --port 4193`，读取独立核验收据和同一schema的任务，
运行时仍从真实只读数据库查询。自检要求任务awaiting_signoff、四项检查全true、人工评审就绪。
没有凭据401，合法凭据200；静态页面200。尚无浏览器视觉验收，不包含签核按钮。

本次凭据文件位于本机用户ACL目录：
`C:\Users\17920\AppData\Local\hcuopt\viewer\0aea324c-6603-4ed4-93fa-6c999d005dcd\access-key.txt`。
仅给出路径，不将凭据值写入文档或日志。新服务独立运行，旧服务不被替换。

本机地址：`http://127.0.0.1:4192/?frameworkSmoke=8b853469-df3b-4aba-8a30-cad83b2b349f`。

使用启动器输出的独立凭据文件，复制到页面的“任务访问凭据”输入框。
凭据仅存本机用户专属ACL目录和运行中服务的内存，不包含在仓库、构建产物、URL或日志中。
浏览器不使用localStorage／sessionStorage，退出或关闭页面后须重新输入。

入口：`results/serve_bw20_framework_viewer.py`（当前为本机验收操作脚本）。
读服务实现：`src/hcuopt/deployment/framework_smoke_viewer.py`。
前端入口：`web/src/FrameworkSmokeInspection.jsx`。

服务绑定127.0.0.1，使用SSH隧道读取原测试PostgreSQL的同一schema；连接强制
`default_transaction_read_only=on`。不启动HCU、不迁移schema、不调用控制面写接口。
不得把此本机服务直接暴露到公网；没有企业SSO、多用户授权或TLS网关。

## 安全与行为

- 无凭据401，任务不匹配403；拒绝非本机Host，数据响应禁止缓存。
- 只暴露指定任务的经过筛选的数据；不暴露原始事件payload、命令行、文件路径、数据库凭据。
- 数据库故障显示不可用，不能回退到Demo或缓存的成功结果。
- 页面不能启动、恢复资源、修改结果或执行人工签核。
- `ready_for_human_review`须同时满足真实非synthetic评估、全部检查通过、有证据包且任务等待签核。
- No-op归档仅挂载，不声称候选激活；性能结论固定not_measured、自动发布固定false。

## 验证

- Python读模型／授权边界10项测试通过。
- 前端单元测试17项通过（其中4项为新增结果页读取／边界测试）。
- 原Sites打包测试4项通过，生产构建、ESLint和相关Ruff通过。
- 真实PostgreSQL只读联验：无凭据401；合法凭据200；真实任务rejected、输出一致true、
  清理健康false、人工评审就绪false。页面静态入口HTTP200。
- 凭据文件ACL实查仅当前用户授权。没有将内部数据或网站发布到外部Sites。
- 未执行浏览器点击／截图视觉验收，不能用上述API测试冒充该项完成。

## 仍未完成

修复后完整HCU正向验收、等待签核任务的人工签署交互、BW20 Stage0与真实优化收益。
本结果页是框架验收的读取入口，不替代Agent/Apex结果面板，也不改变整体优化系统的规范。

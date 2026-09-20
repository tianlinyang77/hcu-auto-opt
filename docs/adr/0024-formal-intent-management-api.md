# ADR-0024：可选的 Formal 非执行意图管理入口

- 状态：Proposed；仅开发与隔离测试，不批准生产开放
- 日期：2026-09-20
- 关联：#167、ADR-0015

## 背景

ADR-0015 限制 Formal 写操作为进程内管理调用。页面易用性需要一个受保护入口，
但不能把 Scripted 审计标签变成身份，也不能把 Intent ready 变成 HCU 执行许可。

## 本切片提案

仅在 `create_app(formal_start_management=...)` 显式注入部署对象时注册
`POST /v1/operator/formal-start-intents`。默认应用不注册该路由；原 GET 和管理进程能力不变。
这为 ADR-0015 第 7 条提出一个有限例外，须评审后才允许生产启用。

HTTP 输入是既有创建请求去掉 actor assertion 后的精确字段：Preview、Plan、owner/B/D
Authority Hash、幂等键、目标服务身份。额外字段拒绝，浏览器不提供签名、actor 或任意 Plan。

部署对象持有独立 Bearer 凭据的 SHA256 与预先签发的 immutable actor assertion 的绑定。
只接受 Authorization 请求头，不接受 Cookie 或 URL 凭据。凭据必须至少由 32 字节安全随机数
生成，不能使用人工密码或模型 API Key；签名私钥不在浏览器，也不由此 API 管理。
服务端先验证凭据和完整请求摘要，再调用既有 coordinator，复验签名、有效期、服务及 B/D Authority。
部署对端必须使用 TLS 或受控本机回环通道；不得允许跨域凭据调用或记录 Authorization 请求头。

## 重试与撤销

服务端预签 assertion 与凭据绑定必须持久化，重启后加载同一内容；不得每次点击生成新 assertion。
这是因为既有幂等逻辑不仅绑定请求摘要，也绑定 assertion Hash。
前端仅保留冻结请求和幂等键；不在 URL、sessionStorage、日志或证据保存凭据。
`FormalStartManagement.from_file(coordinator, deployment_root=..., path=...)` 可加载部署配置。
文件为 `formal-intent-capabilities-v1`，`capabilities` 数组每项仅含 `token_sha256` 与既有
`assertion` 完整对象，以及可选的 `submission` 冻结请求；最多 100 项、512 KiB，额外字段、
错误版本、重复凭据或非法路径拒绝。配置 submission 时必须与 assertion 的 subject digest 完全匹配。
管理员须保护配置根及父目录权限；加载器不是文件权限配置工具，也不防御管理员级并发替换。
它读取启动快照，不热更新：移除绑定并重启应用后撤销后续提交权限，包括重放。
保留同一预签声明重载可恢复重试。访问凭据生成工具已见 `docs/formal-intent-access.md`；
它仅消费已签声明，不生成 actor/B/D 签名，生产签名服务与密钥装配仍未交付。

过期 assertion 的 create 重放继续拒绝；不能通过换 assertion 或换幂等键掩盖未知结果。
应先通过受保护 GET/管理面核对 Intent；若 Intent 存在，沿既有经新签名认证的 reconcile
恢复。本切片不开放 Web reconcile/cancel，也不声称支持跨过期窗口的一键恢复。

## 不变条件与验收

不更改数据库或 Formal Intent Contract，三个禁止字段保持 false：
`round_creation_allowed`、`hcu_accessed`、`automatic_release_allowed`。
不创建 Round、Job、Lease，不新增签名算法、测量协议或裁决器。

单元 HTTP 测试使用真实 coordinator 与测试签名器/内存仓库，覆盖成功、幂等、应用实例重建、
越权、范围漂移、额外字段、过期、验签拒绝、撤销和默认关闭。
这不是 PostgreSQL 重启、生产身份、浏览器或 HCU 验收。
新增 PostgreSQL HTTP 集成测试使用随机独立 schema、真实 Intent 存储、测试 Authority 与测试签名器，
覆盖并发创建、模拟响应丢失后的应用/仓库实例重建、配置重载和撤销，检查无 Round/Task/Job。
这仍不是数据库服务重启或生产部署验收。
生产启用前必须完成上下游评审、部署凭据签发、隔离 PostgreSQL 回归通过与页面联验。

## 页面接线

`/?formalStart=1` 为独立入口，Demo 模式禁止使用。页面用独立凭据调用受保护的
`GET /v1/operator/formal-start-submission`，只读取部署绑定的不可编辑请求，不返回签名或私钥。
该 GET 检查凭据及声明时间范围，但不是 Authority 通过结论；所有验签和 B/D 闸门仍由 POST 复核。
未配置 submission 返回 503，不回退到 Scripted，也不让浏览器自行生成幂等键或授权引用。

确认后提交原请求，校验回执中的 Preview、请求编号、各 Hash、服务身份及三个禁止字段。
页面只把 ready 显示为“授权核对完成，尚未创建轮次”，不显示执行中或性能收益。
凭据不写入 Web Storage，POST 完成或失败后清空；重试需重新输入同一凭据。
刷新后从部署绑定重读同一请求；声明过期后只能通过已有受保护管理面核对，不能新建请求绕过。

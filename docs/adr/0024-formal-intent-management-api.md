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
删除部署绑定即撤销后续提交权限，包括重放。生产加载器和操作工具在后续切片交付。

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
生产启用前必须完成上下游评审、部署凭据签发/加载、隔离 PostgreSQL 并发恢复与页面联验。

# ADR-0018：Messages 单次调度与审核前只读检查

- 状态：Proposed；开发实现和 CPU 联验已完成，跨模块接受待 PR 审核
- 日期：2026-09-07
- 依赖：ADR-0011（Agent/Apex Proposal 权限边界）

## 背景

已有 A 的 Generation Run/Claim/预算账本、B Runner Receipt、C Proposal Store 和 D Verifier，
但真实 Messages 服务尚缺少部署侧接线。已有终态证据入口不能用于尚待人工审核的 Run；
不能为了让页面显示结果，提前完成审核或把开发证据提升为 Formal。

## 决策

1. 新增部署侧 Messages Wrapper 和 Worker，不新增调度状态机、数据库表或 Candidate 格式。
   `run-once` 只领取一个明确指定、单 generator 的 Run，经现有 A Claim → B Runner → C
   Batch → A settle 返回现有状态。没有常驻轮询、隐式重试或公共生成写接口。
2. 使用 `m2b-messages-input-v1` 输入封装；完整输入 manifest Hash 编入既有 Plan 的
   `adapter_profile`，生成器程序另由 Artifact Hash 固定。模型、地址、源码或知识发生变化
   就必须重新建 Plan。知识只提供文本参考，不加载 Skills 代码，不读取 Holdout。
3. 密钥仅由部署环境注入固定 Wrapper，不进入 Request、Plan、DB、argv 或证据。模型只可
   返回意图、理由、风险和单文件 diff；权威 ID、Hash、URI、用量结算由系统负责。
   禁止工具调用、生成代码执行、自动跳转和继承代理。指定 HTTP 服务不具备传输加密，
   仅可在已批准的受信网络内使用，不扩展为通用公网服务配置。
4. Claim lease 不超过冻结 timeout；Worker 在该预算内预留清理/结算时间。API 输出 token
   上限是请求约束，总 token 上限是事后接纳约束，不是保证不超支的货币限额。
5. 调用前独占创建 started marker，持久保存 Claim、输入和 Receipt。相同 Attempt 不能再调
   模型；恢复只重读证据并调用原 A settle，过期或漂移由 A 拒绝。没有 Receipt 的已启动
   Attempt 交原 reconcile 处理，不删除 marker，不推定模型没有收费。
6. HTTP/Runner 成功不等于 Proposal 成功。非法 diff 或零提案生成 failed Batch，A 保留
   Receipt 实际用量；D 核对失败 Batch 的错误码，barrier 按 A Attempt 成功数计算。
7. 新增 `m2b-agent-inspection-v1` 只读外壳与 GET inspection 入口，内层仍为 D v1 ReadModel。
   仅允许所有 Attempt 结算且 Run 为 awaiting_review/failed。部署命令先保存不可变上下文，
   GET 用原生安全 Reader 重读所有 Hash 引用、重跑 D，并在前后比较当前 A 状态。
   状态或文件变化即失败关闭；不修改 A，不写 Review/Promotion 或终态 publication。
8. inspection 沿用 D v1 的 `synthetic=true / scripted_dev_only` 开发证据分类，不据此推断
   Runner 或模型是真实还是模拟；真实执行来源单独由 provenance 描述。外壳明确标注
   `development_only_not_performance`，Formal Intake 和自动发布始终为 false。
9. UI 新增 inspection 直达入口，无 demo 回退；零提案仍展示 Attempt、用量和失败原因。
   `create_app` 必须注入部署所有的 `agent_inspection_read_authorizer(Request, RunID)`，
   每次读取都先验证当前身份对确切 Run 的权限；仅显式 `True` 放行。缺失/异常返回 503，
   拒绝或其他 truthy 值返回 403，均在读 Store/A 状态之前退出。鉴权错误不回显异常细节，
   成功与拒绝响应均设 `Cache-Control: no-store`，不让共享 HTTP 缓存跳过下一次授权。
   该注入点不是现成 SSO，身份校验/授权策略由部署侧已有服务提供；禁止恒真回调、相信
   未验签的用户请求头或用模型 Key 充当浏览器凭据。只限受控本地/内部开发环境，不能把
   配置证据根目录视为开放全站的许可。

## 兼容与失败语义

- 原数据库 Schema、A/B/C 核心 Contract 和终态 `/evidence` 行为不变，无迁移。
- 未配置 inspection root 返回 503；不可信引用/状态漂移返回 422；原生安全读取要求
  POSIX openat/O_NOFOLLOW，Windows 不降级为不安全读取。
- 开发版升级后仅配置 root 不再可读；须显式接线部署鉴权。只保护新增 inspection 入口，
  不宣称旧 API 已完成全站权限改造；反向代理应仅开放获准的只读路径。
- CLI 返回状态且退出 0 不代表 Proposal 获准；人工 Review 后使用原终态证据链，不能覆盖
  原审核前快照冒充新状态。
- Store 含源码、原始模型回复和 Claim token，必须部署独占，不进入公开站点或 Git。

## 验证与接受

契约测试覆盖输入和身份漂移、Claim 防重、Receipt 恢复、过期拒绝、失败用量、D 重读、API
篡改拒绝和前端权限边界。真实 PostgreSQL 与本地模拟模型的 Linux CPU 整链 12 项通过；
原始验收与源码包 Hash 见 [接入记录](../m2b-live-messages-generation.md)。没有 HCU 性能结论。

- [ ] A 接受领取、预算、恢复和状态语义。
- [ ] B/C 接受 Wrapper、输入封装、凭据、Receipt 和失败 Batch 边界。
- [ ] D 接受审核前检查外壳、独立复核和 UI 证据分类。

## 单用户只读部署补充（2026-09-07）

项目所有者选择第一版使用独立访问凭据、限定 Run，不接企业 SSO。新增专用只读 app，只有
healthz 与 inspection GET，不挂载控制面，也不执行迁移。配置通过用户私有文件保存密码
摘要、Run allowlist 与到期时间，每次请求原生安全重读；凭据由排他创建命令随机生成，明文
只交操作者，不进入模型/前端代码。HTTP Basic 仅允许 HTTPS 或本机通道，默认监听 loopback。
它复用原 D inspection service，不实现新 Evidence/Review/签核语义；完整 API 的鉴权回调
仍保留，以供将来的身份服务接入。本补充不等于跨模块接受或实际部署授权。

操作、撤权和验收边界见 [单用户只读部署](../agent-inspection-deployment.md)。

受控部署试验发现内置浏览器的原生 Basic 提示不可见，故前端改用显式只读登录表单。
凭据仅在当前页内存中使用，提交后清空密码输入框，刷新/退出清空页面登录状态；只向当前
源的 inspection 路径发送，拒绝重定向，不使用 cookies 或浏览器持久存储。服务端逐请求
鉴权、Run ACL、到期时间及 D 复核语义均不变；这不是新增身份服务或授予签核权限。

## 回滚方式

停用新 CLI Worker、取消 inspection root 配置并移除页面直达入口即可停用此接线。
在途 Attempt 仍按 A 的原 reconcile/结算规则处理，不删除证据或账本，不触碰 HCU/系统库。

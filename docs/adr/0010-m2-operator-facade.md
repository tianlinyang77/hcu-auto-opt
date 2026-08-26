# ADR-0010：M2 操作面是可信控制面的版本化外观

- 状态：Accepted / OX-1 Scripted implementation authorized
- 日期：2026-08-26
- 跟踪：GitHub Issue #46
- 依赖：ADR-0009
- 接受记录：A/B/C/D 分别在 #49、#50、#51、#52 完成 `accepted-for-draft`；项目所有者接受 OX-0 并在 #46 授权 OX-1 无 HCU Scripted 实现

## 背景

M2a 拟引入多 Candidate SearchRound、Barrier、Budget、Round Evidence 和 Signoff。若操作者仍需
手工组合 Target、Stage0Run、Baseline、Hotspot、Candidate、Profile 和多个写接口，系统虽可信
但难以重复使用；若 Web/CLI 自己维护任务状态或串联部分成功的写请求，又会形成第二套控制面。

## 决策

### 1. 操作面不拥有领域状态

CLI、Web、Profile Registry、Plan Compiler、Read Model 和通知统一称为 Operator Facade。所有
权威写入仍由 SearchRound 控制面完成；Facade 不直写数据库，不关闭 Barrier，不计算 verdict，
也不创建第二套 Round 状态机。

### 2. Profile 是版本化选择模板，不是事实副本

Operator Profile 只保存不可变版本、内容 Hash、允许模式和现有 Authority 引用。TargetSpec、
TargetSnapshot、Stage0Run、Baseline、Hotspot、Candidate Source、Measurement Protocol 和
AdapterProfile 仍由各自当前 Authority 提供。Formal 禁止 `latest` 或移动别名。

Candidate Source 只从 Target Profile 绑定的部署侧内容寻址 Package Store 解析。兼容已签核的
M1 Schema，部署侧 C Intake Publisher 在发布 Package 前分配 `candidate_id` 并写入不可变
Manifest；UI 只提交内容寻址引用和 expected assertion，不能指定 Candidate、Source、
Replacement Point 或 Artifact 事实。

### 3. 启动前生成持久化 Plan Preview

Plan Compiler 在不占用 HCU 的情况下解析 Profile、执行 Preflight，并保存内容寻址、带有效期的
`RoundPlanPreview`。Preview 冻结 Search/Holdout 协议和输入摘要，但不伪造尚未绑定 Round 的
Search Plan Hash 或 Holdout commitment；阻塞项存在、Preview 过期或权威引用漂移时不得启动。

### 4. 一键启动是服务端 durable 命令

客户端只提交 Preview ID/Hash、幂等键、actor 和服务身份预期。控制面先写 durable
`OperatorStartIntent`，随后由 Reconcile 驱动：创建 Round Authority、让 D 冻结 Search Plan
与 Holdout commitment、批量 Intake Candidate 并关闭 Intake，最后才允许排 Job。半完成 Round
可以审计和恢复，但不可执行；禁止由浏览器承担多步写入或补偿逻辑。

StartIntent 在开始 Reconcile 前冻结逐候选成员：Manifest 中预分配的 `candidate_id`、由 Round 和
输入摘要确定性派生的 `round_candidate_id`、独立 Intake 幂等键和成员进度。任一成员未完成绑定
或 Intake Close 未完成时，Round 都不可执行。

### 5. 状态展示来自可重建 Read Model

Round/Candidate/Resource/Evidence Summary 由版本化 reducer 从权威对象和事件重建。缓存或页面
丢失不影响任务；ETA、进度和摘要明确标记为派生信息，不能替代原始 Evidence。

### 6. CLI 先于 Web，写页面晚于只读页面

先完成 Scripted `plan/start/status/report` 和 Windows/Linux 契约测试，再建设只读 Web。只有
Cancel、Signoff Intent/Outbox、鉴权和崩溃恢复通过后，才开放 Web 写操作。

### 7. Scripted/Formal 始终 fail-closed

Scripted 必须使用 synthetic Authority/Profile/Candidate，Formal 必须使用真实且已授权的
Authority/Profile/Candidate。不能通过环境变量、页面参数或 Profile 默认值升级模式；两者均
固定 `automatic_release_allowed=false`。

### 8. 操作成本与性能证据分开

Facade 记录主动操作时间、人工介入、Preflight 提前阻塞、自动清理和报告生成时间。这些指标
用于评价产品易用性，不进入 Candidate 性能裁决或 MDE/FWER。

## 影响

- 日常操作可以减少 SSH、手工 Docker、数据库写入和内部 ID/Hash 复制；
- 新增 Profile/Preview/Read Model 的版本与兼容责任；
- 启动前多一次 Preflight 和确认，但可在占用 HCU 前发现错误；
- Web 交付晚于 CLI，但不会产生两套业务逻辑；
- ADR-0009 已 Accepted；允许实现 synthetic Authority/Profile、Scripted CLI/API、增量迁移和
  无 HCU 测试，但仍不注册 Real Operator Profile，不创建 Formal Round。

## 非目标

- 本 ADR 只批准 OX-1 无 HCU Scripted Facade 和必要迁移；M2a 核心运行时授权来自 ADR-0009，
  本 ADR 不批准 HCU Formal、Agent 或自动发布；
- 不定义模型/服务 E2E 收益，也不改变 M1 v1 Evidence；
- 不规定具体 Web 框架、通知供应商或企业鉴权产品。

## 接受条件

- [x] A 确认 durable StartIntent、幂等、Read Model 和服务身份语义；
- [x] B 确认 MeasurementPreset、Lease/Cleanup 摘要不形成第二套 Harness；
- [x] C 确认 Candidate Source Package 和 Artifact 引用不可变；
- [x] D 确认 Hotspot、verdict、Evidence 与 Signoff 展示不重新计算结论；
- [x] 项目所有者接受 OX-0 Operator Contract；
- [x] 项目所有者另行批准 OX-1 无 HCU Scripted 实现；
- [ ] 独立授权后才允许 Real Operator Profile 或 HCU Formal。

本 ADR 现允许 OX-1 无 HCU Scripted 运行时代码和必要的增量迁移。它不批准 Real Operator
Profile、HCU Formal、Agent、Baseline 提升或自动发布；这些能力继续受 ADR-0009 和各自独立
授权门禁约束。

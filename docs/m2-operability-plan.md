# M2 操作面与易用性建设计划

- 状态：OX-0 completed / OX-1 Scripted implementation authorized
- 跟踪：GitHub Issue #46
- 依赖：[M2 搜索轮次建设计划](m2-round-plan.md)和
  [M2a SearchRound Contract 草案](m2-contract-draft.md)
- OX-0 接口：[M2 Operator Contract 草案](m2-operator-contract-draft.md)
- 当前授权：OX-0 接口已冻结；允许 OX-1 无 HCU Scripted 运行时代码、增量迁移和测试

## 1. 定位

易用性是 M2 的正式交付要求，不是控制面完成后再补的展示页面。建设目标是把注册环境下的
日常操作压缩为“选择 Profile、确认计划、启动、等待、查看报告、签核”，同时保持现有可信
控制面为唯一写入权威。

操作面只负责把人类友好的输入编译成现有 Contract、展示权威状态和引导下一步；它不能：

- 直接修改数据库、Round 状态、Barrier、Budget 或 Evidence；
- 绕过 Target Lock、Stage 0、Lease/Fencing、正确性、Holdout 或清理门禁；
- 把 Scripted/Fake 结果显示成 Formal 结论；
- 把人工接受 Evidence 解释为 Baseline 提升或发布授权；
- 给 Candidate Generator、Agent 或 UI 赋予测量、裁决和签核权威。

M2 继续固定 `automatic_release_allowed=false`。

## 2. 面向用户的标准流程

```text
选择目标环境和工作负载
→ 选择并确认热点
→ 提交候选或选择获准的生成策略
→ 选择资源与测量预设
→ 预览不可变执行计划
→ 幂等启动并等待通知
→ 查看结果和递归 Evidence
→ Formal 人工签核
```

### 2.1 选择目标环境和工作负载

用户选择版本化 Profile，而不是手工填写主机、镜像、Commit、Adapter、Shape 和计时参数。

`TargetProfile` 至少引用：

- 已登记的 Target、资源池和拓扑要求；
- 镜像 Digest、源码 Commit、运行时版本和 Adapter Profile；
- Stage 0 Authority、Baseline Epoch 和允许的运行模式；
- 默认 Lease、清理和证据保留策略。

`WorkloadProfile` 至少引用：

- 模型/权重版本和启动参数；
- 输入数据集 Hash、Batch、长度、dtype 和并发；
- 预热、重复、正确性与性能指标；
- Workload/Configuration Hash 和适用的 Hotspot 集合。

Profile 只是版本化默认值和权威引用，不复制 TargetSnapshot、Stage0Run、Baseline 或 Workload 的
事实。启动时必须把解析结果冻结到 Round，后续 Profile 更新不能改变已创建 Round。

### 2.2 选择并确认热点

操作面读取 Profiler/人工 Triage Evidence，展示时间占比、调用次数、主要 Shape/dtype、源码和
Replacement Point。操作者确认一个 `HotspotSpec`，至少冻结：

- Hotspot ID、来源 `profiler|manual` 和原始 Evidence Hash；
- Replacement Point、当前实现与端到端占比；
- Shape/dtype 范围、优化假设和语义不变量；
- 可接受的 Candidate Track 与回退方式。

Profiler 降级时允许人工确认，但 UI/报告必须持续显示 `manual`，不能冒充自动发现。

### 2.3 提交候选或选择生成策略

M2a 只允许人工提交 2–4 个不可变 Overlay Candidate。操作面为每个候选收集源码/补丁、优化
意图、Replacement Point、支持范围和 Source Hash，并调用 Candidate Intake API。

M2b 若获另行批准，只增加 `CandidateGeneratorAdapter` 选择。生成器只能输出待 Intake 的
Candidate，不能访问 HCU Worker、Measurement Harness、Barrier、FWER、Signoff 或发布接口。
Skills 只作为生成候选的知识来源，不成为系统 Authority。

### 2.4 选择资源与测量预设

普通操作者优先选择版本化 `MeasurementPreset`：

| 预设 | 用途 | 结论边界 |
| --- | --- | --- |
| `explore` | 低成本检查方向 | 只筛选，不形成 Formal 结论 |
| `standard` | Scripted/日常可信回归 | 固定 Search/Holdout 和预算 |
| `formal` | 获授权后的正式证据 | 完整隔离、统计校正、证据与签核 |

Preset 编译为 Candidate、Build/Correctness Attempt、Search/Holdout Sample、墙钟和独占 HCU
Lease 秒数上限。系统依据 Stage 0 MDE 与 Workload MDE 推荐采样预算；高级参数必须显式展开，
不能让默认值悄悄覆盖 Formal 协议。

### 2.5 预览并启动

`PlanCompiler` 在不占用 HCU 的情况下完成 Preflight，并输出规范化 `RoundPlanPreview`：

- 解析后的 Target、Workload、Baseline、Hotspot、Candidate Input Set 和 Profile 版本；
- Search/Holdout 协议、Candidate Input Set Hash、Budget 和预计资源上限；
- `scripted|formal`、Evidence/Signoff 边界和 `automatic_release_allowed=false`；
- 阻塞项、警告、修复动作和计划摘要 Hash。

存在阻塞项时不得创建 Round。用户确认 Preview 后，以其 Hash 和幂等键创建 durable
StartIntent；服务端创建 Round 后再由 D 冻结 Search Plan/Holdout commitment、批量 Intake 并
关闭 Candidate Family。Intent finalized 前不排 Job；重复启动返回同一个逻辑对象，不重复占用
预算。

### 2.6 等待、通知与取消

用户不需要守着终端。操作面读取控制面事件构建只读 `RoundSummary`，展示排队、Build、
Correctness、Search、Holdout、Evidence、Cleanup 和 Signoff 状态。

通知至少覆盖：开始、等待资源、候选失败、需要人工动作、Round 完成、清理失败和等待签核。
通知通过 Outbox 异步发送，失败不能回滚 Round，重复发送不能重复执行业务动作。

Cancel 只能调用控制面取消 API：停止排新 Job，执行 Fence/清理，保留已产生 Evidence。Lease
过期或前端关闭不能被显示为“资源已清理”。

### 2.7 报告与人工签核

结果页同时展示：

- Baseline/Candidate 原始样本、置信区间、MDE 和调整后 verdict；
- 正确性、失败成员、Family/Plan/Artifact/Measurement 绑定；
- Target/Workload/Profile provenance、Budget 和资源清理状态；
- Evidence Index 递归可读性与 Hash 复核结果；
- 当前决定只代表接受或拒绝 Evidence，不代表发布。

Scripted Round 只能进入 `scripted_completed`。Formal Signoff 页面必须重新核对 actor、最终
EvidenceBundle、Intent/Artifact 状态和幂等键；递归验证失败时签核按钮保持禁用。

## 3. 操作面架构

```text
CLI / Web Wizard
      ↓
Operator API
      ├── Profile Registry
      ├── Plan Compiler / Preflight
      ├── Read Model / Report Builder
      └── Notification Adapter
      ↓
现有可信控制面与 Repository
      ↓
Job / Worker / HCU / Evidence / Signoff
```

### 3.1 单一权威

- 所有写操作进入现有控制面 API/Repository；Web 和 CLI 不直连数据库写入；
- Profile Registry 保存模板版本，不保存第二份任务状态；
- Read Model 可重建，权威事件、Round、Evidence 和 Signoff 不依赖页面缓存；
- CLI 与 Web 使用同一 Operator API、错误码和幂等语义；
- UI 中的进度、ETA 和摘要必须标记为派生信息，不能代替原始 Evidence。

### 3.2 拟议 Operator API

以下仅是操作面草案，不代表已实现：

```text
GET  /v1/operator/profiles
GET  /v1/operator/workloads
GET  /v1/operator/hotspots
POST /v1/operator/round-plans:preview
POST /v1/operator/round-plans/{preview_id}:start
GET  /v1/operator/start-intents/{intent_id}
GET  /v1/operator/search-rounds/{round_id}/summary
GET  /v1/operator/search-rounds/{round_id}/report
POST /v1/operator/search-rounds/{round_id}/cancel
POST /v1/operator/search-rounds/{round_id}/signoff
```

Operator API 只能组合和调用 M2 Contract 已批准的能力。ADR-0009 已 Accepted，但当前只允许
注册 synthetic Scripted Profile；Real Profile 仍须独立授权。

### 3.3 拟议 CLI

```text
hcuopt profile list|show
hcuopt workload list|show
hcuopt hotspot list|show
hcuopt round plan
hcuopt round start
hcuopt round status --watch
hcuopt round report
hcuopt round cancel
hcuopt round signoff
```

第一阶段以 Scripted Profile 做一条命令闭环。Formal 命令必须显式 `--formal`、展示风险摘要并
完成服务身份、Profile、Target Lock 和授权核对；不能根据环境变量自动升级为 Formal。

### 3.4 Web 页面

Web 不先于稳定 API 建第二套逻辑，按以下顺序交付：

1. 只读任务列表和 Round 详情；
2. Candidate/Barrier/Budget/资源进度；
3. 结果、置信区间、Evidence 与清理报告；
4. Profile 驱动的创建向导；
5. 受控 Cancel 和 Formal Signoff。

Fake、Scripted 和 Formal 使用固定且明显不同的标识；页面不得通过颜色以外的单一线索区分。

## 4. 分阶段建设

### OX-0：操作 Contract 与验收冻结

与 M2-0 同步，只做文档和无 HCU 设计：

- 冻结 Profile ID/Version、Plan Preview、Summary、错误码和幂等语义；
- 明确哪些字段来自现有 Authority，禁止复制事实；
- 冻结 CLI 命令、Scripted/Formal 标签和操作成本指标；
- A/B/C/D 已完成 `accepted-for-draft` Review；ADR-0009 Accepted 前不创建 Real Operator Profile。

### OX-1：Scripted CLI 与 Plan Compiler

与 M2-1 至 M2-4 的无 HCU 实现并行：

- `profile/workload/hotspot` 查询；
- `round plan/start/status/report`；
- Preview 阻塞项、幂等启动、稳定错误和 Scripted 全链；
- Windows/Linux CLI 契约测试。

当前前两个运行时切片已实现 `m2-operator-v1` Service Identity、三类 strict Profile Contract、
确定性 Profile/Catalog Hash、默认仅允许 synthetic Scripted 的 Profile Registry、Profile
list/show API，以及 PostgreSQL 持久化的 `round plan` Preview/Preflight。Preview 会重新解析
Target/Stage 0/Baseline/Hotspot Authority、复核内容寻址 Candidate Package，并显式冻结协议、预算、
输入集合 Hash、有效期和阻塞/警告；它不占用 HCU，也不创建 Task 或 SearchRound。

第三个运行时切片进一步实现了 durable synthetic StartIntent/Reconcile：Start 只接收 Preview
ID/Plan Hash、warning 确认、actor、幂等键和 Service Identity；服务端确定性冻结 Task/Round/成员
身份，消费 D-owned Scripted Plan Authority，幂等创建 Round、逐成员接入并完成 Intake Close。
StartIntent 未 finalized 前不排 Job，崩溃后可从持久化进度重放；同一 Preview 只能绑定一个逻辑
Round。该切片仍不运行 HCU，也不生成性能结论。

最后一个 OX-1 运行时切片提供版本化只读 Summary/Report，以及
`profile list|show`、`round plan|start|status|report|run` CLI。命令从 Preview/Start 文件自动
传递 ID 与 Hash；显式部署 Package Store、allowlist 和 D-owned Plan Authority 后可形成无 SSH、
无手工数据库写入的 synthetic 操作闭环。finalized 仍只表示 Round Authority 及 Candidate Family
已安全建立，不表示优化已执行。

OX-1 收尾切片增加 `workload list|show`、`hotspot list|show` 和 `round draft`。热点只从最新匹配的
PostgreSQL Scripted Authority 读取，候选只从部署侧内容寻址 Package Store 完整复核后展示；Draft
按稳定编号生成 Plan Spec，普通操作者不再准备完整 UUID、Evidence URI 或 Hash。`round run` 同时
原子保存 `metrics.json`，以真实单调时钟记录主动操作、warning 确认、HCU 前阻塞和报告生成时间；
这些指标固定不是性能 Evidence。

退出条件：注册 Scripted Profile 后，一条命令启动、另一条命令查看状态和导出报告，全程无需
SSH、Docker、数据库写入或复制内部 UUID/Hash。

### OX-2：Read Model、通知与故障引导

- 从权威事件重建 Round/Candidate/Resource/Evidence Summary；
- 失败信息包含阶段、稳定错误码、是否可重试和下一步；
- Outbox 通知与重复投递测试；
- Cancel 后展示 Fence、进程、显存和环境恢复证据，而不是只展示 Lease 过期。

### OX-3：只读 Web

Scripted CLI 和 Read Model 稳定后再实现只读 Web。首版只读取 API，不提供写操作；至少覆盖
任务列表、流程进度、Candidate 家族、结果、Evidence 和 Cleanup。

### OX-4：受控创建与签核

在 M2 Signoff Intent/Outbox、鉴权和故障恢复通过后，才开放 Web 创建、Cancel 和 Signoff。
所有危险操作要求再次显示 Round/Target/Mode/Evidence Hash，并保存 actor 与 reason。

## 5. 分工

| 负责人 | 操作面责任 | 不改变的边界 |
| --- | --- | --- |
| A | Operator API、Profile Registry、Plan Compiler、Read Model、鉴权与幂等 | 唯一控制面与状态权威 |
| B | 资源/测量进度、Lease/Cleanup 摘要和测量阻塞原因 | 唯一 Measurement Harness |
| C | Candidate Intake、源码/制品状态和构建错误摘要 | Artifact 与 Source Hash 权威 |
| D | Hotspot Evidence、统计结果解释、Evidence Report 和签核前验证 | 独立裁决与 Evidence 权威 |

Web 可以由 A 主接线，但页面字段必须由对应 DRI Review，不能由前端重新计算性能结论。

## 6. 验收与操作成本指标

注册 Profile 的日常任务至少满足：

- 不需要 SSH、手工 Docker、数据库写入或复制内部 ID/Hash；
- `plan` 不占用 HCU，且在启动前显示全部阻塞项和资源上限；
- 相同 Preview/幂等键重复启动不会创建第二个 Round 或重复收费；
- `status/report` 能解释当前阶段、失败原因、下一步和证据完整性；
- Cancel、崩溃和 Lease 过期均 fail-closed，并可证明资源恢复；
- Scripted/Fake 不能进入 Formal Signoff；人工签核不改变发布权限；
- Windows/Linux Scripted 一条命令闭环通过后，才讨论 Web 写操作；
- Formal 操作者主动操作时间目标不超过 15 分钟，不包含排队、构建和测量墙钟时间。

最后一项是产品操作目标，不是当前 SLA 或性能证据。系统还应记录：

```text
operator_active_seconds
manual_intervention_count
time_to_first_actionable_error
preflight_blocked_before_hcu_count
automatic_cleanup_success_rate
report_generation_seconds
```

这些指标用于判断系统是否真的降低操作成本，但不得进入 Candidate 性能裁决。

## 7. Go/No-Go

满足以下条件前，易用性不能宣称“建成”：

- OX-0 Contract 和 DRI Review 完成；
- Scripted CLI 能从 Plan 到 Report 完整运行；
- Read Model 可由权威事件重建，页面不成为第二套状态机；
- Formal 风险检查、Evidence 递归验证和 Signoff 崩溃恢复通过；
- 资源清理失败在 CLI/Web 中可见且禁止签核；
- 操作成本指标被真实记录，而不是根据演示过程估算。

未达到以上条件时，可以称为 Demo 或 Scripted 操作入口，不能称为 Formal 一键优化平台。

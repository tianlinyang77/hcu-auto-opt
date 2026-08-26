# M2 Operator Contract 草案（OX-0）

- 状态：Draft / Non-runnable
- 跟踪：GitHub Issue #46
- 依据：[ADR-0010](adr/0010-m2-operator-facade.md)（Proposed）
- 依赖：[M2a SearchRound Contract 草案](m2-contract-draft.md)

## 1. 权威与版本边界

本草案只定义面向 CLI/Web 的选择、Preview、启动和只读展示接口。所有输入模型继承
`ContractModel(extra=forbid)`，Read Model 继承 `ReadModel(extra=ignore)`；代码实现前必须新建
独立版本，不向现有 `v1` 模型静默加字段。

Operator Profile 与现有 `AdapterProfile` 含义不同：前者是用户选择模板，后者是 Task/Worker
能力组合。命名、数据库表和 API 路径必须显式包含 `operator`，禁止复用 Adapter Catalog。

所有 Hash 使用完整 `sha256:<64-hex>`。规范化输入采用 UTF-8 JSON、字段名排序、无多余空白、
UUID 小写连字符和 RFC 3339 UTC 时间。公共 View 不返回凭据、内部地址、宿主路径或未脱敏日志。

## 2. 公共身份

### 2.1 `OperatorServiceIdentity`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `source_commit` | 40-hex Git Commit | 当前服务代码 |
| `control_contract_version` | string | SearchRound Contract 版本 |
| `operator_contract_version` | literal `m2-operator-v1` | 操作 Contract |
| `profile_catalog_hash` | SHA256 | 本次可选 Profile 目录 |
| `server_instance_id` | UUID | 部署 generation；普通进程重启保持，换部署时更新 |

Preview 和所有可写响应都返回该身份。写请求带 `expected_service_identity`；任一字段不匹配返回
`service_identity_mismatch`，客户端必须重新 Preview，不能自动忽略。

### 2.2 `OperatorProfileDescriptor`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `profile_id` | stable string | 不含版本的逻辑名 |
| `profile_version` | positive int | 显式选择，Formal 禁止 latest |
| `profile_kind` | `target|workload|measurement` | 三类目录 |
| `profile_hash` | SHA256 | 规范化内容 Hash |
| `state` | `active|deprecated|revoked` | revoked 禁止新 Preview |
| `allowed_run_modes` | set[`scripted|formal`] | 不能在请求中扩大 |
| `display_name` / `summary` | string | 仅展示，不进入事实绑定 |
| `authority_refs` | object | 指向现有 Authority，不复制内容 |
| `created_at` | datetime | 审计时间 |

Profile 发布后不可修改；更新必须生成新 version/hash。Deprecated 可回放已有 Round，但新 Preview
必须警告；Revoked 只允许读取历史，禁止 Preview/Start。

`authority_refs` 不是自由 object，而是由 `profile_kind` 判别的严格联合：

| 类型 | 必填字段 |
| --- | --- |
| `TargetOperatorProfileRefs` | `target_id`、TargetSpec Hash、exact Adapter Profile、resource policy ID/Hash、required Stage 0 protocol Hash |
| `WorkloadOperatorProfileRefs` | `workload_id/hash`、`configuration_hash`、数据/模型 URI/Hash、Hotspot 范围、baseline selection policy |
| `MeasurementOperatorProfileRefs` | Search/Holdout protocol version/hash、selection rule hash、RoundBudget 默认值、`explore|standard|formal` 结论边界 |

当前唯一 baseline selection policy 为 `latest_frozen_matching`：Plan Compiler 查找与已解析
Target/Workload/Configuration 完全匹配的最新冻结 Epoch，并在 Preview 中显示并绑定具体 ID；Start
时该 ID 漂移或失效必须重新 Preview，不能静默改选。

Profile 不保存 TargetSnapshot、Stage0Run、BaselineEpoch 或 Credential；Plan Compiler 每次从权威
Repository 解析并冻结它们。

## 3. Preview 输入

### 3.1 `OperatorHotspotRef`

绑定 `hotspot_id`、Hotspot Intake Hash、`profiler|manual` 来源、Profiler/Correctness Evidence
URI/Hash、Replacement Point、Workload Hash 和 Shape/dtype 摘要。来源为 manual 时所有 View 保留
该标签。

### 3.2 `OperatorCandidateInput`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `ordinal` | int 0..3 | 稳定排序，不代表排名 |
| `source_package_uri/hash` | URI/SHA256 | 部署方内容寻址根 |
| `source_manifest_version` | string | 可读解析器版本 |
| `candidate_source_hash` | SHA256 | 冻结源码身份 |
| `optimization_intent` | string | 单一可审计假设 |
| `replacement_point` | string | 必须等于 Hotspot/Round |
| `candidate_kind` | `fixture|business` | 由 run_mode 约束 |

不接受本地相对路径、行内源码、移动分支或 Tag-only 制品。Scripted 只能 fixture，Formal 只能
business。2–4 个输入的 ordinal、Source Hash 和 Package Hash 必须唯一。

### 3.3 `RoundPlanPreviewRequest`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `name` | string | 展示名称，不进入性能语义 |
| `run_mode` | `scripted|formal` | 不可自动升级 |
| 三类 `profile_id/version/hash` | exact refs | 禁止移动别名 |
| `hotspot` | `OperatorHotspotRef` | 一个冻结热点 |
| `candidates` | 2..4 inputs | 人工有限族 |
| `max_promoted` | int 1..2 | 不大于候选数 |
| `idempotency_key` | string | 相同输入返回同一 Preview |
| `expected_service_identity` | object | 防旧服务 |

服务端另计算 `preview_request_digest`，覆盖除幂等键外的完整请求；同一幂等键配不同 request
digest 必须冲突。通知偏好、页面语言和展示名称不进入 `resolved_plan_hash`；Target、Workload、
Protocol、Candidate Input Set、Budget 和权限边界必须进入。

## 4. Preview 输出与 Preflight

### 4.1 `PreflightCheckResult`

字段包括 `code`、`scope`、`status=pass|warn|block`、安全消息、`retryable`、`action_code` 和可选
Evidence URI/Hash。消息不能泄漏凭据、内部地址、宿主路径或未经清洗的异常文本。

### 4.2 `ResolvedRoundPlan`

Plan Compiler 从 Repository 解析并冻结：

- TargetSpec/Snapshot、Stage0Run/Protocol、BaselineEpoch 和 Adapter Profile；
- Workload/Configuration/Image/Source Hash；
- Hotspot、Candidate Source Package 和 `candidate_input_set_hash`；
- Search/Holdout 协议 Hash、commitment scheme、选择规则和 RoundBudget；
- `synthetic`、结论边界和 `automatic_release_allowed=false`。

`candidate_input_set_hash` 绑定 Preview 中的 ordinal、Source Package/Source Hash、优化意图与
Replacement Point，但不是 SearchRound Intake Close 后的 `candidate_family_hash`。后者包含服务端
创建的 Candidate ID，只能在 StartIntent 完成 Intake Close 后产生并回绑 Preview。

Formal 必须满足：Stage0Run 为 Formal/finalized、Target/Baseline/Adapter 为真实且已授权、Profile
均允许 Formal、Candidate 全为 business、优化 blocker 已解析。Preview 只冻结 D 的协议和
commitment scheme，不在 Round Authority 产生前生成实际 Search Plan Hash 或 Holdout
commitment。Scripted 必须使用 synthetic Authority/Profile/fixture，且不能产生 Formal Signoff。

### 4.3 `RoundPlanPreviewView`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `preview_id` | UUID | 持久化 Preview |
| `preview_request_digest` | SHA256 | 幂等输入摘要，不等于语义 Plan Hash |
| `resolved_plan_hash` | SHA256 | Start 的唯一输入摘要 |
| `resolved_plan` | object | 可审核但不可由客户端改写 |
| `checks` | list | 全部 pass/warn/block |
| `start_allowed` | bool | 有 block 必为 false |
| `required_ack_codes` | string list | 只允许预注册 warning |
| `expires_at` | datetime | 过期必须重新 Preview |
| `service_identity` | object | 本次服务身份 |
| `automatic_release_allowed` | literal false | 始终关闭 |

Preview 不生成或公开 Holdout commitment、nonce/内容。Preview 持久化后不可更新；权威引用
漂移、Profile revoked、服务身份变化或过期均要求创建新 Preview，不能原地刷新 Hash。

## 5. Durable Start Contract

`OperatorRoundStartRequest` 只包含 `preview_id`、`resolved_plan_hash`、actor、幂等键、确认的 warning
codes 和 `expected_service_identity`。客户端不得重传或覆盖 resolved plan。

服务端重新执行安全 Preflight，先在一个事务中写确定性 `OperatorStartIntent`，再由 Reconcile
执行以下步骤：

```text
锁定未过期 Preview
→ 创建不可执行的 SearchRound Authority
→ D 冻结 Search Plan 与 nonce-sealed Holdout commitment
→ 批量 Intake 2–4 个 Candidate
→ 冻结 Candidate Family / Intake Close
→ 绑定 preview_id 与 round_id
→ Finalize StartIntent，允许控制面排 Job
```

StartIntent 至少保存 `intent_id`、Preview/Plan Hash、确定性 Task/Round ID、输入 digest、actor、
幂等键、实际 Search Plan Hash、Holdout commitment、Candidate Family Hash 和
`preparing|round_created|plans_frozen|intake_closed|finalized|failed`。每一步都幂等并可重放；任何
失败不得让 Round 排 Job。CLI/Web 只轮询同一 Intent，不能串联底层写接口或自行补偿。

同一 Preview 只能启动一个逻辑 Round；完全相同重放返回相同 Round 并标记 `replayed=true`。
同一幂等键配不同 Hash、Preview 已由不同输入启动或服务身份漂移均返回冲突。

Start 首次返回 `202 Accepted` 和 `OperatorStartView`，至少包含 Intent ID/state、Preview/Plan
Hash、run mode、可空 Task/Round ID、可空的实际 Search Plan Hash/Holdout commitment/Candidate
Family Hash、`replayed`、service identity、错误摘要和 `automatic_release_allowed=false`，并通过
`Location` 指向 Intent 查询接口。只有 finalized 时上述 Round/计划/Family 字段全部非空且
`executable=true`；其他状态不能伪装成可执行 Round。

## 6. 只读 Summary 与 Report

`OperatorRoundSummary` 由版本化 `m2-operator-read-model-v1` reducer 从权威对象/事件重建：

- Round：模式、状态、版本、阶段、进度计数和当前 Barrier；
- Candidate：状态、Phase、Artifact、失败码、晋级和最终 verdict；
- Resource：队列、Lease/Fencing、Worker 和 Cleanup；
- Evidence：完整性、缺失引用、递归 Hash 复核和 Signoff readiness；
- Action：需要的 actor role、稳定 action code、原因、retryable 和安全命令提示。

Cleanup 使用 `not_started|running|verified|failed|unknown`；Lease 过期不能映射为 verified。ETA 和
百分比必须标记 derived/estimate。前端不得用 Summary 重新计算速度、CI、MDE、FWER 或 verdict。

`OperatorRoundReport` 引用最终 Round Evidence、全部 Candidate 结果、Budget、Cleanup、适用的
Signoff 和递归校验结果。Scripted Report 显著标记 synthetic，且不提供 Formal Signoff action。

## 7. Cancel、Signoff 与通知

- Cancel 请求带 actor、reason、Round version、幂等键和服务身份；响应展示停止排队与 Cleanup
  状态，但 verified 前不能显示“已释放”；
- Formal Signoff Wrapper 只转发已批准的 RoundSignoff Intent 输入，Evidence invalid、Cleanup
  非 verified、run_mode 非 formal 时 fail-closed；
- 通知偏好不属于 resolved plan。通知 Outbox 只发送事件副本，发送失败不改变 Round，点击通知
  也不能直接执行 Cancel/Signoff。

## 8. API 与 CLI 映射

| Operator API | CLI |
| --- | --- |
| `GET /v1/operator/profiles` | `hcuopt profile list/show` |
| `GET /v1/operator/workloads` | `hcuopt workload list/show` |
| `GET /v1/operator/hotspots` | `hcuopt hotspot list/show` |
| `POST /v1/operator/round-plans:preview` | `hcuopt round plan` |
| `POST /v1/operator/round-plans/{preview_id}:start` | `hcuopt round start` |
| `GET /v1/operator/start-intents/{intent_id}` | `hcuopt round start/status` 内部轮询 |
| `GET /v1/operator/search-rounds/{round_id}/summary` | `hcuopt round status --watch` |
| `GET /v1/operator/search-rounds/{round_id}/report` | `hcuopt round report` |
| `POST /v1/operator/search-rounds/{round_id}/cancel` | `hcuopt round cancel` |
| `POST /v1/operator/search-rounds/{round_id}/signoff` | `hcuopt round signoff` |

CLI 输出默认适合人阅读，并提供 `--json` 输出同一 Contract；脚本不得解析彩色表格文本。

## 9. 稳定错误

| Code | 语义 | Retryable |
| --- | --- | --- |
| `operator_profile_not_found` | ID/version 不存在 | false |
| `operator_profile_revoked` | Profile 禁止新任务 | false |
| `operator_profile_mode_mismatch` | Profile 不允许当前模式 | false |
| `operator_preview_blocked` | Preflight 存在 block | 取决于子检查 |
| `operator_preview_expired` | 必须重新 Preview | true |
| `operator_plan_hash_mismatch` | 客户端 Hash 漂移 | false |
| `operator_warning_ack_required` | 缺少预注册 warning 确认 | true |
| `operator_preview_already_started` | Preview 已绑定其他输入 | false |
| `operator_start_failed` | StartIntent 暂停，需按子错误 Reconcile/处理 | 取决于子错误 |
| `operator_read_model_unavailable` | Summary 尚未重建 | true |
| `operator_cleanup_not_verified` | Cancel/Signoff 前清理未证实 | true |

已有 `service_identity_mismatch`、`m2a_not_approved`、`synthetic_round_not_signable` 和 Round
Contract 错误继续透传 code/retryable，Facade 只能补充 action code，不能改写语义。

## 10. 持久化与测试草案

拟议持久化对象只包括：不可变 Operator Profile version、不可变 Plan Preview、durable
OperatorStartIntent、Preview-to-Round 唯一绑定、可重建 Read Model cache 和 Notification
Outbox。StartIntent 只记录命令进度与权威引用，不保存第二份 Round state、verdict 或 Evidence
内容。

OX-0/OX-1 必测：

- Profile version/hash、revoked、mode 和 `extra=forbid`；
- Preview request/plan 两类 digest、过期、权威漂移，且实际 commitment/nonce 不在 Preview 泄漏；
- Formal/Scripted Candidate、Authority、Adapter 和 synthetic 判别联合；
- 两个并发 Start 只能创建一个 Intent/Round/Candidate Family；各崩溃点可 Reconcile，未 finalized
  Round 不能排 Job；
- 相同输入重放和不同输入冲突；
- Read Model 删除后从事件重建完全一致；
- Lease expired 与 Cleanup verified 不混淆；
- Summary/错误/通知不泄漏凭据、内部地址、宿主路径或原始异常；
- Windows/Linux CLI `--json` Golden Contract 一致；
- M1 v1 Evidence 回放不变，全程无需 HCU。

## 11. Review 与退出条件

| DRI | 必须确认 | 状态 |
| --- | --- | --- |
| A | Profile/Preview、StartIntent/Reconcile、幂等、Read Model、API/DB | Pending |
| B | MeasurementPreset、Lease/Cleanup Summary、错误语义 | Pending |
| C | Candidate Package、Source/Artifact 引用和批量 Intake | Pending |
| D | Hotspot、统计展示、Evidence Report 与 Signoff readiness | Pending |
| 项目所有者 | 只批准 OX-1 无 HCU Scripted 实现 | Pending |

ADR-0009 与 ADR-0010 Accepted、上述 Review 完成前，本草案不得进入 Real Profile、数据库迁移或
Formal Task。OX-0 合入只表示接口可继续评审，不表示 Contract 已冻结或易用性已经建成。

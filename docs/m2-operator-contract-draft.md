# M2 Operator Contract 草案（OX-0）

- 状态：Frozen for OX-0 / OX-1 Scripted implementation authorized
- 跟踪：GitHub Issue #46
- 依据：[ADR-0010](adr/0010-m2-operator-facade.md)（Accepted / OX-1 Scripted implementation authorized）
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

当前 Scripted 实现通过 `GET /v1/operator/identity` 发布该身份。部署必须提供实际 Source Commit
和稳定 deployment generation；客户端应先读取身份，再将其原样作为 Preview 的 expected
assertion。

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
| `TargetOperatorProfileRefs` | `target_id`、TargetSpec Hash、exact Adapter Profile、resource policy ID/Hash、Candidate Package Store ID/version/hash、required Stage 0 protocol Hash |
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

运行时必须实现为以 `source=profiler|manual` 判别的 strict union：profiler 分支绑定原始 Profiler
Evidence URI/Hash，manual 分支绑定人工 Intake/Attestation URI/Hash；自由 object、仅派生摘要或
缺少原始 Evidence 的引用均无效。

### 3.2 `OperatorCandidateInput`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `ordinal` | int 0..3 | 稳定排序，不代表排名 |
| `source_package_ref` | strict object | Package/Manifest/Source Hash 和 Schema Version 的 expected assertion |
| `optimization_intent` | string | 单一可审计假设 |

`source_package_ref` 只包含 `candidate_source_hash`、`source_package_hash`、`manifest_hash` 和 literal
`manifest_schema_version=m1-candidate-source-v1`，不接受 URI、宿主路径、行内源码、移动分支或
Tag-only 制品。实际 Package Store 只由 Target Operator Profile 决定，客户端不能覆盖。

为兼容已签核且强制包含 `candidate_id` 的 M1 `CandidateSourcePackageManifest`，部署侧 C Intake
Publisher 必须在发布 Package 前分配全局唯一 Candidate UUID，并写入 Manifest。该 UUID 不是 UI
输入；Plan Compiler 从受信 Manifest 解析它，Start 时使用该 UUID 创建通用 Candidate 行。除同一
StartIntent 的幂等重放外，已经被其他 Task/Round 使用的 UUID 必须阻塞 Preview/Start。

Plan Compiler 必须通过 Profile 绑定的 Package Store Authority 重新读取 Package：拒绝受信根外
对象、符号链接、可变对象和未注册 scheme；复算 Manifest 与每个文件 Hash；再把 Manifest 中的
Candidate ID、Hotspot、Baseline Source Hash、Candidate Source Hash、Replacement Point、
Candidate Kind、Profiler Evidence 和 Mount Target 与已解析 Authority 逐项核对。UI 提交值只是
expected assertion，不是 Source/Replacement Authority；Artifact 只能由 C Builder/Artifact
Store 在 Build 后产生。Scripted 只能解析 fixture，Formal 只能解析 business。2–4 个输入的
ordinal、Candidate ID、Candidate Source Hash 和 Manifest Hash 必须唯一。

M2 外层 `source_package_hash` 不修改 M1 Manifest；它计算为规范化 JSON
`{manifest_hash, files:[{path,content_hash}]}` 的 SHA256，其中 files 按规范化 POSIX path 排序。
这样 Package 身份同时绑定原始 Manifest 字节和 Manifest 声明的每个文件内容。

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
- Hotspot、Package Store Authority、已验证 Candidate Source Manifest 和
  `candidate_input_set_hash`；
- Search/Holdout 协议 Hash、commitment scheme、选择规则和 RoundBudget；
- `synthetic`、结论边界和 `automatic_release_allowed=false`。

`candidate_input_set_hash` 按 ordinal 排序，绑定 Package Store ID/version/hash、Package Hash、
Manifest Schema/Hash、Manifest 中预分配的 Candidate ID、Baseline/Candidate Source Hash、Hotspot、
Replacement Point、Candidate Kind、文件清单摘要和优化意图。它不是 SearchRound Intake Close
后的 `candidate_family_hash`；后者还绑定服务端确定性 `round_candidate_id` 和 Round Authority，
只能在 StartIntent 完成 Intake Close 后产生并回绑 Preview。

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
→ 按冻结成员绑定创建/重放 2–4 个 Candidate 与 RoundCandidate
→ 冻结 Candidate Family / Intake Close
→ 绑定 preview_id 与 round_id
→ Finalize StartIntent，允许控制面排 Job
```

StartIntent 至少保存 `intent_id`、Preview/Plan Hash、确定性 Task/Round ID、输入 digest、actor、
幂等键、逐候选 `candidate_members`、实际 Search Plan Hash、Holdout commitment、Candidate Family Hash 和
`preparing|round_created|plans_frozen|intake_closed|finalized|failed`。每一步都幂等并可重放；任何
失败不得让 Round 排 Job。CLI/Web 只轮询同一 Intent，不能串联底层写接口或自行补偿。

每个 `OperatorStartCandidateMember` 在 Intent 初始事务内冻结：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `ordinal` / `candidate_input_digest` | int/SHA256 | 绑定 Preview 成员和规范化输入 |
| `source_package_hash` / `source_package_manifest_hash` / `candidate_source_hash` | SHA256 | 绑定受信 Package |
| `candidate_id` | UUID | 从已验证 M1 Manifest 读取，不在 Reconcile 时随机生成 |
| `round_candidate_id` | UUID | UUIDv5(round ID、ordinal、Candidate ID、input digest) |
| `intake_idempotency_key` | string | 由 Intent ID、ordinal、input digest 确定性生成 |
| `state` | `pending|round_member_bound|failed` | 逐成员恢复进度；Candidate 与 Round membership 由同一权威接入原子建立 |
| `error_code` | string/null | 稳定、脱敏；失败时保留 |

Reconcile 可在创建任意第 1～N 个成员后重启，但只能重放同一组 ID 与幂等键。所有成员达到
`round_member_bound` 后才可原子 Intake Close；关闭后崩溃只能重放相同 Candidate Family。并发
Reconcile 依赖数据库唯一约束和行锁收敛，任何部分成功、成员失败或 Hash 冲突都保持
`executable=false`，不得 Build 或排 Job。

同一 Preview 只能启动一个逻辑 Round；完全相同重放返回相同 Round 并标记 `replayed=true`。
同一幂等键配不同 Hash、Preview 已由不同输入启动或服务身份漂移均返回冲突。
首次 Start 已持久化 Intent 后，完全相同的重放不再受 Preview 后续过期影响；尚未创建 Intent 的
过期 Preview 仍必须重新 Plan。非终态 Reconcile 必须继续匹配创建 Intent 时的 Service Identity
和 Plan Authority，部署误换 Authority 密钥时在创建更多 Round 权威前安全停止。

Start 首次返回 `202 Accepted` 和 `OperatorStartView`，至少包含 Intent ID/state、Preview/Plan
Hash、run mode、可空 Task/Round ID、可空的实际 Search Plan Hash/Holdout commitment/Candidate
Family Hash、`replayed`、service identity、错误摘要和 `automatic_release_allowed=false`，并通过
`Location` 指向 Intent 查询接口。只有 finalized 时上述 Round/计划/Family 字段全部非空且
`executable=true`；其他状态不能伪装成可执行 Round。

当前 Scripted 实现已落地上述 durable StartIntent、逐成员进度和 Reconcile，并额外提供内部恢复
入口 `POST /v1/operator/start-intents/{intent_id}:reconcile`。Scripted Plan Authority 使用部署侧
至少 256-bit 密钥对 Round ID 派生 restart-stable Holdout nonce，只向 Operator 返回 commitment
和绑定密钥指纹的 Authority Hash；密钥与 nonce 不进入 Preview、StartIntent、API 或日志。未显式注入该 D-owned
Adapter 时 Start 安全失败。该实现只推进到 Candidate Intake Close，不排 Job、不运行 HCU。

## 6. 只读 Summary 与 Report

OX-1 当前实现的是 `m2-operator-read-model-v1` 基础切片：它从已有 StartIntent、SearchRound、
Candidate、Budget Ledger 和 Evidence Bundle 权威对象读取 Round 状态、Candidate Build 终态计数、
settled Budget 条目数、Evidence 是否可用，以及
`reconcile_scripted_search_round()` 给出的唯一 `next_action`。它不复制状态机，也不重新计算 verdict、
CI、MDE、FWER 或性能结论。CLI 默认输出适合人阅读；`--json` 输出版本化 Contract。

以下完整模型仍属于 OX-2，不能从 OX-1 基础 Report 的存在推断已经实现：

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

OX-1 Scripted Report 固定
`conclusion_boundary=synthetic_only_no_real_performance_claim`、
`formal_signoff_allowed=false` 和 `automatic_release_allowed=false`；终态只跟随 Round Authority，
不能被展示层或 CLI 自行推进。

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
| `GET /v1/operator/identity` | 客户端连接/漂移检查 |
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
| `operator_candidate_package_invalid` | 受信 Store、Manifest 或文件 Hash/绑定校验失败 | false |
| `operator_candidate_id_conflict` | Manifest Candidate ID 已被其他输入使用 | false |
| `operator_preview_blocked` | Preflight 存在 block | 取决于子检查 |
| `operator_preview_expired` | 必须重新 Preview | true |
| `operator_plan_hash_mismatch` | 客户端 Hash 漂移 | false |
| `operator_warning_ack_required` | 缺少预注册 warning 确认 | true |
| `operator_preview_already_started` | Preview 已绑定其他输入 | false |
| `operator_start_failed` | StartIntent 暂停，需按子错误 Reconcile/处理 | 取决于子错误 |
| `operator_candidate_intake_failed` | 某个冻结成员无法按确定性绑定完成 Intake | 取决于子错误 |
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
- Package Store Authority 不可由客户端覆盖；根外对象、符号链接、可变对象、Manifest/文件 Hash
  漂移以及 Candidate/Hotspot/Baseline/Replacement/Kind 绑定漂移全部被拒绝；
- `candidate_input_set_hash` 对 Manifest Schema/Hash、Candidate Kind 或 Package Store Authority
  任一变化敏感；
- Preview request/plan 两类 digest、过期、权威漂移，且实际 commitment/nonce 不在 Preview 泄漏；
- Formal/Scripted Candidate、Authority、Adapter 和 synthetic 判别联合；
- 两个并发 Start 只能创建一个 Intent/Round/Candidate Family；各崩溃点可 Reconcile，未 finalized
  Round 不能排 Job；
- 在第 1～N 个 Candidate 创建后、RoundCandidate 绑定后、Intake Close 前后分别注入崩溃并并发
  Reconcile；重放必须返回同一组 Candidate ID、RoundCandidate ID 和 Candidate Family；
- 相同输入重放和不同输入冲突；
- Read Model 删除后从事件重建完全一致；D 的 verdict、CI、MDE、FWER、Evidence Hash 和
  Signoff readiness 必须逐字段保持，展示字段变化不能改变结论；
- Lease expired 与 Cleanup verified 不混淆；
- Summary/错误/通知不泄漏凭据、内部地址、宿主路径或原始异常；
- Windows/Linux CLI `--json` Golden Contract 一致；
- M1 v1 Evidence 回放不变，全程无需 HCU。

## 11. Review 与退出条件

| DRI | 必须确认 | 状态 |
| --- | --- | --- |
| A | Profile/Preview、StartIntent/Reconcile、幂等、Read Model、API/DB | Accepted for draft，tianlinyang77，#49 |
| B | MeasurementPreset、Lease/Cleanup Summary、错误语义 | Accepted for draft，lvj-repox，#50 |
| C | Candidate Package、Source/Artifact 引用和批量 Intake | Accepted for draft，reverie-hub，#51 |
| D | Hotspot、统计展示、Evidence Report 与 Signoff readiness | Accepted for draft，dddddddxl，#52 |
| 项目所有者 | 接受 OX-0 Contract | Accepted，2026-08-26 |
| 项目所有者 | 另行批准 OX-1 无 HCU Scripted 实现 | Authorized，#46，2026-08-26 |

OX-0 已完成接口冻结和 A/B/C/D Review，ADR-0009 已 Accepted，OX-1 无 HCU Scripted 实现已获
授权。可以创建版本化运行时 Contract、synthetic Profile/Authority、增量迁移、CLI/API 和测试；
仍不得创建 Real Profile、Formal Task、HCU 测量或生产发布路径。

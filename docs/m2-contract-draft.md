# M2a SearchRound Contract 草案

- 状态：Frozen for M2a / Scripted implementation authorized
- 依据：[ADR-0009](adr/0009-m2a-round-barrier.md)（Accepted / M2a Scripted implementation authorized）
- 适用范围：同一 Hotspot 下 2–4 个人工 Python/Triton startup Overlay Candidate
- 当前授权：增量 Contract/迁移、无 HCU Scripted 控制面、synthetic Fixture 和测试

## 1. 权威边界

本 Contract 已冻结 M2a 的接口形状，但不表示这些模型、表、API 或状态已经实现。当前实现授权
只覆盖无 HCU Scripted 路径：

- 可以新增版本化运行时 Contract、增量迁移、控制面、synthetic Authority/Profile/Fixture 和
  Windows/Linux/PostgreSQL 测试；
- 不把这些类型加入可创建真实任务的 Adapter Profile；
- 不创建 M2a Formal Task，不运行 HCU 测量；
- 不运行 Agent、Apex Generator、Beam Search 或自动调参；
- 不改变已签核的 M1 v1 Schema、数据或回放语义；
- 不开放自动安装、Baseline 提升、M3 E2E 或生产发布。

M2a 继续继承 Formal Stage 0 的 `DEGRADED_MANUAL_INTAKE` 模式。人工 Candidate Intake 是能力
边界的一部分，不得用自动发现结果冒充人工确认的 Hotspot 或源码包。

## 2. 冻结顺序

```text
Create Round authority
→ Freeze Search Plan + nonce-sealed Holdout commitment
→ Intake 2–4 immutable Candidates
→ Intake Close / candidate_family_hash
→ Build all members / artifact_family_hash
→ Correctness terminal states
→ Search measurements / Search Barrier
→ If promoted: reveal/verify Holdout Plan, freeze holdout_family_hash and m >= 1
→ If promoted: Holdout measurements / Holdout Barrier / Bonferroni FWER
→ If none promoted: no_promotable_candidate, skip Holdout/FWER
→ RoundEvidenceBundle
→ If formal: Human Round Signoff
→ If scripted: synthetic validation / scripted_completed
```

`candidate_family_hash`、`artifact_family_hash` 和条件性的 `holdout_family_hash` 构成最多
三个不同时间点的权威：

| Hash | 冻结时间 | 规范化输入 | 回答什么 |
| --- | --- | --- | --- |
| Candidate Family | Intake Close | 排序后的 ordinal、Candidate/RoundCandidate ID、Package Store/Package/Manifest Hash、Manifest Version、Baseline/Candidate Source Hash、Hotspot、Replacement Point、Candidate Kind、意图 | 提交了哪些已验证源码候选 |
| Artifact Family | 所有 Build 终态 | 排序后的 Candidate + Artifact ID/Hash；失败成员使用终态与失败证据 Hash | 实际生成或拒绝了哪些制品 |
| Holdout Family | Search Barrier Close 且至少一项晋级 | 排序后的 promoted Candidate、Artifact Hash、`m`、选择规则 Hash | 哪些不可变制品进入最终比较 |

规范化序列使用 UTF-8 JSON、字段名排序、无多余空白、UUID 小写连字符格式和完整
`sha256:<64-hex>`，然后计算 SHA256。任何失败成员都保留在 Candidate/Artifact Family 中；进入
Holdout Family 后失败的成员仍计入冻结的 `m`。

## 3. Contract 模型

所有模型继承仓库 `ContractModel`，默认 `extra=forbid`。以下字段名在评审接受前均为草案。

### 3.1 `SearchRound`

轮次级唯一权威，不复用 M1 `manual_candidate` Task 状态机。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `schema_version` | literal `m2a-search-round-v1` | Contract 版本 |
| `round_id` | UUID | 轮次 ID |
| `task_id` | UUID | M2a Task；当前一个 Task 只允许一个活动 Round |
| `idempotency_key` | string | 创建 Round 的全局重放身份；输入完全相同才返回同一 Round |
| `state` | RoundState | 只由 A 控制面推进 |
| `run_mode` | `scripted` 或 `formal` | 决定真假证据和 Signoff 边界 |
| `project_mode` | `degraded_manual_intake` 或 null | Formal 必须为前者；Scripted 必须为空 |
| `target_snapshot_id` | UUID | Formal 绑定真实 Target；Scripted 绑定 synthetic fixture snapshot |
| `stage0_run_id` | UUID | Formal 必须 formal/finalized；Scripted 绑定 synthetic fixture authority |
| `stage0_protocol_hash` | SHA256 | 绑定真实协议或 synthetic fixture 协议 |
| `baseline_epoch_id` | UUID | 同一逻辑基线 |
| `hotspot_id` | UUID | 同一人工业务 Hotspot |
| `replacement_point` | string | 全家族相同替换点 |
| `workload_id` / `workload_hash` | string / SHA256 | 冻结业务 Workload |
| `configuration_hash` | SHA256 | 冻结运行配置 |
| `image_digest` | SHA256 digest | 禁止 Tag 漂移 |
| `adapter_profile` | string | Formal 必须是 opt-in Real M2a Profile；Scripted 必须显式 synthetic |
| `declared_candidate_count` | int 2..4 | Intake Close 必须精确满足 |
| `max_promoted` | int 1..2 | 且不大于候选数 |
| `family_alpha` | float 0..1 | 由 D 协议冻结 |
| `search_plan_hash` | SHA256 | Search 内容承诺 |
| `holdout_plan_commitment` | SHA256 | Intake 前提交 `SHA256(nonce || canonical_plan)` |
| `holdout_commitment_scheme` | literal `sha256-nonce-v1` | 禁止普通无盐 Hash |
| `holdout_plan_authority_id` / `holdout_plan_authority_hash` | string/SHA256 | D 控制的精确 Store/协议身份 |
| `holdout_plan_hash` | SHA256/null | Search Barrier 后受控揭示并验证成功才写入 |
| `holdout_reveal_lease_id` | UUID/null | 有晋级时绑定获授权 Worker/Lease/Fencing 的一次性读取权 |
| `holdout_reveal_evidence_hash` | SHA256/null | 绑定 commitment、32-byte nonce、Plan URI/Hash、actor 和时间 |
| `selection_rule_hash` | SHA256 | Search 晋级规则 |
| `budget` | `RoundBudget` | 声明硬上限 |
| `candidate_family_hash` | SHA256/null | Intake Close 后只写一次 |
| `artifact_family_hash` | SHA256/null | Build Barrier 后只写一次 |
| `holdout_family_hash` | SHA256/null | 有晋级成员时只写一次；零晋级保持 null |
| `automatic_release_allowed` | literal false | Scripted/Formal 均禁止自动发布 |
| `version` | positive int | 乐观并发控制 |
| `created_at` / `intake_closed_at` | datetime | 审计时间 |

`run_mode=formal` 必须绑定 Formal/finalized Stage0Run、真实 Target/Baseline 和 Real Adapter，且
继承 `degraded_manual_intake`。`run_mode=scripted` 的 `project_mode` 必须为空，并使用显式
synthetic Authority/Profile；所有证据必须 `synthetic=true`，不得调用 Formal Signoff。Scripted
Bundle 递归校验通过后只能由 A 的 Scripted Finalizer 推进到 `scripted_completed`，不得进入
`awaiting_signoff`、`completed` 或 `rejected`，也不得创建 Signoff Intent/Artifact。两种模式都保持
`automatic_release_allowed=false`。

若 Search Barrier 没有晋级成员，`holdout_family_hash`、`holdout_plan_hash` 和 reveal evidence
保持为空，Round 以 `no_promotable_candidate` 跳过 Holdout/FWER；不得构造空 Family 或计算
`m=0`。

Scripted 的 `holdout_plan_authority_id` 必须指向 synthetic Store。Formal 必须指向部署方 opt-in
的受保护内容寻址 Store，并由独立 D 服务身份和文件/对象 ACL 保护；通用业务表只保存
commitment、Authority ID/Hash 和脱敏状态，禁止保存明文计划或 nonce。Search Barrier 关闭后，
D 才能签发一次性 Reveal Lease，绑定 Round、Holdout Family、Worker、资源 Lease/Fencing 和
过期时间。Worker 重建 commitment 成功后才写入 Plan/Reveal Evidence Hash；Reveal Lease 缺失、
过期、跨 Round/Worker 或 Fencing 漂移均拒绝 Holdout 测量。

拟议 `RoundState`：

```text
intake_open | intake_closed | building | correctness
| search_measuring | search_barrier | holdout_measuring | holdout_barrier
| awaiting_signoff | scripted_completed | completed | rejected | cancelled
```

### 3.2 `RoundCandidate`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `round_candidate_id` | UUID | Round 内成员 ID |
| `round_id` / `candidate_id` | UUID | 指向 Round 和通用 Candidate |
| `ordinal` | int 0..3 | 只用于稳定排序，不表示排名 |
| `source_package_store_id` / `source_package_store_hash` | string/SHA256 | Target Profile 绑定的部署侧 Authority |
| `source_package_hash` | SHA256 | 外层规范化 Package 身份，绑定 Manifest 与文件清单 |
| `source_manifest_version` / `source_manifest_hash` | string/SHA256 | 已验证 M1 Manifest 身份 |
| `baseline_source_hash` / `candidate_source_hash` | SHA256 | 与 Manifest 和 Round Baseline 一致 |
| `optimization_intent` | string | 一个清晰假设 |
| `replacement_point` | string | 必须等于 Round 值 |
| `track` | literal `triton` | M2a 不开放 HIP/配置混批 |
| `release_mode` | literal `overlay` | startup Overlay |
| `candidate_kind` | `business` 或 `fixture` | Formal 只能 business；Scripted 只能 fixture |
| `artifact_id` / `artifact_hash` | UUID/SHA256/null | Build 成功后绑定 |
| `terminal_failure_code` | string/null | Build/Correctness/预算失败 |
| `failure_evidence_hash` | SHA256/null | 失败成员也进入 Artifact Family |
| `state` | RoundCandidateState | Candidate 在本 Round 的状态 |
| `idempotency_key` | string | 输入完全相同才可重放 |

M2 不修改 M1 `CandidateSourcePackageManifest`。部署侧 C Intake Publisher 在 Package 发布前分配
Candidate UUID 并写入 Manifest；控制面从 Target Profile 绑定的 Package Store 重新读取、重哈希
和验证 Manifest/文件，再使用该 UUID 创建通用 Candidate。`round_candidate_id` 必须按 Round ID、
ordinal、Candidate ID 和输入摘要确定性派生；UI、自定义 URI 或 Reconcile 过程不得另造身份。
外层 `source_package_hash` 使用 Manifest Hash 与按 path 排序的文件 Hash 清单计算，不向 M1
Manifest 增加字段。

同一 Round 禁止重复 `candidate_id`、`candidate_source_hash`、Manifest Hash、`ordinal` 或幂等键。
Candidate Family Hash 必须直接包含上述 Package/Manifest/Kind 字段，不能假设 Candidate ID
隐式承载其语义。Intake Close 后以上输入字段不可更新；Search 开始后 Artifact ID/Hash 也不可
替换或重建。

### 3.3 `RoundMeasurementRef`

它只包装 M1 单次比较证据，不复制或修改 `m1-kernel-performance-evidence-v1`。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `round_measurement_ref_id` | UUID | 引用 ID |
| `round_id` / `candidate_id` | UUID | 成员绑定 |
| `phase` | `search` 或 `holdout` | 两个 Phase 绝不复用 |
| `candidate_family_hash` | SHA256 | 绑定 Intake Family |
| `artifact_family_hash` | SHA256 | 绑定 Build Family |
| `holdout_family_hash` | SHA256/null | Holdout 必填，Search 必须为空 |
| `artifact_id` / `artifact_hash` | UUID/SHA256 | 本 Candidate 的冻结制品 |
| `measurement_id` | UUID | 现有 MeasurementSeries |
| `evidence_schema_version` | literal `m1-kernel-performance-evidence-v1` | M1 证据只读复用 |
| `raw_evidence_uri` / `raw_evidence_hash` | URI/SHA256 | 可重读原始主文件 |
| `measurement_plan_hash` | SHA256 | B Harness 计划 |
| `phase_plan_hash` | SHA256 | Search 用冻结 Hash；Holdout 用已揭示并验证的 Plan Hash |
| `holdout_reveal_evidence_hash` | SHA256/null | Holdout 必填，Search 必须为空 |
| `baseline_sample_set_hash` | SHA256 | 该 Candidate/Phase 自己的同时期 Baseline 身份 |
| `lease_id` / `resource_id` / `fencing_token` | UUID/UUID/int | 控制面权威资源绑定 |
| `status` | literal `measured` | 只有完整采集才建立 Ref；B 不写快慢 verdict |
| `created_at` | datetime | 审计时间 |

`measurement_id`、`raw_evidence_hash` 和 `baseline_sample_set_hash` 在所有 Round/Phase/Candidate
间唯一，避免复制同一数据后改标签。Holdout 还必须证明其输入、进程、缓存 Namespace 与 Search
不同。采集、清理或证据生产失败时不创建伪装成完整测量的 Ref，失败证据直接进入
`BarrierMemberResult`；D 可据此给出 `invalid`，该成员仍保留在冻结家族中。

Scripted 路径不得创建 `RoundMeasurementRef`。它只生成
`M2ScriptedPhaseReceipt(status=not_measured, synthetic=true)`，用于验证 Phase 隔离、预算、
Fencing、清理和证据传递控制流。该 Receipt 使用独立 Schema/ID，不能被 D、Barrier 或
Round Evidence 当成 `m1-kernel-performance-evidence-v1` 的正式测量输入。只有 Real Harness
产出的 M1 原始证据通过重读与 Hash 校验后，才能建立
`RoundMeasurementRef(status=measured, synthetic=false)`。

### 3.4 `RoundBarrierResult`

Barrier 是批级权威，单个 Worker 不能自行宣告关闭。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `barrier_id` | UUID | Barrier ID |
| `round_id` | UUID | 所属 Round |
| `run_mode` / `synthetic` | discriminated pair | Scripted/true 与 Formal/false 不得混用 |
| `phase` | `search` 或 `holdout` | 每轮每 Phase 唯一 |
| `input_family_hash` | SHA256 | Search 用 Artifact Family；Holdout 用 Holdout Family |
| `expected_member_count` | int | 冻结成员数 |
| `members` | list[`BarrierMemberResult`] | 按 Candidate ID 排序的全部终态 |
| `rule_version` / `rule_hash` | string/SHA256 | 关闭与选择规则 |
| `promoted_candidate_ids` | UUID list | 仅 Search；最多 `max_promoted` |
| `outcome` | `members_promoted` / `no_promotable_candidate` / `completed` | 批级关闭结果 |
| `input_summary_hash` | SHA256 | 全部成员输入摘要 |
| `closed_by` | authority identity | A/D 权威组件，不是测量 Worker |
| `closed_at` | datetime | 关闭时间 |
| `idempotency_key` | string | 并发关闭返回同一对象 |

`BarrierMemberResult` 必须为每个冻结成员保存 Candidate/Artifact、正确性、预算状态和终态。
Formal 成功成员只接受 `RoundMeasurementRef`，Scripted 成功成员只接受独立的
`M2ScriptedPhaseReceipt`；两种 ID 不得同时存在，Receipt 也不得冒充正式性能证据。失败成员保存
失败证据且不创建成功 ID。Holdout 成员即使测量失败也必须保留此前通过的正确性证据。成员缺失、
仍在运行、Family Hash 不符或迟到写回时，Barrier 保持未关闭并返回稳定错误。

Search Barrier 的 `promoted_candidate_ids=[]` 时 outcome 必须为 `no_promotable_candidate`，
控制面直接生成失败家族 Evidence；不得创建 Holdout Barrier 或 MultipleComparison。

`phase` 与结果字段使用判别联合约束：Search Barrier 有晋级成员时 `outcome=members_promoted`，
无晋级成员时 `outcome=no_promotable_candidate`；Holdout Barrier 只能
`outcome=completed`，且 `promoted_candidate_ids` 必须为空。Search 的
`expected_member_count` 等于 Artifact Family 成员数，Holdout 的值等于冻结的 `m`。任何
phase/outcome、成员数或 promoted 列表不一致都拒绝持久化。

### 3.5 `MultipleComparisonResult`

M2a 固定使用 Bonferroni FWER，不允许同轮临时切换 FDR。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `multiple_comparison_id` | UUID | 批级裁决 ID |
| `round_id` / `holdout_barrier_id` | UUID | 绑定已关闭 Holdout Barrier |
| `run_mode` / `synthetic` | discriminated pair | Scripted 结果仅验证统计控制流，不是真实性能结论 |
| `holdout_family_hash` | SHA256 | 冻结最终比较家族 |
| `method` | literal `bonferroni_fwer` | 协议方法 |
| `protocol_version` / `protocol_hash` | string/SHA256 | D 规则 |
| `family_alpha` | float | Round 冻结值 |
| `m` | int 1..2 | 冻结 Holdout 成员数，失败后不缩小；禁止 0 |
| `alpha_candidate` | float | 必须等于 `family_alpha / m` |
| `candidate_results` | list[`AdjustedCandidateResult`] | 全部 Holdout 成员 |
| `recommended_candidate_id` | UUID/null | 最多一个推荐；不删除其他 faster |
| `result_hash` | SHA256 | 规范化结果 Hash |
| `created_at` | datetime | 审计时间 |

每个 `AdjustedCandidateResult` 保存 Measurement Ref、正确性、adjusted CI、Stage 0 MDE、当前
Holdout Workload MDE、可信门限和 `faster|slower|inconclusive|invalid`。没有有效区间的失败
成员仍保留，且仍计入 `m`。

Scripted 可以使用显式 synthetic Fixture 重放选择、CI、FWER 和失败路径，但其成员只绑定
`M2ScriptedPhaseReceipt(status=not_measured)`，批级结果固定 `run_mode=scripted`、
`synthetic=true`。这些 verdict 是算法夹具输出，不得作为 HCU 性能结论、Formal Signoff 或发布依据。

若存在 `faster`，`recommended_candidate_id` 必须是 adjusted CI lower 最大的成员；lower 相同按
Candidate UUID 小写连字符字符串升序破同分。全部 `faster` 仍保留在 `candidate_results` 和
Round Evidence 中；推荐字段不触发 Baseline 更新、安装或发布。没有 `faster` 时该字段必须为空。

有效证据使用 `confidence = 1 - alpha_candidate` 的确定性 bootstrap。记 adjusted CI 为
`[lower, upper]`，`threshold=max(Stage 0 MDE, Holdout Workload MDE)`：

```text
lower > threshold   → faster
upper < -threshold  → slower
otherwise           → inconclusive
invalid bindings / correctness / cleanup / evidence → invalid
```

### 3.6 `RoundEvidenceBundle`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `schema_version` | literal `m2a-round-evidence-v1` | Bundle 版本 |
| `round_evidence_bundle_id` | UUID | Bundle ID |
| `round_id` / `task_id` | UUID | 权威对象 |
| `run_mode` | `scripted` 或 `formal` | 绑定真假 Authority |
| `terminal_reason` | `holdout_completed` 或 `no_promotable_candidate` | 决定最终化路径 |
| `candidate_family_hash` / `artifact_family_hash` | SHA256 | 始终存在的两个冻结家族 |
| `holdout_family_hash` | SHA256/null | 仅有晋级成员时存在 |
| `search_plan_hash` / `holdout_plan_commitment` | SHA256 | Search 计划与预先承诺 |
| `holdout_plan_hash` / `holdout_reveal_evidence_hash` | SHA256/null | 有晋级成员并完成 reveal 时存在 |
| `candidate_evidence` | list | 全部赢家、淘汰、失败和 invalid 成员 |
| `search_barrier_id` | UUID | 始终存在 |
| `holdout_barrier_id` / `multiple_comparison_id` | UUID/null | 零晋级时必须为空，其余必填 |
| `budget_ledger_hash` | SHA256 | 声明与实际消耗 |
| `evidence_index_uri` / `evidence_index_hash` | URI/SHA256 | 跨根递归索引与保留策略 |
| `summary` | object | 只保存派生摘要，不代替原始证据 |
| `synthetic` | bool | Scripted 必须 true；Formal 必须 false |
| `automatic_release_allowed` | literal false | Contract 与 DB 双重强制 |
| `created_at` | datetime | 审计时间 |

Evidence Index 必须列出每个外部 URI、内容 Hash、类型、生产者、保留责任和可访问性检查结果。
递归验证任何缺失或 Hash 不一致时 Bundle 为 `invalid`，不能只复制赢家摘要后签核。

Scripted 实现使用 `m2a-evidence-index-v1`。条目以稳定 role 标识 Search/Holdout Plan、Barrier、
Candidate/Artifact、正确性、原始输入、同时期 Baseline、预算、清理、失败和 FWER 证据；子条目
允许递归分组，但全树 role、URI 和跨角色 Hash 必须唯一。Verifier 实际重读全部条目并独立复算
SHA256，索引声明的 `accessibility_status=verified` 不能代替真实读取。实现与运行方式见
[`m2-round-evidence.md`](m2-round-evidence.md)。

`terminal_reason=no_promotable_candidate` 时 Holdout Family/Plan reveal、Holdout Barrier 和 FWER
引用必须全部为空；其他 Candidate、Search、失败、预算与清理证据仍必须完整。
`terminal_reason=holdout_completed` 时这四类引用必须全部非空。`run_mode=scripted` 与
`synthetic=true`、`run_mode=formal` 与 `synthetic=false` 也是不可分割的判别联合，不能只在
API 层检查。

### 3.7 `RoundSignoffIntent` 与 `RoundSignoff`

Signoff 不是一次“写数据库再尽力写文件”的操作。首先创建 durable Intent：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `signoff_intent_id` / `round_signoff_id` | UUID | 由幂等输入确定性生成 |
| `round_id` / `round_evidence_bundle_id` | UUID | 锁定最终 Formal Evidence |
| `decision` / `actor` / `reason` | string | 人工输入 |
| `decision_at` | datetime | 首次 Intent 事务固定，重放不改变 |
| `input_digest` | SHA256 | 覆盖全部签核输入 |
| `idempotency_key` | string | 完全相同输入重放 |
| `state` | `preparing` / `artifact_published` / `finalized` / `failed` | Reconcile 权威 |

Intent 创建事务同时写 outbox，但不推进 Round 终态。Publisher 使用 Intent 中固定的 Signoff ID
和 `decision_at` 生成规范化 `signoff-decision.json`，经临时文件和原子 rename 发布到内容寻址
存储；相同输入重复发布得到相同 Hash。Finalizer 在新事务中重新读取 Artifact、复算 Hash、
锁定 Intent/Round 并写最终 `RoundSignoff`、审计事件和终态。任何崩溃点由 Reconcile 按 Intent
状态重放；孤儿 Artifact 可复用但不能单独证明签核完成。

最终 `RoundSignoff` 字段：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `round_signoff_id` | UUID | 签核 ID |
| `round_id` | UUID | 每轮唯一 |
| `round_evidence_bundle_id` | UUID | 必须是该 Round 最终 Bundle |
| `decision` | `approved` 或 `rejected` | 人工决定 |
| `actor` / `reason` | string | 审计信息 |
| `idempotency_key` | string | 完全相同输入重放 |
| `decision_artifact_uri` / `decision_artifact_hash` | URI/SHA256 | 内容寻址 `signoff-decision.json` |
| `created_at` | datetime | 审计时间 |

`signoff-decision.json` 规范化保存 Round、EvidenceBundle、适用的 Family Hash、decision、
actor、reason、idempotency key 和 Intent 固定时间。数据库行与 Artifact 必须互相引用且 Hash
一致。只有 `run_mode=formal`、`synthetic=false`、Artifact 可读且 Hash 复核通过才能 Finalize；
批准仍保持 `automatic_release_allowed=false`，不修改 Baseline Epoch。

## 4. Budget Contract

`RoundBudget` 至少声明：

```text
max_candidates
max_build_attempts
max_correctness_attempts
max_search_samples
max_holdout_samples
max_wall_seconds
max_exclusive_lease_seconds
```

每个 Job Attempt 先创建 `RoundBudgetReservation`：`reservation_id`、Round/Job/Attempt、Candidate、
Phase、各 charge kind 的 planned amount 和状态。`RoundBudgetLedgerEntry` 是追加写事件，包含
`reservation_id`、`entry_type=reserve|settle|release`、reserved/actual amount、Lease 持有时间、
Harness 有效时间、原始使用证据 Hash 和事件自己的幂等键。

- `reserve`：排队前锁定 Round，按当前 reserved + consumed 原子检查并占用预算；
- `settle`：Job 已执行后写入实际消耗并释放未使用的 reservation；
- `release`：仅用于尚未执行的取消，或 Reconcile 确认不可能执行的 reservation；
- 同一 reservation 的 `settle` 与 `release` 互斥；迟到 settle、重复 event 或跨 Attempt 引用被拒绝；
- Job 重试创建新的 Attempt 和 reservation，不复用上一次收费身份。

超预算保留 `budget_exhausted` 证据，不缩减已冻结的正式采样计划。即使清理使实际 Lease 时间
超过预留，也如实 settle，并阻止后续 Job；不得截断证据来伪装未超预算。

硬预算使用 exclusive Lease 实际持有时间，包含失败恢复和释放前清理；Harness 有效时间同时
记录供利用率与操作成本分析，不替代或放宽硬预算。

## 5. 持久化草案

迁移文件编号和文件名在 ADR Accepted 后按仓库当时的真实迁移序列分配；本草案不预占或猜测
编号。拟新增表和关键约束如下：

| 表 | 关键唯一/不可变约束 |
| --- | --- |
| `search_rounds` | `round_id`、`idempotency_key`；一个 Task 一个活动 Round；Candidate/Artifact Hash 只写一次，Holdout Hash 仅晋级时写一次 |
| `round_candidates` | unique `(round_id,candidate_id)`、`(round_id,round_candidate_id)`、`(round_id,ordinal)`、`(round_id,candidate_source_hash)`、`(round_id,source_package_hash)`、`(round_id,source_manifest_hash)`、`idempotency_key` |
| `round_plans` | unique `(round_id,phase)`；通用表只保存 commitment 与 D Authority 引用，Holdout 内容/nonce 位于受保护 Store，对非 D 角色不可读 |
| `round_measurements` | unique `(round_id,candidate_id,phase)`、`measurement_id`、`raw_evidence_hash`、`baseline_sample_set_hash` |
| `round_barriers` | unique `(round_id,phase)`、`idempotency_key`；关闭后不可修改成员 |
| `multiple_comparison_results` | unique `round_id`、`holdout_barrier_id`、`result_hash` |
| `round_budget_reservations` | unique `(job_id,attempt)` 和 `reservation_id`；状态转换检查约束只允许一个终态 |
| `round_budget_ledger` | append-only；unique `(reservation_id,entry_type)`、`(round_id,idempotency_key)`，并以 partial unique `reservation_id WHERE entry_type IN ('settle','release')` 强制二选一 |
| `round_evidence_bundles` | unique `round_id`、`evidence_index_hash`；Scripted 强制 synthetic=true，Formal 强制 false，automatic release 始终 false |
| `round_signoff_intents` | unique `round_id`、`idempotency_key`、`input_digest`；保存确定性 ID、固定输入和 Intent 状态 |
| `outbox_events` | unique `(aggregate_type,aggregate_id,event_type)`；引用 Signoff Intent，保存 payload Hash、发布状态和重试信息 |
| `round_signoffs` | unique `round_id`、`idempotency_key`、`decision_artifact_hash`；只接受 finalized Intent |

必须使用 PostgreSQL 集成测试验证 Barrier、Budget、Intake Close、Signoff 并发和 Outbox
Reconcile；SQLite 单测不能替代 `SELECT ... FOR UPDATE`、partial unique index、互斥终态或
事务冲突语义。

旧 M1 表不迁移、不 UPDATE、不复用 `manual_candidate_signoffs`。通用 Candidate、Artifact、
Measurement 可通过外键被 Round 引用，但其既有行保持不可变。

## 6. API 草案

本节仍是 SearchRound 权威写接口。面向操作者的 Profile、Plan Preview、Summary、CLI 和 Web
只能作为这一层的受控外观，不能创建第二套 Round 状态或直接写数据库；拟议接口与分阶段
验收见 [M2 操作面与易用性建设计划](m2-operability-plan.md)，字段草案见
[M2 Operator Contract 草案](m2-operator-contract-draft.md)。

SearchRound 权威写接口由 Operator Facade 的服务身份组合调用；普通 CLI/Web 不能绕过 Preview 和
StartIntent 直接串联这些接口：

| Method | Path | 作用 |
| --- | --- | --- |
| POST | `/v1/search-rounds` | 使用全局 `idempotency_key` 按 `run_mode` 创建或重放 synthetic Scripted / Formal Round |
| GET | `/v1/search-rounds/{round_id}` | 读取 Round 权威与服务身份 |
| GET | `/v1/search-rounds/{round_id}/summary` | 读取成员、Job、Barrier、Budget、Evidence、Signoff |
| POST | `/v1/search-rounds/{round_id}/candidates` | 仅 StartIntent Reconciler 在 Intake Open 时按冻结成员登记 Candidate |
| POST | `/v1/search-rounds/{round_id}/intake-close` | 原子关闭 Intake 并冻结 Candidate Family |
| POST | `/v1/search-rounds/{round_id}/cancel` | 仅在运行 Job 已清理、Budget 已终态时取消排队 Job；不补造清理证据 |
| POST | `/v1/search-rounds/{round_id}/signoff` | 仅 Formal：创建/重放 durable Signoff Intent；不提前推进终态 |

面向权威组件，不提供给 Candidate Generator：

| Method | Path | 调用者 |
| --- | --- | --- |
| POST | `/v1/search-rounds/{round_id}/artifact-family:freeze` | A/C Build 汇总器 |
| POST | `/v1/search-rounds/{round_id}/barriers/search:close` | A + D Search 权威 |
| POST | `/v1/search-rounds/{round_id}/holdout-plan:reveal` | D 授权揭示并验证 nonce commitment |
| POST | `/v1/search-rounds/{round_id}/barriers/holdout:close` | A + D Holdout 权威 |
| POST | `/v1/search-rounds/{round_id}/multiple-comparison` | D FWER 组件 |
| POST | `/v1/search-rounds/{round_id}/evidence-bundles` | D Round Evidence 组件 |
| POST | `/v1/search-rounds/{round_id}/scripted:finalize` | A Scripted Finalizer；只接受 synthetic Bundle，不创建 Signoff |
| POST | `/v1/search-rounds/{round_id}/signoff:finalize` | A Signoff Finalizer；只接受已发布且复核的 Artifact |
| POST | `/v1/search-rounds/{round_id}/reconcile` | A 只读恢复审计；核对 Authority 图并返回唯一安全的下一动作 |

是否保留这些内部 HTTP 路径由实现评审决定；即使改为 Repository 方法，调用权限和幂等语义
不变。Worker 仍通过通用 Claim/Complete/Fail API 提交原始 Job 结果，不能直接推进 Round、
关闭 Barrier、写 FWER 或 Signoff。

所有可写响应必须返回服务 `source_commit`、`contract_version`、`adapter_profile` 和 Round
`version`；客户端在下一次写入前核对，避免把旧端口实例当成当前 Contract。

## 7. 稳定错误码草案

| HTTP | code | 条件 | retryable |
| ---: | --- | --- | --- |
| 409 | `m2a_not_approved` | ADR/Profile/项目授权尚未开放 | false |
| 409 | `project_mode_not_allowed` | 不是允许的 Formal Stage 0 模式 | false |
| 409 | `round_intake_closed` | 关闭后新增或替换 Candidate | false |
| 409 | `round_candidate_count_mismatch` | Intake Close 时不是声明的 2–4 个 | false |
| 409 | `round_candidate_package_invalid` | Package Store、Package/Manifest/File Hash 或 Authority 绑定无效 | false |
| 409 | `round_candidate_id_conflict` | Manifest Candidate ID 已被其他输入占用 | false |
| 409 | `round_family_hash_mismatch` | Candidate Family 绑定漂移 | false |
| 409 | `artifact_family_hash_mismatch` | Artifact/失败家族漂移 | false |
| 409 | `holdout_family_hash_mismatch` | 晋级族或 `m` 漂移 | false |
| 409 | `holdout_commitment_mismatch` | 揭示的 nonce/Plan 不能重建预先 commitment | false |
| 409 | `holdout_plan_not_revealed` | Holdout 测量前尚未完成受控揭示 | true |
| 409 | `holdout_plan_authority_unavailable` | 冻结的 D Store/协议身份不可用或漂移 | true |
| 403 | `holdout_reveal_lease_invalid` | Reveal Lease 过期、跨 Worker/Round 或 Fencing 不符 | false |
| 409 | `round_artifact_frozen` | Search 后尝试重建或换 Artifact | false |
| 409 | `round_barrier_not_ready` | 仍有成员未到终态或证据缺失 | true |
| 409 | `round_barrier_already_closed` | 不同输入重复关闭 | false |
| 409 | `phase_evidence_reused` | Search/Holdout Measurement、URI 或 Hash 复用 | false |
| 409 | `baseline_sample_reused` | 跨 Candidate/Phase 复用同时期 Baseline | false |
| 403 | `holdout_plan_forbidden` | 未授权角色读取 Holdout 内容 | false |
| 409 | `round_budget_exhausted` | 声明预算不足以排下一 Job | false |
| 409 | `round_signoff_evidence_mismatch` | Signoff 未绑定最终 Round Evidence | false |
| 409 | `round_signoff_artifact_unavailable` | Finalizer 无法重读或重哈希 Signoff Artifact | true |
| 409 | `synthetic_round_not_signable` | Scripted/synthetic Round 调用 Formal Signoff | false |
| 409 | `service_identity_mismatch` | 客户端期望 Commit/Contract 与服务不一致 | false |

已有 `stale_claim_token`、`stale_fencing_token`、`target_not_ready`、`adapter_unavailable` 继续
复用。通用 `conflict` 只作为未知兼容兜底；上述可预期控制流错误必须返回稳定 code，不能让
客户端解析英文 message。

## 8. Contract 测试清单

### 无数据库

- 所有模型拒绝 extra field、错误 Hash、错误 UUID、Candidate 数越界和非 Overlay/HIP 输入；
- Package Store Authority 不可由 UI 覆盖；Manifest/文件重哈希及 Candidate、Hotspot、Baseline、
  Replacement Point、Candidate Kind 绑定任一失败都拒绝 Intake；
- Candidate/Artifact 及适用的 Holdout Family Hash 对排序无关，但对字节、成员、失败证据或 `m` 变化敏感；
- Candidate Family Hash 对 Package Store/Package Hash、Manifest Version/Hash、ordinal、Candidate Kind 任一变化敏感；
- Barrier 拒绝 phase/outcome、成员数和 promoted 列表的非法组合；
- Formal 只接受 business Candidate，Scripted 只接受 fixture Candidate；
- Holdout `RoundMeasurementRef` 缺 `holdout_family_hash` 时失败；Search 带该字段时失败；
- nonce/Plan 不能重建 commitment、普通无盐 Hash 或未 reveal 的 Holdout Measurement 被拒绝；
- Formal 明文 Holdout Plan/nonce 进入通用表、环境变量或无 ACL Store 时失败；跨 Worker/Round、
  过期或 Fencing 漂移的 Reveal Lease 被拒绝；
- Bonferroni `m=0`、`alpha_candidate != family_alpha/m`、缺少失败成员或缩小 `m` 时失败；
- 调整后 CI 对称产生 faster/slower/inconclusive，证据无效才产生 invalid；
- 多个 faster 全部保留且只推荐 adjusted lower 最大者；同分按 Candidate UUID 升序确定性重放；
- 零晋级 Bundle 必须缺 Holdout/FWER 引用，普通 Bundle 缺任一引用时失败；
- Scripted Bundle 必须 synthetic，Formal Bundle 必须 non-synthetic；Synthetic Signoff 被拒绝，
  Scripted 只能进入 `scripted_completed`；
- M1 v1 已签核 Fixture 逐字节回放通过，M2 模型不修改解析结果。

### PostgreSQL

- 两个请求并发 Intake Close 只生成一个 Candidate Family；
- 创建第 1～N 个 Candidate 或 RoundCandidate 后崩溃、Intake Close 前后崩溃和并发 Reconcile
  只收敛到同一组确定性成员；未 finalized Round 不得 Build 或排 Job；
- 两个组件并发 Close Barrier 只生成一个逻辑 Barrier；
- 迟到 Worker、旧 Claim/Fencing Token 和关闭后 Candidate 写入均被拒绝；
- Budget reserve/settle/release 事件幂等；并发 settle/release 只能一个提交成功，失败重试不重复收费；
- Search/Holdout Measurement 和 Baseline Sample Set 的唯一约束不可绕过；
- Signoff 在 Intent 后、Artifact 发布后、Finalize 前各点故障均可 Reconcile；终态前 Artifact 必须可读且 Hash 一致；
- 从现有迁移升级后，已签核 M1 Task/Evidence/Signoff 内容和回放结果不变。

### Scripted Round

2–4 个 Fixture 覆盖 known faster、slower、inconclusive、invalid、零晋级、Build 失败、
Correctness 失败和预算耗尽。Fixture 只能验证控制流/算法，必须标记 synthetic，Formal
Signoff API 必须拒绝它。

## 9. A/B/C/D Review 签字表

项目所有者确认四线对 PR #55 的 Proposed 设计无阻塞，并代为登记 `accepted-for-draft`。该记录
不表示 reviewer 曾在对应 Issue 单独留言；最终实现仍须按各行职责接受 CODEOWNER Review。

| Review | 必须确认的内容 | 状态 | Reviewer / 日期 |
| --- | --- | --- | --- |
| A 控制面 | 状态、事务、幂等、Budget、Barrier、服务身份和 Signoff | Accepted for draft | tianlinyang77 / #56 / 2026-08-26 |
| B 测量 | 唯一 Harness、Phase 隔离、同时期 Baseline、Lease 消耗和清理证据 | Accepted for draft | lvj-repox / #57 / 2026-08-26 |
| C 构建 | 人工 Intake、Candidate/Artifact 与条件性 Holdout Family Hash、Worktree、Artifact 冻结和失败证据 | Accepted for draft | reverie-hub / #58 / 2026-08-26 |
| D 判定 | Plan 隔离、晋级规则、Bonferroni、失败计入 `m` 和 Round Evidence | Accepted for draft | dddddddxl / #59 / 2026-08-26 |
| 项目所有者 | 只批准 M2a 无 HCU Scripted 代码实现 | Authorized | #44 / 2026-08-26 |
| 项目所有者 | 另行批准一次 Formal HCU 窗口 | Pending | — |

ADR-0009 已进入 Accepted，并获得 M2a 无 HCU Scripted 实现授权。“批准 M2a Scripted 代码
实现”与“批准 M2a Formal HCU 运行”仍是两个决定；后者继续 Pending。

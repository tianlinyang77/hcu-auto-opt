# M2a SearchRound Contract 草案

- 状态：Draft / Non-runnable
- 依据：[ADR-0009](adr/0009-m2a-round-barrier.md)（Proposed）
- 适用范围：同一 Hotspot 下 2–4 个人工 Python/Triton startup Overlay Candidate
- 当前授权：文档、Contract 草案和无 HCU 测试设计

## 1. 权威边界

本草案定义 M2a 的接口形状，不代表这些模型、表、API 或状态已经实现。ADR-0009 未经 A/B/C/D
评审和项目所有者批准前：

- 不把草案类型加入可创建真实任务的 Adapter Profile；
- 不创建 M2a Formal Task，不运行 HCU 测量；
- 不运行 Agent、Apex Generator、Beam Search 或自动调参；
- 不改变已签核的 M1 v1 Schema、数据或回放语义；
- 不开放自动安装、Baseline 提升、M3 E2E 或生产发布。

M2a 继续继承 Formal Stage 0 的 `DEGRADED_MANUAL_INTAKE` 模式。人工 Candidate Intake 是能力
边界的一部分，不得用自动发现结果冒充人工确认的 Hotspot 或源码包。

## 2. 冻结顺序

```text
Create Round authority
→ Freeze Search Plan + sealed Holdout Plan hash
→ Intake 2–4 immutable Candidates
→ Intake Close / candidate_family_hash
→ Build all members / artifact_family_hash
→ Correctness terminal states
→ Search measurements / Search Barrier
→ Freeze promoted holdout_family_hash and m
→ Holdout measurements / Holdout Barrier
→ Bonferroni FWER
→ RoundEvidenceBundle
→ Human Round Signoff
```

`candidate_family_hash`、`artifact_family_hash` 和 `holdout_family_hash` 是三个不同时间点的
权威：

| Hash | 冻结时间 | 规范化输入 | 回答什么 |
| --- | --- | --- | --- |
| Candidate Family | Intake Close | 排序后的 Candidate ID、Source Hash、Hotspot、Replacement Point、意图 | 提交了哪些源码候选 |
| Artifact Family | 所有 Build 终态 | 排序后的 Candidate + Artifact ID/Hash；失败成员使用终态与失败证据 Hash | 实际生成或拒绝了哪些制品 |
| Holdout Family | Search Barrier Close | 排序后的 promoted Candidate、Artifact Hash、`m`、选择规则 Hash | 哪些不可变制品进入最终比较 |

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
| `state` | RoundState | 只由 A 控制面推进 |
| `project_mode` | literal `degraded_manual_intake` | 不扩大 Stage 0 能力 |
| `target_snapshot_id` | UUID | 固定 Target |
| `stage0_run_id` | UUID | 必须是 Formal/finalized |
| `stage0_protocol_hash` | SHA256 | 绑定测量 Authority |
| `baseline_epoch_id` | UUID | 同一逻辑基线 |
| `hotspot_id` | UUID | 同一人工业务 Hotspot |
| `replacement_point` | string | 全家族相同替换点 |
| `workload_id` / `workload_hash` | string / SHA256 | 冻结业务 Workload |
| `configuration_hash` | SHA256 | 冻结运行配置 |
| `image_digest` | SHA256 digest | 禁止 Tag 漂移 |
| `adapter_profile` | string | 必须是 opt-in Real M2a Profile |
| `declared_candidate_count` | int 2..4 | Intake Close 必须精确满足 |
| `max_promoted` | int 1..2 | 且不大于候选数 |
| `family_alpha` | float 0..1 | 由 D 协议冻结 |
| `search_plan_hash` | SHA256 | Search 内容承诺 |
| `holdout_plan_hash` | SHA256 | Intake 前提交，内容由 D 保护 |
| `selection_rule_hash` | SHA256 | Search 晋级规则 |
| `budget` | `RoundBudget` | 声明硬上限 |
| `candidate_family_hash` | SHA256/null | Intake Close 后只写一次 |
| `artifact_family_hash` | SHA256/null | Build Barrier 后只写一次 |
| `holdout_family_hash` | SHA256/null | Search Barrier 后只写一次 |
| `version` | positive int | 乐观并发控制 |
| `created_at` / `intake_closed_at` | datetime | 审计时间 |

拟议 `RoundState`：

```text
intake_open | intake_closed | building | correctness
| search_measuring | search_barrier | holdout_measuring | holdout_barrier
| awaiting_signoff | completed | rejected | cancelled
```

### 3.2 `RoundCandidate`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `round_candidate_id` | UUID | Round 内成员 ID |
| `round_id` / `candidate_id` | UUID | 指向 Round 和通用 Candidate |
| `ordinal` | int 0..3 | 只用于稳定排序，不表示排名 |
| `source_hash` | SHA256 | Intake 时冻结 |
| `optimization_intent` | string | 一个清晰假设 |
| `replacement_point` | string | 必须等于 Round 值 |
| `track` | literal `triton` | M2a 不开放 HIP/配置混批 |
| `release_mode` | literal `overlay` | startup Overlay |
| `candidate_kind` | literal `business` | Formal 不接受 fixture |
| `artifact_id` / `artifact_hash` | UUID/SHA256/null | Build 成功后绑定 |
| `terminal_failure_code` | string/null | Build/Correctness/预算失败 |
| `failure_evidence_hash` | SHA256/null | 失败成员也进入 Artifact Family |
| `state` | RoundCandidateState | Candidate 在本 Round 的状态 |
| `idempotency_key` | string | 输入完全相同才可重放 |

同一 Round 禁止重复 `candidate_id`、`source_hash`、`ordinal` 或幂等键。Intake Close 后以上输入
字段不可更新；Search 开始后 Artifact ID/Hash 也不可替换或重建。

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
| `phase_plan_hash` | SHA256 | 等于对应 Search/Holdout Plan Hash |
| `baseline_sample_set_hash` | SHA256 | 该 Candidate/Phase 自己的同时期 Baseline 身份 |
| `lease_id` / `resource_id` / `fencing_token` | UUID/UUID/int | 控制面权威资源绑定 |
| `status` | literal `measured` | 只有完整采集才建立 Ref；B 不写快慢 verdict |
| `created_at` | datetime | 审计时间 |

`measurement_id`、`raw_evidence_hash` 和 `baseline_sample_set_hash` 在所有 Round/Phase/Candidate
间唯一，避免复制同一数据后改标签。Holdout 还必须证明其输入、进程、缓存 Namespace 与 Search
不同。采集、清理或证据生产失败时不创建伪装成完整测量的 Ref，失败证据直接进入
`BarrierMemberResult`；D 可据此给出 `invalid`，该成员仍保留在冻结家族中。

### 3.4 `RoundBarrierResult`

Barrier 是批级权威，单个 Worker 不能自行宣告关闭。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `barrier_id` | UUID | Barrier ID |
| `round_id` | UUID | 所属 Round |
| `phase` | `search` 或 `holdout` | 每轮每 Phase 唯一 |
| `input_family_hash` | SHA256 | Search 用 Artifact Family；Holdout 用 Holdout Family |
| `expected_member_count` | int | 冻结成员数 |
| `members` | list[`BarrierMemberResult`] | 按 Candidate ID 排序的全部终态 |
| `rule_version` / `rule_hash` | string/SHA256 | 关闭与选择规则 |
| `promoted_candidate_ids` | UUID list | 仅 Search；最多 `max_promoted` |
| `input_summary_hash` | SHA256 | 全部成员输入摘要 |
| `closed_by` | authority identity | A/D 权威组件，不是测量 Worker |
| `closed_at` | datetime | 关闭时间 |
| `idempotency_key` | string | 并发关闭返回同一对象 |

`BarrierMemberResult` 必须为每个冻结成员保存 Candidate/Artifact、正确性、Measurement Ref 或
失败证据、预算状态和终态。成员缺失、仍在运行、Family Hash 不符或迟到写回时，Barrier
保持未关闭并返回稳定错误。

### 3.5 `MultipleComparisonResult`

M2a 固定使用 Bonferroni FWER，不允许同轮临时切换 FDR。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `multiple_comparison_id` | UUID | 批级裁决 ID |
| `round_id` / `holdout_barrier_id` | UUID | 绑定已关闭 Holdout Barrier |
| `holdout_family_hash` | SHA256 | 冻结最终比较家族 |
| `method` | literal `bonferroni_fwer` | 协议方法 |
| `protocol_version` / `protocol_hash` | string/SHA256 | D 规则 |
| `family_alpha` | float | Round 冻结值 |
| `m` | int | 冻结 Holdout 成员数，失败后不缩小 |
| `alpha_candidate` | float | 必须等于 `family_alpha / m` |
| `candidate_results` | list[`AdjustedCandidateResult`] | 全部 Holdout 成员 |
| `recommended_candidate_id` | UUID/null | 最多一个推荐；不删除其他 faster |
| `result_hash` | SHA256 | 规范化结果 Hash |
| `created_at` | datetime | 审计时间 |

每个 `AdjustedCandidateResult` 保存 Measurement Ref、正确性、adjusted CI、Stage 0 MDE、当前
Holdout Workload MDE、可信门限和 `faster|slower|inconclusive|invalid`。没有有效区间的失败
成员仍保留，且仍计入 `m`。

### 3.6 `RoundEvidenceBundle`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `schema_version` | literal `m2a-round-evidence-v1` | Bundle 版本 |
| `round_evidence_bundle_id` | UUID | Bundle ID |
| `round_id` / `task_id` | UUID | 权威对象 |
| `candidate_family_hash` / `artifact_family_hash` / `holdout_family_hash` | SHA256 | 三个冻结家族 |
| `search_plan_hash` / `holdout_plan_hash` | SHA256 | 两份计划 |
| `candidate_evidence` | list | 全部赢家、淘汰、失败和 invalid 成员 |
| `search_barrier_id` / `holdout_barrier_id` | UUID | 两次批级 Barrier |
| `multiple_comparison_id` | UUID | FWER 结果 |
| `budget_ledger_hash` | SHA256 | 声明与实际消耗 |
| `evidence_index_uri` / `evidence_index_hash` | URI/SHA256 | 跨根递归索引与保留策略 |
| `summary` | object | 只保存派生摘要，不代替原始证据 |
| `synthetic` | literal false | Formal Round 禁止 synthetic |
| `automatic_release_allowed` | literal false | Contract 与 DB 双重强制 |
| `created_at` | datetime | 审计时间 |

Evidence Index 必须列出每个外部 URI、内容 Hash、类型、生产者、保留责任和可访问性检查结果。
递归验证任何缺失或 Hash 不一致时 Bundle 为 `invalid`，不能只复制赢家摘要后签核。

### 3.7 `RoundSignoff`

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

`signoff-decision.json` 规范化保存 Round、EvidenceBundle、三个 Family Hash、decision、actor、
reason、idempotency key 和时间。数据库行与 Artifact 必须互相引用且 Hash 一致。批准仍保持
`automatic_release_allowed=false`，不修改 Baseline Epoch。

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

`RoundBudgetLedgerEntry` 是追加写：`round_id`、`job_id`、Candidate、Phase、charge kind、
reserved amount、actual amount、lease-held seconds、harness-active seconds、idempotency key 和
原始使用证据 Hash。控制面排队前做原子 reserve，Job 终态后做一次 settle；超预算保留
`budget_exhausted` 证据，不缩减已冻结的正式采样计划。

硬预算使用 Lease 持有时间，Harness 有效时间同时记录供效率分析。该选择仍待 ADR 评审确认。

## 5. 持久化草案

迁移文件编号和文件名在 ADR Accepted 后按仓库当时的真实迁移序列分配；本草案不预占或猜测
编号。拟新增表和关键约束如下：

| 表 | 关键唯一/不可变约束 |
| --- | --- |
| `search_rounds` | `round_id`；一个 Task 一个活动 Round；三个 Family Hash 只允许从 null 写一次 |
| `round_candidates` | unique `(round_id,candidate_id)`、`(round_id,ordinal)`、`(round_id,source_hash)`、`idempotency_key` |
| `round_plans` | unique `(round_id,phase)`；Holdout 内容 URI 对非 D 角色不可读，Hash 可读 |
| `round_measurements` | unique `(round_id,candidate_id,phase)`、`measurement_id`、`raw_evidence_hash`、`baseline_sample_set_hash` |
| `round_barriers` | unique `(round_id,phase)`、`idempotency_key`；关闭后不可修改成员 |
| `multiple_comparison_results` | unique `round_id`、`holdout_barrier_id`、`result_hash` |
| `round_budget_ledger` | append-only；unique `(round_id,idempotency_key)` 和一次 reserve/settle 语义 |
| `round_evidence_bundles` | unique `round_id`、`evidence_index_hash`；Formal 强制 synthetic/release 为 false |
| `round_signoffs` | unique `round_id`、`idempotency_key`、`decision_artifact_hash` |

必须使用 PostgreSQL 集成测试验证 Barrier、Budget、Intake Close 和 Signoff 并发；SQLite 单测
不能替代 `SELECT ... FOR UPDATE`、partial unique index 或事务冲突语义。

旧 M1 表不迁移、不 UPDATE、不复用 `manual_candidate_signoffs`。通用 Candidate、Artifact、
Measurement 可通过外键被 Round 引用，但其既有行保持不可变。

## 6. API 草案

面向操作者：

| Method | Path | 作用 |
| --- | --- | --- |
| POST | `/v1/search-rounds` | 创建绑定 Formal Stage 0 的 M2a Round |
| GET | `/v1/search-rounds/{round_id}` | 读取 Round 权威与服务身份 |
| GET | `/v1/search-rounds/{round_id}/summary` | 读取成员、Job、Barrier、Budget、Evidence、Signoff |
| POST | `/v1/search-rounds/{round_id}/candidates` | 仅 Intake Open 时登记人工 Candidate |
| POST | `/v1/search-rounds/{round_id}/intake-close` | 原子关闭 Intake 并冻结 Candidate Family |
| POST | `/v1/search-rounds/{round_id}/cancel` | 停止排新 Job，保留已有证据并执行清理 |
| POST | `/v1/search-rounds/{round_id}/signoff` | 绑定最终 Round Evidence 人工签核 |

面向权威组件，不提供给 Candidate Generator：

| Method | Path | 调用者 |
| --- | --- | --- |
| POST | `/v1/search-rounds/{round_id}/artifact-family:freeze` | A/C Build 汇总器 |
| POST | `/v1/search-rounds/{round_id}/barriers/search:close` | A + D Search 权威 |
| POST | `/v1/search-rounds/{round_id}/barriers/holdout:close` | A + D Holdout 权威 |
| POST | `/v1/search-rounds/{round_id}/multiple-comparison` | D FWER 组件 |
| POST | `/v1/search-rounds/{round_id}/evidence-bundles` | D Round Evidence 组件 |

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
| 409 | `round_family_hash_mismatch` | Candidate Family 绑定漂移 | false |
| 409 | `artifact_family_hash_mismatch` | Artifact/失败家族漂移 | false |
| 409 | `holdout_family_hash_mismatch` | 晋级族或 `m` 漂移 | false |
| 409 | `round_artifact_frozen` | Search 后尝试重建或换 Artifact | false |
| 409 | `round_barrier_not_ready` | 仍有成员未到终态或证据缺失 | true |
| 409 | `round_barrier_already_closed` | 不同输入重复关闭 | false |
| 409 | `phase_evidence_reused` | Search/Holdout Measurement、URI 或 Hash 复用 | false |
| 409 | `baseline_sample_reused` | 跨 Candidate/Phase 复用同时期 Baseline | false |
| 403 | `holdout_plan_forbidden` | 未授权角色读取 Holdout 内容 | false |
| 409 | `round_budget_exhausted` | 声明预算不足以排下一 Job | false |
| 409 | `round_signoff_evidence_mismatch` | Signoff 未绑定最终 Round Evidence | false |
| 409 | `service_identity_mismatch` | 客户端期望 Commit/Contract 与服务不一致 | false |

已有 `stale_claim_token`、`stale_fencing_token`、`target_not_ready`、`adapter_unavailable` 继续
复用。通用 `conflict` 只作为未知兼容兜底；上述可预期控制流错误必须返回稳定 code，不能让
客户端解析英文 message。

## 8. Contract 测试清单

### 无数据库

- 所有模型拒绝 extra field、错误 Hash、错误 UUID、Candidate 数越界和非 Overlay/HIP 输入；
- 三个 Family Hash 对输入排序无关，但对任一字节、成员、失败证据或 `m` 变化敏感；
- Holdout `RoundMeasurementRef` 缺 `holdout_family_hash` 时失败；Search 带该字段时失败；
- Bonferroni `alpha_candidate != family_alpha/m`、缺少失败成员或缩小 `m` 时失败；
- M1 v1 已签核 Fixture 逐字节回放通过，M2 模型不修改解析结果。

### PostgreSQL

- 两个请求并发 Intake Close 只生成一个 Candidate Family；
- 两个组件并发 Close Barrier 只生成一个逻辑 Barrier；
- 迟到 Worker、旧 Claim/Fencing Token 和关闭后 Candidate 写入均被拒绝；
- Budget reserve/settle 原子，失败重试不重复收费也不丢失实际消耗；
- Search/Holdout Measurement 和 Baseline Sample Set 的唯一约束不可绕过；
- Signoff 幂等重放返回同一 ID，不同输入冲突；
- 从现有迁移升级后，已签核 M1 Task/Evidence/Signoff 内容和回放结果不变。

### Scripted Round

2–4 个 Fixture 覆盖 known faster、slower、inconclusive、invalid、Build 失败、Correctness 失败和
预算耗尽。Fixture 只能验证控制流/算法，必须标记 synthetic，不能签成 Formal 性能成果。

## 9. A/B/C/D Review 签字表

ADR-0009 仍为 Proposed；下表默认未签署，不能用 PR 合入或 CI 绿灯代替 Owner 决定。

| Review | 必须确认的内容 | 状态 | Reviewer / 日期 |
| --- | --- | --- | --- |
| A 控制面 | 状态、事务、幂等、Budget、Barrier、服务身份和 Signoff | Pending | — |
| B 测量 | 唯一 Harness、Phase 隔离、同时期 Baseline、Lease 消耗和清理证据 | Pending | — |
| C 构建 | 人工 Intake、三个 Family Hash、Worktree、Artifact 冻结和失败证据 | Pending | — |
| D 判定 | Plan 隔离、晋级规则、Bonferroni、失败计入 `m` 和 Round Evidence | Pending | — |
| 项目所有者 | 只批准 M2a 代码实现，或另行批准一次 Formal HCU 窗口 | Pending | — |

只有 A/B/C/D 对 Contract 签署且项目所有者明确批准后，ADR 才能从 Proposed 进入 Accepted。
“批准 M2a 代码实现”与“批准 M2a Formal HCU 运行”必须分成两个决定。

# ADR-0009：M2a 使用人工有限候选的轮次级 Search/Holdout 与 Barrier

- 状态：Accepted / M2a Scripted implementation authorized
- 日期：2026-08-26
- 跟踪：GitHub Issue #44
- 接受记录：A/B/C/D 评审范围分别见 #56、#57、#58、#59；项目所有者确认四线无阻塞并接受本 ADR
- 实现授权：仅 M2a 无 HCU Scripted Contract、迁移、控制面、Fixture 与测试

## 背景

M1 已证明单人工 Candidate 能在固定 Target、Workload、Baseline 和 Adapter Profile 下完成
可信 Build、正确性、原始性能测量、独立裁决、EvidenceBundle 和人工签核。M1 的正式证据
已经被签核，现有 v1 Schema 和 `manual_candidate` 单候选语义由此形成兼容边界。

M2 需要回答的新问题不是“还能不能再跑一个 Candidate”，而是：一轮多个候选如何共享逻辑
基线但不池化同时期样本，如何隔离 Search 与 Holdout，如何在全部候选完成后进行批级统计
校正，以及失败、预算和人工签核如何按轮次收敛。把这些语义塞回 M1 Candidate 级流程会绕过
真正的 Barrier，也会破坏已签核 Contract。

## 决策

### 1. M2 分为 M2a 和 M2b

M2a 每轮固定只接受 2–4 个人工提供、内容不可变的业务 Candidate，硬上限为 4，Search 最多
提升 2 个。它们必须属于同一个
Hotspot、同一个 Replacement Point、同一个 Baseline Epoch，且继续限定为 Python/Triton
startup Overlay。M2a 继续运行在 Formal Stage 0 已批准的
`DEGRADED_MANUAL_INTAKE` 边界内，不把降级的 Profiler 能力伪装成自动发现能力。M2a 不运行
Agent、Beam Search 或自动调参。

M2b 才允许 Candidate Generator Adapter。是否进入 M2b 必须等 M2a 的 Scripted、PostgreSQL
和 Target Lock 闭环全部通过后另行评审；本 ADR 不批准 M2b 实现或真实运行。

### 2. 新建轮次权威，不修改 M1 单候选 Workflow

新增独立的 `search_round` Workflow 和 `SearchRound` 权威对象。M1 的
`manual_candidate`、状态机、数据库约束和 API 保持不变。一个 M2 Task 当前只运行一个
活动 Round；并行 Round 和跨 Hotspot 组合留到后续版本。

Round 在接收 Candidate 前固定 Target Snapshot、Stage0Run、Baseline Epoch、Hotspot、
Workload/Configuration Hash、Image Digest、Adapter Profile、统计协议和总预算。候选数达到
声明值后执行 Intake Close；关闭后禁止新增、替换或重建 Candidate。

Intake Close 对按 ordinal 排序后的 Candidate/RoundCandidate ID、Package Store/Package/Manifest
Hash、Manifest Version、Baseline/Candidate Source Hash、Hotspot、Replacement Point、Candidate
Kind 和优化意图计算 `candidate_family_hash`。全部 Build 到达
终态后，再对每个 Candidate 的 Artifact ID/Hash，或没有 Artifact 时的失败终态与失败证据
Hash，计算 `artifact_family_hash`。两者职责不同：前者证明“提交了哪些源码候选”，后者证明
“实际测量或拒绝了哪些不可变制品”。任何后续计划、Barrier 和 Evidence 都必须同时绑定这
两个 Hash；缺少 Artifact 的失败 Candidate 也不能从家族中删除。

### 3. M1 单次比较证据只读复用

`m1-kernel-performance-evidence-v1` 不改字段、不改语义。M2 每次 Baseline/Candidate 比较仍由
B 的唯一 Harness 产生这份可复算原始证据，并由新的 `RoundMeasurementRef` 额外绑定：

- `round_id`、`candidate_family_hash`、`artifact_family_hash` 和 Candidate；
- `phase=search|holdout`；
- `measurement_id`、原始 URI/Hash 和 Measurement Plan Hash；
- 该 Candidate 自己的同时期 Baseline 样本身份；
- Search Plan Hash、Holdout commitment/揭示后 Plan Hash 和本次 Lease/Fencing 权威。

M2 不重写或迁移已有 M1 Evidence。D 先复用单次比较验证器重读每份原始证据，再执行轮次级
选择和多重比较。

无 HCU Scripted 运行只验证编排，生成独立的 synthetic Phase Receipt，状态固定为
`not_measured`。它不能创建或序列化为 `RoundMeasurementRef`，也不能作为 Barrier 的
Measurement Ref 输入；正式 Ref 固定绑定 `m1-kernel-performance-evidence-v1` 且
`synthetic=false`。

### 4. Search 与 Holdout 在候选测量前冻结并隔离

D 在 Candidate Intake Close 前生成两份独立计划：

- Search Plan：允许 C 和开发人员知道，用于候选初筛；
- Holdout Plan：Search 前只发布带 256-bit 随机 nonce 的 commitment
  `SHA256(nonce || canonical_plan)`；具体 Shape/输入和 nonce 由 D 的
  `HoldoutPlanAuthority` 保存。普通业务表、Operator Facade、C/Agent 和 Search Worker 只保存或
  读取 commitment、Authority ID/Hash 与脱敏状态，不能读取计划内容或 nonce。

`HoldoutPlanAuthority` 是逻辑权限边界，不要求 MVP 拆成独立微服务。Scripted 使用显式 synthetic
Store；Formal 必须使用部署方 opt-in 的受保护内容寻址 Store、独立服务身份和文件/对象 ACL，
不能把明文计划放进通用数据库字段或进程环境变量。Search Barrier 完成后，D 才签发绑定
Round、Holdout Family、获授权测量 Worker、Lease/Fencing 和过期时间的一次性 Reveal Lease。
Worker 读取后必须重建 commitment，并记录 Plan Hash、Reveal Lease、actor、时间和访问结果。
普通无盐 Hash 不能作为保密承诺，避免小 Shape 空间被枚举。

Search 和 Holdout 使用不同输入、Measurement ID、进程、缓存 Namespace 和原始文件。
Candidate 进入 Search 后，源码、SourceSnapshot 和 Artifact Hash 永久冻结；不得根据 Search
结果重新编译后再进入 Holdout。任何 Search 样本都不能复制、聚合或改名后进入 Holdout。

### 5. 同一逻辑 Baseline，不池化同时期样本

同一 Round 的 Candidate 绑定同一个 Baseline Epoch，表示源码、配置和环境权威一致。但每个
Candidate 的 Search 和 Holdout 都使用自己的 Baseline-Candidate-Candidate-Baseline 采集组，
Baseline 样本不得跨 Candidate、跨 Phase 或跨时间窗口池化。报告中的每一行比较都携带自己的
Baseline Measurement 引用；可视化不得跨行连接或比较不同时间的 Baseline 曲线。

### 6. Barrier 是批级同步点

Search Barrier 只有在本轮全部 Candidate 到达 Build/Correctness/Search 的终态后才能关闭。
快 Candidate 必须等待慢 Candidate；失败、`invalid` 和预算耗尽也是终态证据，不能从候选族中
删除。D 使用 Search 数据和预注册规则最多提升 2 个 Candidate，Search 排名只是选择证据，
不是最终 `faster` 结论。

若没有 Candidate 可晋级，Search Barrier 以 `no_promotable_candidate` 关闭，Round 跳过
Holdout 和多重比较，直接生成包含全部失败/淘汰证据的 Round EvidenceBundle。Formal Round
等待人工签核，Scripted Round 经 synthetic 验证后进入独立终态。此路径不创建空 Holdout
Family，不计算 `m=0`，Holdout Barrier 和
MultipleComparison 引用必须为空。

Holdout 候选族在开始第一份 Holdout Measurement 前冻结。Holdout Barrier 等待该族全部终态，
随后一次性执行多重比较；单个 Candidate 不能绕过 Barrier 提前进入签核。进入冻结 Holdout
候选族后发生测量失败、清理失败、证据无效或预算耗尽的 Candidate 仍是该族成员，并以
`invalid` 或相应失败终态进入 Barrier。

### 7. M2a 使用保守的 Bonferroni FWER

M2a 每轮候选很少且结果将交给人工签核，因此选择 Family-Wise Error Rate，而不是 FDR。
Holdout 候选数 `m` 在 Holdout 开始前冻结，D 对每个候选使用
`alpha_candidate = alpha_family / m` 的确定性 bootstrap 置信区间。候选只有在以下条件全部
满足时才得到最终 `faster`：

`m` 取冻结 Holdout 候选族的成员数，不能因为某个 Candidate 后续失败、`invalid`、超预算或
缺少有效区间而缩小。失败成员仍消耗一次比较机会；这使多重比较校正保持保守且可重放。

1. 正确性为 `correct`；
2. Holdout 原始证据和清理证据有效；
3. Bonferroni 调整后的置信区间下界高于
   `max(Formal Stage 0 MDE, 当前 Holdout Workload MDE)`；
4. Artifact、Baseline、Target、Round、Phase 和计划绑定完整。

对绑定和清理均有效的 Holdout 证据，记调整后置信区间为 `[lower, upper]`，可信门限为上述
两个 MDE 的较大值：`lower > threshold` 为 `faster`，`upper < -threshold` 为 `slower`，
其余为 `inconclusive`。正确性、绑定、原始证据或清理无效时才是 `invalid`。若以后候选族
显著增大并考虑 BH-FDR，必须新增协议版本和 ADR，不能在同一结果族中临时切换算法。

全部 `faster` 结果都保留在 Round Evidence 和人工签核材料中，但系统最多推荐一个 Candidate：
选择 adjusted CI lower 最大者；若相同则按 Candidate UUID 小写连字符字符串升序确定性破同分。
推荐只是展示字段，不删除其他 `faster`，也不自动修改 Baseline 或发布。

### 8. 预算由声明上限和实际消耗共同约束

Round Budget 至少包含 Candidate、Build、Correctness、Search Sample、Holdout Sample、墙钟和
独占 HCU Lease 秒数上限。每个 Job 完成或失败后把实际消耗原子回写 Budget Ledger；控制面在
排队下一 Job 前再次检查。超预算使剩余 Candidate 进入有证据的 `budget_exhausted` 终态，不得
悄悄缩减正式采样计划或复用部分样本。

Budget Ledger 使用不可变事件：每个 Job Attempt 先生成唯一 `reservation_id` 和一条
`reserve`，正常终态追加一条 `settle`，启动前取消或 Reconcile 回收则追加一条 `release`。
`settle` 与 `release` 互斥，均引用原 reservation；不同事件使用不同幂等键。控制面排队前在
Round 行锁内计算 reserved + consumed 并原子 reserve，Job 终态后写实际消耗，避免重试重复
收费。

M2a 仍只有 Overlay Build 队列，不开放 REBUILD/HIP 队列。Correctness 使用当前串行 shared
资源语义，Performance/Holdout 使用 exclusive Lease；Barrier 和 D 裁决不占 HCU。

HCU 硬预算按 exclusive Lease 实际持有秒数执行，覆盖测量、失败恢复和释放前清理；Harness
有效测量秒数同时记录，只用于利用率和操作成本分析，不能替代 Lease 时间或放宽硬预算。

### 9. 轮次 EvidenceBundle 保存全家族，不只保存赢家

`RoundEvidenceBundle` 绑定 Round、Candidate Family Hash、Artifact Family Hash、Search Plan、
Holdout commitment/reveal、全部 Candidate/Artifact、所有正确性和 Measurement 引用、适用的
Barrier/FWER 结果、Budget Ledger、失败证据和清理证明。赢家、淘汰、失败、`inconclusive`
与 `invalid` 均不可删除。

Formal 人工 Signoff 接受或拒绝整份 Round Evidence。批准不会自动安装 Overlay、改变 Baseline Epoch、
触发 M3 E2E、开启生产灰度或把 `automatic_release_allowed` 改成 `true`。签核决定本身必须发布
内容寻址的 `signoff-decision.json`，同时保留 PostgreSQL 审计行。

签核采用 durable intent/outbox：控制面先在事务内锁定 Round、写入确定性 Signoff Intent 和
outbox；Publisher 幂等、原子地发布 Artifact；Finalizer 重新读取并校验 URI/Hash 后，才在
第二个事务内写最终 Signoff、审计事件并推进 Round 终态。任一崩溃点由 Reconcile 从 Intent
状态恢复；数据库未确认可读 Artifact 前不得把 Round 标记为 `completed/rejected`。

Scripted Round 与 Formal Round 共用控制流，但 Evidence 必须显式区分。Scripted Fixture 的
Round Evidence 固定 `synthetic=true`，只能验证状态、统计和故障路径，禁止调用 Formal
Signoff；Formal Finalize/Signoff 则强制 `synthetic=false`、真实 Authority 和 Real Adapter
provenance。Scripted Bundle 经递归校验后进入独立的 `scripted_completed` 终态，不创建 Signoff
Intent/Artifact，也不能进入 Formal 的 `awaiting_signoff/completed/rejected`。两种模式都强制
`automatic_release_allowed=false`。

## 拟议状态机

```text
Round:
intake_open → intake_closed → building → correctness
  → search_measuring → search_barrier
  → holdout_measuring → holdout_barrier
  → formal: awaiting_signoff → completed | rejected
  → scripted: scripted_completed

search_barrier → formal: awaiting_signoff: no_promotable_candidate
search_barrier → scripted: scripted_completed: no_promotable_candidate

Candidate:
proposed → building → built → correctness_running
  → search_running → search_waiting
  → promoted | search_rejected | invalid
  → holdout_running → round_waiting
  → round_accepted | round_rejected | inconclusive | invalid
```

状态名将在 Contract 评审中最终冻结。任何 Worker 仍只能提交自己的原始结果；Round 推进、
Barrier 关闭、FWER 和签核均由各自权威组件执行。

## 拟议持久化对象

| 对象 | 关键字段 |
| --- | --- |
| `search_rounds` | Round/Task/Baseline/Hotspot、状态、候选数、Family Hash、Search Plan、Holdout commitment/reveal、协议、Budget、版本 |
| `round_measurements` | Round/Candidate/Phase、Measurement、同时期 Baseline 身份、原始 URI/Hash |
| `round_barriers` | Phase、Family Hash、成员终态、关闭时间、规则版本、输入摘要 |
| `multiple_comparison_results` | Holdout Family、FWER 方法、family alpha、m、每候选调整结果 |
| `round_budget_reservations` / `round_budget_ledger` | Attempt reservation、reserve/settle/release 事件、实际消耗和原始证据 |
| `round_signoff_intents` / `round_signoffs` | durable intent/outbox、Round Evidence、decision、Signoff Artifact 和最终审计行 |

现有 `candidates.round_id` 和通用 Artifact/Evaluation 表可以引用新 Round，但 M1 的
`manual_candidate_signoffs` 不复用为 Round Signoff。

## M2a → M2b Go/No-Go

### 实现状态（2026-08-27）

无 HCU Scripted 路径已按本 ADR 接入 PostgreSQL：迁移 `0009_m2_round_authority.sql` 以
append-only 表保存 Search/Holdout Barrier、Holdout Reveal、Bonferroni Result 和
RoundEvidenceBundle；A Finalizer 在同一事务中重算 Budget Ledger Hash、调用 D verifier 递归
重建 Bundle，并只推进到 `scripted_completed`。Formal Store、HCU Target Lock 和人工 Signoff
仍未实现，不能据此越过下面的 Go/No-Go 条件。

只有以下条件全部满足才讨论 M2b：

- 2–4 个 Scripted Candidate 能覆盖 faster/slower/inconclusive/invalid 和预算失败；
- PostgreSQL 并发测试证明 Intake Close、Barrier Close、Budget 消耗和 Signoff 幂等；
- 零晋级能跳过 Holdout/FWER 并保存完整证据；Scripted Evidence 不能进入 Formal Signoff；
- Holdout commitment/reveal、Signoff 崩溃恢复和 Budget reserve/settle/release 故障注入通过；
- Candidate/Artifact Family Hash 篡改、Search 样本篡改、Holdout 泄漏、跨候选 Baseline 复用和 Barrier 提前关闭均被拒绝；
- Target Lock 上人工 Candidate Round 完整运行并健康清理；
- Round Evidence 可递归重哈希，且人工签核后仍保持自动发布关闭；
- A/B/C/D 和项目所有者共同签署 M2a 验收记录。

## 非目标

- M2 不给出模型或服务端到端收益结论；该工作属于 M3。
- 本 ADR 不开放 HIP、`_C.so`、整仓重编译、驱动、系统库、通信库和多机调度。
- 不允许 Agent 直接运行 HCU 测量、修改 Measurement Plan、写 verdict、关闭 Barrier 或签核。
- 不允许把同一份数据同时标为 Search 和 Holdout。

## 后果

- M2a 会引入批级同步点，快 Candidate 必须等待慢 Candidate，但统计语义可审计。
- 每个 Candidate 保留自己的同时期 Baseline 会增加 HCU 时间，但避免跨时间基线漂移。
- Bonferroni 在小候选族上较保守；它优先保护可信度，后续扩展需显式新版本。
- M1 已签核证据无需迁移，M2 的新对象可以增量实现和独立回滚。

## 冻结的评审决策

以下四项已由项目所有者依据 #56～#59 的四线评审范围接受，并作为 M2a Scripted 实现边界：

1. M2a 固定 2–4 个 Candidate，硬上限 4，最多提升 2 个；
2. Holdout 内容由 D `HoldoutPlanAuthority` 管理；Scripted 可用 synthetic Store，Formal 必须使用
   opt-in 受保护 Store、独立身份/ACL 和一次性 Reveal Lease，不要求拆微服务；
3. 保留全部 `faster`，只推荐 adjusted lower 最大的一个，相同则按 Candidate UUID 升序破同分；
4. HCU 硬预算以 exclusive Lease 实际持有时间执行，同时记录 Harness 有效时间用于效率分析。

本次 Accepted 允许实现无 HCU Scripted 路径，但不批准 Real Operator Profile、真实多 Candidate
Formal Task、HCU 性能测量、Agent/M2b、Baseline 提升、M3 E2E 或自动发布。上述能力仍须分别
满足本 ADR 的 Go/No-Go 条件并取得项目所有者的独立授权。

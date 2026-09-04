# v1 领域契约

API/Worker 版本化模型位于 `src/hcuopt/contracts/v1.py`，跨模块平台契约位于 `src/hcuopt/contracts/platform_v1.py`，M2b Proposal 合同位于 `src/hcuopt/contracts/agent_v1.py`，状态机位于 `src/hcuopt/domain/`，数据库迁移位于 `src/hcuopt/storage/sql/`。这些边界必须共同演进，禁止分别维护同名字段。Target、执行、源码、制品和证据接口详见 [公共接口层](public-interfaces.md)。

## 核心对象

| 对象 | 生产者 | 消费者 | 关键不变量 |
|---|---|---|---|
| Stage0Evidence | B/C 探针 | A/D | 原始证据可追溯，能力结果不可由调用方伪造 |
| Stage0Run | A | B/C/D | 绑定 Target Snapshot；Dry Run 不得转为 Formal 结论 |
| Stage0ProbeRecord | B/C Job | A/D | completed Job、租约、原始证据和 Adapter 来源一致 |
| OptimizationTask | A | 所有 Worker | 固定目标、预算和自动发布权限 |
| BaselineEpoch | A/B | C/D | 硬件、软件、配置、Workload 指纹完整 |
| Hotspot | B | C/D | 包含调用路径、占比、机会评分、可补丁性及正确性规格 URI/Hash |
| OptimizationCandidate | C | B/D/A | 绑定 round、parent、epoch、source_hash |
| BuildArtifact | C | D/A | 内容不可变，包含 build recipe、Hash、SBOM |
| MeasurementSeries | B | D/A | 原始样本 URI/Hash、协议、环境指纹和 Adapter 来源完整 |
| M1PerformanceEvidence | B | D | ADR-0006 唯一 Schema；独立进程/缓存/Event、Stage 0 预算和 Lease-bound 清理可重算 |
| M2FormalPhaseExecutionRequest / Receipt | A/B runtime | A/D | Formal-only；分别绑定 Search/Holdout、A1 Authorization/Resolved Plan、Authority Context、三 Profile、Target Lock refresh、Host/HCU/CPU/NUMA、窗口、独占 Lease/续租/Fencing、Round Budget、原始 M1 Evidence 和清理结果；只记录执行事实，不写性能结论 |
| FormalStartActorAssertion / FormalExecutionStartAuthority / FormalEvaluationStartAuthority | operator / B / D | A | 三份独立签名对象；共同绑定唯一 Intent/Preview/Task/Round 与 A1/A2a Hash；B v2 Authority 只能在 Preview 形成后的授权窗口内签发，并从部署注册表绑定 Real Adapter、资源窗口、预算及 Lease/Fencing/Cleanup policy；D 从独立部署注册表绑定 Search Plan、密封 Holdout commitment、FWER 规则、生产 Evidence Root 与独立 Verifier；A 复验包含 owner verifier 的六方角色隔离 |
| FormalStartIntent / DeploymentFormalStartAuthorityStore | A | A/B/D/受保护只读端 | 独立于 Scripted StartIntent；B/D signed Authority 在同一内容寻址命名空间原子 publish-once，Preview 委托既有 Store；A 重读时复算 embedded Hash 并独立验签，幂等、可取消和可恢复；本切片最多到 `ready_for_round_creation`，仍固定禁止 Round 创建、HCU 访问和自动发布 |
| M1CorrectnessResult | D | A/D | 同时绑定原始数值证据与 Verification Artifact URI/Hash |
| EvaluationRun | D | A | 一次有意评测；指明 MeasurementSeries 和判定规则版本 |
| ExecutionAttempt | B | A | 一次物理执行；重试不覆盖 EvaluationRun 或旧日志 |
| ResourceLease | A/B | Worker | 携带 fencing_token；过期后不能写回 |
| ExperimentEvidence | A/D | Registry/KB | 区分事实、复验知识和 Agent 推测 |
| KnowledgeSnapshot | C | Agent/A/D | 知识来源、版本、许可证和 Hash 不可变；只提供建议 |
| CandidateGenerationRequest | A | Agent/C/D | 绑定 Target、Stage 0、Baseline、Hotspot、Workload 与知识快照；禁止 HCU/Holdout/测量访问 |
| CandidateProposal | Agent | A/C/D | 同时冻结完整 Request Hash 与待审 Patch 身份；不是 Candidate 或性能结论 |
| CandidateProposalReviewRecord | C/人工审核人 | A/C/D | 决策绑定 Proposal、Request、Patch、Baseline、Hotspot、审核人、原因、时间、幂等键和审核证据 |
| CandidateProposalPromotionReceipt | C | A/D | 只引用既有 `CandidateSourcePackageRef` 与 M2a source-family verifier 证据；不得新造 Candidate/Artifact/Family |
| BusinessCandidatePackageStoreDescriptor | C | A/D | 固定同一可信 Store 中的 2～4 个真实源码包、Baseline、测试任务和允许替换路径 |
| BusinessCandidateFamilyVerificationRecord | C Verifier | A/D | Store 重读和 Baseline 源码重放均通过，并固定 Store 描述中的测试任务；只表示可申请正式测试窗口，不表示正确或更快 |
| ApexGenerationPlan | A | Agent/B/C/D | 只控制生成器、Generator Artifact Hash、重试、去重和生成预算；不控制 Round/HCU |
| AgentRunRequest / RunnerExecutionRecord | A/B runtime | B Receipt Store | 受限 Runner 边界；绑定 Attempt/Run/Request/Plan/generator、Runner provenance、Generator/Executable 内容 Hash；进程域、输入、环境和生成预算受限，失败时不返回 Proposal bytes |
| RunnerExecutionReceipt / Ref | B deployment | A/C/D | 内容寻址且不可变；绑定执行状态、实际 usage、raw output、cleanup 与 Runner provenance；A 复算内容 Hash 并校验完整 Ref；失败 Receipt 不暴露 raw Proposal |
| GenerationRunStatusView / Budget Ledger | A | C/D/UI | 持久化 Attempt、Receipt Ref、Batch/Proposal Ref 和生成预算；终态结算证据一次性冻结，完全相同重放幂等、任何改绑或事后补绑均失败关闭；D 必须重读底层 Receipt/Batch/Patch，不信任汇总自报 |

## M1 签核后的兼容边界

2026-08-26，首份真实 `manual_candidate` EvidenceBundle 已完成人工签核。根据 ADR-0006，以下
接口从此按“可继续读取和重放”的兼容边界管理：

- `m1-kernel-performance-evidence-v1` 的字段、单位、ABBA acquisition 和 Producer 不写
  verdict 的语义；
- `MeasurementSeries` 到 M1 原始证据 URI/Hash 的绑定；
- M1 Correctness、EvaluationRun、EvidenceBundle 和单 Candidate Signoff 的既有语义；
- `manual_candidate` 的一个 Task 只能登记一个逻辑 Candidate 的约束。

M2 不在这些 v1 对象中追加 Search、Holdout、候选族或多重比较字段，而是用新对象引用既有
单次比较证据。需要不兼容变化时必须发布新 Schema 版本，保留旧版本解析器、迁移策略和 M1
签核证据回放测试。M2a 的 Proposed 契约见 [M2 Contract 草案](m2-contract-draft.md)；它在
ADR-0009 Accepted 前不属于可运行 Contract。

## Candidate 最小字段

```yaml
candidate_id:
task_id:
round_id:
track: config | triton | hip
baseline_epoch_id:
parent_candidate_id:
source_hash:
target_hardware:
model_id:
framework_version:
workload_id:
build_recipe:
correctness_recipe:
benchmark_recipe:
release_mode: hot_patch | overlay | manual_only
```

## Job 与错误语义

每个 Job 必须带全局唯一 `idempotency_key`，每次领取生成新的 `claim_token`。GPU Job 还必须带资源当前的 `fencing_token`；Claim 或 Fencing 任一过期，Heartbeat、Complete 和 Fail 均返回冲突，不接受迟到写回。

`lease_scope` 明确区分资源纪律：Agent/Build 为 `none`，正确性验证为 `shared`，Profiler、Performance 和 E2E 计时为 `exclusive`。共享正确性 Job 不得借机记录或发布性能结论。

M2a Formal Phase Adapter 在预算 reserve 前必须同时验证 A1 Authorization/Resolved Plan、Authority
Context、版本化 Adapter Profile、Target Lock refresh、Target/Host/HCU/CPU/NUMA、批准窗口、独占
Lease 的权威回执/续租截止时间和当前 Fencing Token。A1 Authorization 与 A2a Resolved Plan 必须
由部署侧重读并复验 owner signature、内容 Hash、Profile/Family/预算/窗口，不能信任请求自报。
过期 Lease 先通过注入的 fenced recovery
恢复资源再拒绝执行；Harness 未启动时取消使用 release；一旦启动，成功、timeout、证据失败和
cleanup 失败都使用 settle；同步 Harness 的计划上限和实际执行均不得跨续租截止时间。成功回执
携带既有完整 `RoundMeasurementRef`，durable authority 跨进程拒绝复用 Measurement、原始证据、
baseline sample、process identity 与 cache namespace。终态回执通过 no-follow 受保护根原子发布，
Receipt ID 不可改绑，并固定
`performance_conclusion=not_measured`，统计裁决仍只属于 D。

M2a Formal StartIntent 使用独立 `m2a-formal-start-intent-v1` Contract 和 PostgreSQL 表，不能复用
Scripted `operator_start_intents`。调用方只能提交 Preview、Resolved Plan、owner window、B/D
Authority 的内容 Hash；A 从部署 Store 重读全部对象并验证 operator/B/D 的独立签名。B 与 D 的
Authority 必须同时绑定由幂等键确定的唯一 `intent_id`、`task_id`、`round_id` 和 `preview_id`，避免
一个窗口授权被复用为第二个 Round。B 的 `M2FormalExecutionStartAuthorityIssuer` 只接收 Preview ID
和幂等键，从部署 Store/Registry 重读并签发 v2 Authority；详情见
[M2a B execution Start Authority](m2a-formal-execution-start-authority.md)、
[M2a D evaluation Start Authority](m2a-formal-evaluation-start-authority.md) 和
[M2a Formal StartIntent](m2a-formal-start-intent.md)。

B/D issuer 的输出只能通过 `DeploymentFormalStartAuthorityStore` 发布。该 Store 按 Authority Hash
定位并原子 publish-once，每次读取都重新解析正确类型并复算 Contract 内置 Hash；缺失对象可恢复
等待，覆盖冲突、跨类型、篡改、畸形、超限、路径逃逸或链接对象全部 fail closed。它只委托既有
Preview Store，不维护第二份 Preview，也不代替 A 的 production signature verifier。

错误使用稳定结构：

```json
{
  "code": "stale_claim_token",
  "message": "worker no longer owns this job",
  "retryable": false
}
```

MVP 中 `automatic_release_allowed` 在 API 和数据库层都强制为 `false`。
任务同时保存 `stage0_authority=none|synthetic|formal`。旧 Fake 入口只能得到
`synthetic`；只有 Target-bound Formal Run 经七探针 Barrier finalize 后才能得到
`formal`。

## Contract 解冻

变更必须有 ADR、接口上下游批准、A 批准以及迁移/兼容测试。`domain/` 的 CODEOWNER 是 A，但 A 不能单方面改变 D 的判定语义或 B 的测量语义。

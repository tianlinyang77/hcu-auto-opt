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
| ApexGenerationPlan | A | Agent/B/C/D | 只控制生成器、重试、去重和生成预算；不控制 Round/HCU |

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

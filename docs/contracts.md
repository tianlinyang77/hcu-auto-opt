# v1 领域契约

API/Worker 版本化模型位于 `src/hcuopt/contracts/v1.py`，跨模块平台契约位于 `src/hcuopt/contracts/platform_v1.py`，状态机位于 `src/hcuopt/domain/`，数据库迁移位于 `src/hcuopt/storage/sql/`。这些边界必须共同演进，禁止分别维护同名字段。Target、执行、源码、制品和证据接口详见 [公共接口层](public-interfaces.md)。

## 核心对象

| 对象 | 生产者 | 消费者 | 关键不变量 |
|---|---|---|---|
| Stage0Evidence | B/C 探针 | A/D | 原始证据可追溯，能力结果不可由调用方伪造 |
| OptimizationTask | A | 所有 Worker | 固定目标、预算和自动发布权限 |
| BaselineEpoch | A/B | C/D | 硬件、软件、配置、Workload 指纹完整 |
| Hotspot | B | C | 包含调用路径、占比、机会评分与可补丁性 |
| OptimizationCandidate | C | B/D/A | 绑定 round、parent、epoch、source_hash |
| BuildArtifact | C | D/A | 内容不可变，包含 build recipe、Hash、SBOM |
| MeasurementSeries | B | D/A | 原始样本 URI/Hash、协议、环境指纹和 Adapter 来源完整 |
| EvaluationRun | D | A | 一次有意评测；指明 MeasurementSeries 和判定规则版本 |
| ExecutionAttempt | B | A | 一次物理执行；重试不覆盖 EvaluationRun 或旧日志 |
| ResourceLease | A/B | Worker | 携带 fencing_token；过期后不能写回 |
| ExperimentEvidence | A/D | Registry/KB | 区分事实、复验知识和 Agent 推测 |

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

## Contract 解冻

变更必须有 ADR、接口上下游批准、A 批准以及迁移/兼容测试。`domain/` 的 CODEOWNER 是 A，但 A 不能单方面改变 D 的判定语义或 B 的测量语义。

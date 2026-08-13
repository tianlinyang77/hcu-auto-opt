# v0.1 领域契约

代码源定义位于 `src/dcuopt/domain/`。本文件说明跨模块不变量。

## 核心对象

| 对象 | 生产者 | 消费者 | 关键不变量 |
|---|---|---|---|
| Stage0Evidence | B/C 探针 | A/D | 原始证据可追溯，能力结果不可由调用方伪造 |
| OptimizationTask | A | 所有 Worker | 固定目标、预算和自动发布权限 |
| BaselineEpoch | A/B | C/D | 硬件、软件、配置、Workload 指纹完整 |
| Hotspot | B | C | 包含调用路径、占比、机会评分与可补丁性 |
| OptimizationCandidate | C | B/D/A | 绑定 round、parent、epoch、source_hash |
| BuildArtifact | C | D/A | 内容不可变，包含 build recipe、Hash、SBOM |
| EvaluationResult | D | A | 指明使用的 MeasurementRecord 和判定规则版本 |
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

## Contract 解冻

变更必须有 ADR、接口上下游批准、A 批准以及迁移/兼容测试。`domain/` 的 CODEOWNER 是 A，但 A 不能单方面改变 D 的判定语义或 B 的测量语义。


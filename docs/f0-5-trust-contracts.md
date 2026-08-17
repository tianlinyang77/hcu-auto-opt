# F0.5 可信证据契约

F0.5 是 F1 真实 No-op 框架开始前的一次小范围契约修订。它不实现 SSH、SGLang、
Profiler 或任何性能优化，只保证后续真实执行不会覆盖复测历史，也不会把 Fake 数据误报成性能结果。

公共契约修订号为 `platform-v1.1`，模块路径仍为
`src/dcuopt/contracts/platform_v1.py`。

## 新增契约

| 契约 | 责任 |
|---|---|
| `AdapterProvenance` | 保存 Adapter Profile、能力、实现名、版本、真假类型和可选源码 Commit |
| `MeasurementSeries` | 保存测量状态、指标、单位、协议、样本数量、原始样本 URI/Hash 和环境指纹 |
| `EvaluationRun` | 表示一次有意发起的评测；同一候选可以有多次评测运行 |
| `ExecutionAttempt` | 表示评测运行的一次物理执行；超时重试不会伪装成新的评测 |

## 证据边界

- `ExecutionResult` 必须携带执行 Adapter 的 provenance。
- `EvidenceBundle` 必须列出参与生成证据的 Adapter provenance。
- Fake Adapter 产生的结果必须为 `synthetic=true`。
- Fake Measurement Harness 只能返回 `status=not_measured`，样本数必须为 0。
- Fake 的 performance/E2E EvaluationRun 不允许写入通过结论，`passed` 必须为空。
- Synthetic 结果禁止携带 `speedup_ratio`、延迟、吞吐和置信区间等性能声明字段。
- 真实测量必须同时提供原始样本 URI、内容 Hash 和环境指纹。

Fake 仍然可以驱动 Walking Skeleton 到达人工作业状态，但它只能证明控制流，不构成 Stage 0
或性能证据。

## 持久化模型

数据库迁移 `0002_evaluation_evidence.sql` 将旧 `evaluations` 表升级为
`evaluation_runs`，删除 `(candidate_id, phase)` 唯一约束，改用 `idempotency_key` 区分：

- 同一 Job 的重复完成回调：返回同一个 EvaluationRun；
- 新 Job 发起的有意复测：创建新的 EvaluationRun；
- 一次 EvaluationRun 的物理重试：写入不同的 ExecutionAttempt。

因此，复测不会覆盖旧结果，网络重放也不会重复创建证据。
迁移会保留既有 Fake Evaluation，但会删除其中旧的加速比和置信区间字段，改为
`measurement_status=not_measured`，避免历史夹具继续冒充性能结果。

## F1 实现要求

- B 的真实 Executor 必须构造带真实 provenance 的 `ExecutionResult`。
- B 的 Measurement Harness 必须返回 `MeasurementSeries`，不得返回自由格式性能字典。
- D 生成 `EvidenceBundle` 时必须汇总实际使用的 Adapter provenance 和 measurement ID。
- A 持久化评测时必须为每次有意运行创建新的 idempotency key。
- 重试使用同一个 EvaluationRun，并递增 `ExecutionAttempt.attempt_number`。

详细决策见 `docs/adr/0003-f0-5-evaluation-evidence.md`。

# ADR-0003：拆分评测运行、执行尝试与结构化测量

- 状态：Accepted
- 日期：2026-08-17

## 背景

Walking Skeleton 使用 `(candidate_id, phase)` 作为 Evaluation 唯一键，并在冲突时覆盖指标。
这种结构适合控制流夹具，但不能保存同一候选的有意复测。原始测量数据也只能放入无类型字典，
Fake Adapter 还会生成看起来像真实结果的加速比。

F1 尚未产生真实数据，现在是修正这些边界成本最低的时间点。

## 决策

1. `ExecutionResult` 继续只描述命令执行，不承载测量样本。
2. 新增 `MeasurementSeries`，原始样本通过 URI 和内容 Hash 归档。
3. 一次有意评测使用 `EvaluationRun`，一次物理执行使用 `ExecutionAttempt`。
4. EvaluationRun 用调用方提供的 idempotency key 抵抗消息重放，但允许创建新的复测运行。
5. 所有执行、测量和证据必须携带 Adapter provenance。
6. Fake 测量不生成样本、加速比、延迟、吞吐或置信区间。
7. Fake performance/E2E 可以驱动控制流，但 EvaluationRun 不保存通过判定。

## 后果

- F1 的真实 Adapter 必须实现 `platform-v1.1`，旧的自由格式 Measurement Harness 不兼容。
- 数据库增加一次不可逆的结构升级；原 Evaluation 会作为 synthetic legacy run 保留，旧 Fake 性能声明字段会被移除。
- API `/v1/tasks/{task_id}/evaluations` 路径保持不变，返回内容变为 EvaluationRun 历史。
- 后续 Stage 0 可以从原始样本重新计算噪声、CV 和 MDE，而不依赖已覆盖的汇总值。

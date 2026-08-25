# ADR-0008：分离 Stage 0 Authority 与 M1 业务 Workload 身份

- 状态：Accepted
- 日期：2026-08-25

## 背景

Formal Stage 0 使用微型计时 Workload 标定计时分辨率、噪声和基础 MDE；M1 则测量冻结的
真实业务 Hotspot。二者共享 Target Snapshot、Stage0Run 和注册采样纪律，但不是同一个
Task、Workload 或 Adapter Profile。旧 Contract 把 M1 Task 的 `workload_id` 直接继承自
Stage 0 Task，并在 D 中要求 Formal 报告的 Task/Profile 等于 M1 Task/Profile，导致真实
M1 链必然在独立裁决时失败。

## 决策

1. `ManualCandidateTaskCreate` 必须显式提交 M1 业务 `workload_id/workload_hash`，Baseline
   Epoch 冻结这组业务身份，不再复用 Stage 0 微型 Workload 名称。
2. `M1Stage0ReportReference` 同时保留 `stage0_task_id`、`stage0_workload_id` 和
   `stage0_adapter_profile`。B 与 D 必须从哈希化机器报告重新核对这些 Authority 身份。
3. Stage 0 与 M1 继续绑定同一 Target Snapshot、Stage0Run、协议 Hash、输入证据 Hash 和
   machine-report Hash；本 ADR 不允许跨 Target 或跨协议搬运 MDE。
4. D 的最终可信门限仍为
   `max(Formal Stage 0 MDE, 当前 M1 Workload Baseline MDE)`。身份分离不构成门限放宽。
5. 这是首次正式 M1 证据前的 Contract 修正；旧的 scripted/fixture payload 必须迁移，
   不提供静默 fallback。

## 后果

- Stage 0 的基础尺子可以作为显式 Authority 输入，同时 M1 原始证据准确标记业务 Workload。
- 真实 M1 Task、Stage 0 Task 和两个 Adapter Profile 不再被错误要求相等。
- 缺少任一 Authority 身份的旧 Job fail-closed，不能产出新的正式 `MeasurementSeries`。

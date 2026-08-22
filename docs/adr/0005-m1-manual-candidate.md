# ADR-0005：M1 使用单人工候选和独立裁决链

- 状态：Accepted
- 日期：2026-08-22

## 背景

F1 已证明锁定源码、确定性制品、真实 SGLang 执行、等价性证据和资源清理能够被同一套
控制面串起来。Formal Stage 0 的当前结论是 `DEGRADED_MANUAL_INTAKE`：测量闸门通过，
但 Profiler 只能降级定位，热替换也只证明了启动时 Overlay。因此系统可以接收人工定位的
候选，但没有依据开放 Agent 搜索、自动发布或生产灰度。

直接复用旧 Optimization 状态机会把单候选误写成多候选 Round/Beam Search，并让 Build、
正确性、原始性能测量和统计裁决之间的责任边界不清晰。

## 决策

1. 新增 `manual_candidate` Workflow，当前只接受完成并独立验证的 Formal Stage 0，且
   `ProjectMode` 必须为 `DEGRADED_MANUAL_INTAKE`。
2. 创建任务时原子生成一个不可变 Baseline Epoch，固定 Target Snapshot、Workload
   ID/Hash、配置 Hash、Baseline SourceSnapshot、镜像 Digest、Adapter Profile、
   Stage0Run 和 Stage 0 协议 Hash。
3. 每个 M1 Task 只允许一个 Candidate。Candidate 接入必须显式回传
   `baseline_epoch_id`，声明源码 Hash、优化意图、替换点、类型和幂等键；Candidate ID
   由服务端根据幂等键稳定生成。
4. MVP 只接受 Triton/Python 启动时 Overlay，不接收 `_C.so`、系统库、驱动、整仓重编译
   或 Candidate 分支。
5. Durable Job 顺序固定为 Build → Correctness → Performance → Adjudication。
   Build 不占 HCU；正确性使用共享租约；性能测量使用独占租约；独立裁决不占 HCU。
6. B 的 Performance Job 只提交真实 `MeasurementSeries` 原始证据，不直接写 Candidate
   verdict。D 的 Adjudication Job 绑定相同 Task/Candidate/Baseline/Target 后，才写入
   `faster`、`slower`、`inconclusive` 或 `invalid`。
7. 非 `invalid` 结果都进入人工签核。签核只表示接受或拒绝这份证据，不授予自动发布；
   `automatic_release_allowed` 始终为 `false`。
8. Job 终态失败、最大重试次数耗尽和 stale Worker 都必须原子收敛 Task/Candidate；相同
   Job completion、Candidate intake 和 Signoff 重放必须返回同一逻辑对象。
9. 默认 Adapter Catalog 暂不注册 M1 Real Profile。只有 B/C/D 的真实实现同时具备
   `candidate_builder`、`kernel_correctness`、`measurement_harness`、
   `candidate_adjudicator` 和 `resource_cleaner` 后才开放任务创建。

## 后果

- A 可以先冻结控制面和公共 payload，B/C/D 在不改状态机和数据库语义的前提下并行接入。
- M1 能保存成功、失败和 inconclusive 证据，但仍不是自动优化器，也不能形成发布授权。
- Correctness 和 Performance 分两次租约；稀缺独占 HCU 只用于计时测量。
- 后续 Agent、多候选 Barrier、Holdout/FDR 和自动搜索必须另立 ADR，不得悄悄扩展本链。

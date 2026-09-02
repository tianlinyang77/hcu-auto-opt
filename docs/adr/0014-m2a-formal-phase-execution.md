# ADR-0014：M2a Formal Search/Holdout 使用独立执行 Adapter

- 状态：Proposed / implementation under review
- 日期：2026-09-02
- 跟踪：GitHub Issue #126
- 适用范围：Contract 与无 HCU 失败路径；不授权真实 HCU 测量

## 背景

ADR-0009 已定义 M2a 的轮次级 Search/Holdout、预算和 Barrier，但当前执行实现仅覆盖
无 HCU Scripted 编排。Formal 执行需要把一次物理运行与批准的 Authority Context、目标资源、
时间窗口、独占 Lease、Fencing Token 和预算消费绑定，同时继续复用 M1 唯一 Measurement
Harness。若把这些语义继续放入 Scripted Receipt，synthetic 编排证据可能被误当成正式执行证据。

## 决策

新增独立的 `m2a-formal-phase-execution-v1` Contract 和 Formal Phase Execution Adapter，不扩展
Scripted Receipt。请求必须冻结 A1 Authorization/Resolved Plan、Formal Authority Context、
Round/Candidate/Artifact、三个 Profile、Target Snapshot/Lock refresh、Search 或 Holdout Plan、
Host/HCU/CPU/NUMA、批准窗口、独占 Lease 权威回执/续租/Fencing Token、Job Attempt 和 Round
Budget reservation。Adapter Profile 自身内容寻址且版本化，状态固定为
`implementation_ready_unregistered`，本 ADR 不注册 Real 实例。

Adapter 只调用现有 `run_manual_performance()` Harness，不重新实现采样或统计裁决。它在预算
reserve 前 fail-closed 验证 Authority、窗口、HCU/CPU/NUMA 拓扑、Lease 续租、Fence、当前真实
Target Lock refresh 和预算；过期 Lease 先调用注入的 fenced recovery 并验证资源恢复，再拒绝
执行。执行后复核原始 Evidence URI/Hash、Fence、时钟恢复、残留容器与资源健康。Harness 未
启动时取消使用 release；一旦启动，无论成功、timeout、证据错误还是 cleanup 失败都使用 settle。

Search 与 Holdout 使用独立请求和执行记录，并拒绝复用 Measurement ID、URI、Hash 或 Phase
Plan。终态 Formal Execution Receipt 使用内容寻址存储，Receipt ID 到内容 Hash 的绑定
write-once；相同内容重放幂等，任何相同 ID、不同内容的发布均失败关闭。

Receipt 只证明执行、预算结算与清理事实，固定
`performance_conclusion=not_measured`。性能验证、Barrier 和 `faster` 结论仍属于 D 的既有权限。

## 后果

- 无 HCU 测试可以覆盖缺 Lease、窗口过期、Fence 错误、预算不足、timeout、清理失败和改绑拒绝。
- Formal Adapter 依赖现有 Harness、预算权威、Lease/Fence/Target Lock 活性检查与恢复回调。
- 本 ADR 不批准访问 nmz36、不授权真实 HCU 执行，也不改变 M1 Evidence Schema。
- 上下游评审通过前，本 ADR 保持 Proposed，真实 Formal 运行继续关闭。

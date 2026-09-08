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

Adapter 只调用现有 `run_manual_performance()` Harness，不重新实现采样或统计裁决。部署必须注入
只读 Authority Reader 和签名验证器；Adapter 在每次 reserve 前重新读取完整 A1 Authorization 与
A2a Resolved Plan，复算 Plan Hash，并交叉验证 owner signature、Profile/Family/预算/窗口与当前
Round/Candidate。它同时 fail-closed 验证 Authority、HCU/CPU/NUMA 拓扑、Lease 续租、Fence、当前真实
Target Lock refresh 和预算；过期 Lease 先调用注入的 fenced recovery 并验证资源恢复，再拒绝
执行。同步版本要求计划上限与实际执行都不能跨越 `lease_renewal_due_at`。执行后复核原始
Evidence URI/Hash、Fence、时钟恢复、残留容器与资源健康。Harness 未
启动时取消使用 release；一旦启动，无论成功、timeout、证据错误还是 cleanup 失败都使用 settle。

成功执行只发布仓库既有 `RoundMeasurementRef`，完整保留 baseline sample set、process identity
set 和 cache namespace set Hash。Search/Holdout 的唯一性由部署显式配置的 durable authority
原子登记；单机锁定 Worker 的参考实现使用持久化 SQLite，进程重启或多个 Worker 仍拒绝复用
Measurement、URI、Evidence、sample/process/cache identity 或 Phase Plan。终态 Formal Execution
Receipt 使用受保护根、逐级 no-follow 与原子 publish-once；Receipt ID 到内容 Hash 的绑定
write-once，相同内容重放幂等，父目录 symlink 逃逸、并发改绑和相同 ID 不同内容均失败关闭。

Receipt 只证明执行、预算结算与清理事实，固定
`performance_conclusion=not_measured`。性能验证、Barrier 和 `faster` 结论仍属于 D 的既有权限。

## 后果

### 2026-09-08：M1 生产端的隔离身份交接（#126）

Formal 结果校验要求三组身份摘要，原 M1 Harness 的 `MeasurementSeries.summary` 未输出，
导致现有唯一 Harness 无法通过该结果边界。本切片补生产端输出，不放宽 Formal 的必填检查。

`m1_isolation_hashes()` 从 M1 原始 acquisitions 投影三组集合，排序、去重后，以既有
`canonical_json_bytes` 编码 `{schema_version: "m1-isolation-set-v1", kind: <字段名>,
members: <集合>}`，计算 SHA256：

- `baseline_sample_set_hash`：仅 baseline arm 的原始 device Event record SHA256 集合；
- `process_identity_set_hash`：全部 acquisition 的 `(process_id, process_start_token)` 集合；
- `cache_namespace_set_hash`：全部 acquisition 的 activation cache namespace Hash 集合。

摘要不加入 Round、Candidate、phase、文件路径或 report ID 盐值；同一批采样换标签后仍得到
相同身份。字段由 Harness 从已构造的原始证据生成，忽略调用者提供的同名字段。原 M1
performance.json 格式不变；旧证据不回填或重写，没有迁移、裁决或 Real Profile 注册。

新增组合测试使用真实 `M1TrustedMeasurementHarness` 与 CPU 计时/清理夹具，再调用现有
Formal 结果校验和 `RoundMeasurementRef` 构造。它只验收结果交接，不调用 Formal `run()`，
不证明完整 A/B 授权、租约、预算、Search/Holdout 或 HCU 已联通。

保留的后续工作：部署层完整冻结输入、当期 Target Lock/Lease/fencing、
完整生命周期联验，以及 D 独立重读原始 acquisition/Event 并复算摘要。整组 Hash 只能识别
整组复用，不等于逐成员的部分重叠检测；不能据此宣布 #126 或正式隔离验收完成。

### 2026-09-08：运行时上下文与局部预算接线

Formal Adapter 在 reserve 之前编译并核对 Harness payload。由冻结 binding/round/reservation
填充 Task、Baseline、Stage 0、Workload/Configuration 身份，以及 M1 实际读取的
`_job_context`（Lease ID、独占 scope、resource、Fence）和 `budget`（样本数、墙钟上限）。
调用者若携带这些字段，必须与派生值的规范 JSON 完全一致；额外上下文字段、不同资源或
放大预算均在 reserve 前拒绝，不记录为已启动测量、不调用资源清理。

M1 预算接口要求整数秒，因此 Formal 的局部墙钟预算向下取整；不足一秒拒绝，不向上扩大
授权。M1 在初始化 device timer 前复算实际采样 Plan Hash 并核对预期样本数，拒绝冻结计划
与实际 `plan_factory` 的偏差。

运行时 Lease guard 由 Adapter 注入 M1 原有 `lease_lost_event.is_set()` 检查点，不接受调用方
提供的事件或假值。guard 每次核对窗口、租约/续租截止和部署 Lease 活性，异常或非精确 True
均视作失效；M1 在计时标定前以及既有采样检查点消费它。该机制不自动续租，不代替 Worker
进程超时/强制 fencing，也不能中断一条已经阻塞的设备调用。

此增量不证明完整 Target/Artifact/Stage0 来源已与部署注册逐一接通，也未完成 M1 部分采样
失败到 Formal 实际预算消费的完整交接。Real Profile、D 独立重读、完整 run 生命周期与当期
实机验收仍未放行。旧 M1 非 Formal 调用保留原行为。

- 无 HCU 测试可以覆盖缺 Lease、窗口过期、Fence 错误、预算不足、timeout、清理失败和改绑拒绝。
- Formal Adapter 依赖 A1/A2a Authority Reader、owner signature verifier、现有 Harness、durable
  isolation authority、预算权威、Lease/Fence/Target Lock 活性检查与恢复回调。
- 本 ADR 不批准访问 nmz36、不授权真实 HCU 执行，也不改变 M1 Evidence Schema。
- 上下游评审通过前，本 ADR 保持 Proposed，真实 Formal 运行继续关闭。

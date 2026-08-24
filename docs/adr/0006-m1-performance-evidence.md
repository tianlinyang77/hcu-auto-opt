# ADR-0006：M1 使用单一、可独立复算的性能证据 Contract

- 状态：Accepted
- 日期：2026-08-24

## 背景

ADR-0005 已冻结职责边界：B 的 Performance Job 只产出真实原始测量，D 重新读取原始证据
并独立写入 `faster`、`slower`、`inconclusive` 或 `invalid`。B 与 D 曾分别定义不同的
Schema、指标名、进程分组、缓存和清理结构，导致 Producer 的正式输出无法交给 Verifier。

Formal Stage 0 已把当前计时指标冻结为 `kernel_elapsed/ns`，并通过注册协议冻结 ABBA、
restart、warmup、sample 和 batch 预算。M1 不能另造指标名，也不能把 Stage 0 的 MDE
应用到另一套采样预算。

## 决策

1. M1 性能原始证据的唯一 Schema 是
   `m1-kernel-performance-evidence-v1`，指标固定为 `kernel_elapsed`、单位固定为 `ns`。
   B 和 D 必须导入或直接消费同一个仓库模型，不得在 Verifier 内复制第二套同名 Contract。
2. 一个 acquisition 只包含 Baseline 或 Candidate 中的一臂。每个 ABBA acquisition 都使用
   独立进程身份、独立且执行前为空的缓存 Namespace；Candidate 必须绑定准确的启动时
   Overlay Artifact 和 import attestation，Baseline 必须证明未加载 Candidate。
3. 子进程内的 HCU Event 记录必须先原子写成独立原始文件。B 从可信证据根按 URI/SHA256
   重新读取，核对进程、arm、acquisition、sample、batch 和真实 Measurement Harness
   Provenance 后，才把引用写入主证据。裸整数或调用方汇总不能成为正式样本。
4. 主证据保存完整采样计划、Stage 0 authority 和
   `sample_budget_hash`。M1 计划必须与当前注册 Stage 0 协议的 restart、ABBA 顺序、warmup、
   samples 和 batch 完全一致；不允许把同一个 MDE 搬到缩减计划。
5. Performance Job 必须绑定控制面提供的 Adapter Profile、Baseline Epoch、Target、Workload、
   Artifact 和独占 Lease。进程必须能从原始 procfs 记录复核 start token，且成功测量要求
   `wait_status=0`。
6. Fence 和 Health 在采集结束后执行。只有清理成功且 `resource_id/fencing_token` 与本次
   Lease 相等时才写最终主证据；`MeasurementSeries.raw_samples_hash` 必须覆盖清理证据。
   采集或清理失败时写单独的哈希化 failure evidence，不返回 `status=measured`。
7. `max_samples` 在启动前检查，`max_wall_seconds` 在标定、每次采样和清理后持续检查。
   超预算是证据失败，不能降级为性能结论。
8. B 的证据中不允许 Producer verdict。D 必须重新打开主证据以及它引用的 Event、缓存、
   lifecycle、Stage 0 和清理记录，再独立计算结论。M1 继续保持
   `automatic_release_allowed=false`。

## 兼容和迁移

- 未合入的 D 实现必须删除自己的 `kernel_latency` 和“一个进程同时保存 Baseline/Candidate”
  模型，改为消费本 ADR 的 acquisition Contract。
- Schema v1 在 M1 首次真实 Target Lock 前仍可通过新 ADR 演进；一旦产生被签核的正式证据，
  任何不兼容修改必须新增 Schema 版本并提供迁移或并行读取测试。
- C 只负责构造 Baseline/Overlay 进程与 Artifact 激活。子进程计时入口必须实现 B 冻结的
  Event Record 协议，不能把开发 benchmark 摘要冒充正式 MeasurementSeries。

## 后果

- B/C/D 有一个可执行的交接边界，D 不再依赖 Producer 自报摘要。
- 文件数增加，但每个原始 Event、缓存和 lifecycle 记录都可以单独重哈希和归因。
- M1 不允许为了缩短测试时间改变正式采样预算；测试夹具必须明确执行注册协议或只验证
  非正式的模型构造。

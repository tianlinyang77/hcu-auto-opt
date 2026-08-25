# M1 手工 Candidate 可信评估链

## 这阶段在做什么

M1-A 把一份人工提供的 Triton/Python Overlay 候选，放进一条可恢复、可审计的真实优化
流水线。它解决的不是“自动生成 Kernel”，而是先让团队对同一个 Target、同一个基线、
同一份候选和同一组证据说话。

```text
Formal Stage 0（DEGRADED_MANUAL_INTAKE）
  → Immutable Baseline Epoch
  → Manual Hotspot Intake（真实 Profiler 证据 + 人工补充信息）
  → Manual Candidate Intake
  → Build（C）
  → Correctness（D 协议；共享 HCU 租约）
  → Raw Performance Measurement（B Harness；独占 HCU 租约）
  → Independent Adjudication（D）
  → EvidenceBundle
  → Human Signoff
```

当前代码已完成 A/B/C/D 的公共接线：C 构建不可变 Overlay，D Correctness Worker 重读
原始数值证据并发布 Verification Artifact，B 通过唯一 Harness 产生 MeasurementSeries，
D Adjudicator 再次重读正确性和性能证据。四段能力必须组合成同一个 opt-in Real Profile。
默认 Adapter Catalog 仍故意不注册 M1，因此测试夹具或不完整 Worker 不能开放真实任务。
业务 Hotspot 冻结后，还需提供对应的 `M1WorkloadFactory` 和 Correctness Evidence Producer，
再进行 nmz36 Target Lock 实机闭环。

## B 测量链当前能力

B 从持久化 Job 取得 Formal Stage 0 machine report 的 URI、SHA256、输入证据 Hash 和协议
Hash，并在可信证据根下重新读取、重新哈希。只有 Stage 0 测量闸门为 `pass`，且
Stage0Run、TargetSnapshot、Target、Workload 和注册协议全部一致时，才允许生成 M1 计划。
计划内的 MDE、噪声、alpha、power 和 bootstrap 参数来自这一次 Formal 报告及其注册协议，
不会写成 nmz36 的永久常量。D 还会使用相同 alpha、power 和 restart 预算，从当前 M1
Workload 的 Baseline restart 均值复算一份 MDE，并以两份 MDE 的较大值作为可信门限，
避免把 Stage 0 微型负载的可检测能力直接冒充业务 Workload 的可检测能力。

测量按 `Baseline-Candidate-Candidate-Baseline` 分组执行。每个 acquisition 都必须使用新的
外部进程，并记录进程身份、启动/回收原始记录、设备 Event、host interval、warmup、样本
顺序、镜像、缓存 namespace、Overlay import attestation 和运行前后遥测。采集结束后再执行
Fence/Health；只有资源和 fencing token 与本次 Lease 一致时，才把清理结果写进最终证据。
Candidate 必须证明加载的是 Job 绑定的 Artifact Hash；Baseline 必须证明未加载 Candidate。

B 最终只返回一个指向 `m1-kernel-performance-evidence-v1` 原始文件的 `MeasurementSeries`。
主文件 Hash 同时覆盖计划、样本和清理结果，且模型明确禁止 producer verdict。
`faster/slower/inconclusive/invalid`
只能由 D 重新读取原始样本后给出。

## 不可变绑定

创建 M1 Task 时，Repository 在同一事务中创建 Baseline Epoch，固定以下字段：

| 绑定 | 用途 |
|---|---|
| `target_snapshot_id` | 防止运行期间换主机、拓扑或镜像配置 |
| `stage0_run_id` + `stage0_protocol_hash` | 证明本任务继承的是哪次正式能力判定 |
| `workload_id` + `workload_hash` | 防止 Baseline/Candidate 使用不同请求集 |
| `source_snapshot_id` | 固定干净、真实且与 Target Lock Commit 一致的源码 |
| `image_digest` | 禁止同名 Tag 漂移 |
| `configuration_hash` | 固定推理参数和系统配置 |
| `adapter_profile` | 保证四段 Job 使用同一套真实实现族 |

数据库已有不可变触发器，Baseline Epoch 创建后禁止 UPDATE/DELETE。Candidate Intake 还必须
显式提交 `baseline_epoch_id`；仅知道 Task URL 不足以绕过基线绑定。

## Job 与四人边界

| Job | Worker / 租约 | 输入 | 输出与责任人 |
|---|---|---|---|
| `manual_build` | Build / none | Baseline Source、Candidate Hash、替换点 | C：干净 Candidate SourceSnapshot + 不可变 Overlay Artifact |
| `manual_correctness` | GPU / shared | Artifact、Target、Workload | D：按冻结正确性协议给出 correct/incorrect/invalid；B 提供隔离与清理能力 |
| `manual_performance` | GPU / exclusive | 已通过正确性的 Artifact、B 唯一 Harness | B：真实 MeasurementSeries 和原始证据，不写快慢 verdict |
| `manual_adjudicate` | Evaluation / none | Performance 原始证据与全部不可变 ID | D：独立产生 verdict、EvaluationRun 和 EvidenceBundle |

所有真实结果都必须带 Real Adapter provenance；Fake provenance、Synthetic Artifact 或
Synthetic Measurement 会在 Contract 层失败。正确性和性能 Job 都必须提交 fenced、healthy
的清理证据。D 的最终 EvidenceBundle 还必须只绑定本轮 Measurement ID，并包含 Candidate
Artifact、正确性原始证据、正确性 Verification Artifact 和性能 Raw Samples URI；漏掉任一项
都不能写入 verdict。两次 HCU Job 的 Lease ID、Resource ID 和 Fencing Token 由控制面传给
Adjudicator，D 不接受 Producer 自报的租约身份。

## 状态机

```text
Task:
manual_candidate_pending
  → manual_building
  → manual_correctness
  → manual_performance
  → manual_adjudicating
  → awaiting_signoff
  → completed | rejected

Candidate:
proposed → building → built → correctness_running
  → performance_running → adjudicating → awaiting_signoff
  → accepted | rejected
```

Build 的终态失败把 Candidate 收敛为 `build_failed`；其他 Job 终态失败收敛为 `rejected`。
Worker 心跳过期且重试预算已耗尽时使用同一套收敛逻辑。旧 Claim/Fencing Token 不能再次
写回。

## API 使用顺序

### 1. 创建绑定 Formal Stage 0 的任务

```http
POST /v1/manual-candidate/tasks
Content-Type: application/json

{
  "name": "LayerNorm manual overlay",
  "stage0_run_id": "<formal-stage0-run-uuid>",
  "adapter_profile": "<registered-real-m1-profile>",
  "baseline_source_snapshot_id": "<clean-baseline-source-uuid>",
  "workload_hash": "sha256:<64-hex>",
  "configuration_hash": "sha256:<64-hex>",
  "idempotency_key": "m1-layernorm-20260822",
  "budget": {"max_wall_seconds": 1800, "max_samples": 5000}
}
```

服务端不接受命令行、挂载或任意可执行参数作为公开 Budget。具体执行命令属于已审核的
Adapter Profile 私有配置。

### 2. 读取系统生成的 Baseline Epoch

```http
GET /v1/manual-candidate/tasks/<task-id>/summary
```

### 3. 登记人工热点

```http
POST /v1/manual-candidate/tasks/<task-id>/hotspots
```

该接口保存真实 Profiler 原始输出的 URI/Hash、内容寻址的 Correctness Spec URI/Hash，以及
人工核对的 shape、dtype、meta、实现位置、调用路径、Amdahl 占比、上游查重、可补丁性、
选择理由和操作人。记录是不可变且幂等的；`fixture` 与 `business` 必须显式区分。完整字段
和可信源码包格式见
[M1-C 人工热点与 Overlay 制品链](m1-hotspot-overlay.md)。

### 4. 注册唯一 Candidate

```http
POST /v1/manual-candidate/tasks/<task-id>/candidates
Content-Type: application/json

{
  "hotspot_id": "<已登记热点的 UUID>",
  "baseline_epoch_id": "<summary 中的 baseline_epoch_id>",
  "source_hash": "sha256:<64-hex>",
  "optimization_intent": "replace the manually located LayerNorm hotspot",
  "replacement_point": "sglang.srt.layers.layernorm",
  "track": "triton",
  "release_mode": "overlay",
  "candidate_kind": "business",
  "idempotency_key": "m1-layernorm-candidate-v1"
}
```

注册成功会原子写入 Candidate 并排队 `manual_build`。同一幂等键和完全相同输入返回同一
Candidate；同一 Task 不允许注册第二个逻辑 Candidate。

实际 M1-C Builder 要求 `hotspot_id`；保留为空只用于尚未接入 C 侧的旧控制面契约测试，不能
产生真实 Overlay 制品。

### 5. 查看证据并签核

```http
GET /v1/manual-candidate/tasks/<task-id>/summary
POST /v1/manual-candidate/tasks/<task-id>/signoff
```

Signoff 必须绑定 D 最终写入的 EvidenceBundle。批准只会把 Task/Candidate 记为
`completed/accepted`，不会自动安装 Overlay，也不会把 `automatic_release_allowed` 改成
`true`。

## 本地验证

不需要 HCU 的 Contract 与状态机测试：

```bash
pytest tests/unit/test_m1_control_plane.py -q
```

Repository 并发、幂等、stale Worker 和不可变 Baseline 必须使用真实 PostgreSQL：

```bash
export HCUOPT_DATABASE_URL=postgresql://hcuopt:hcuopt@127.0.0.1:5432/hcuopt
pytest tests/integration/test_m1_control_plane_postgres.py -q
```

B 的可移植契约、no-op、已知信号、Stage 0 报告篡改和 Worker fail-closed 测试：

```bash
pytest tests/unit/test_m1_measurement.py tests/unit/test_m1_control_plane.py -q
```

nmz36 Target Lock 测试还需要 C 提供真实的 `M1WorkloadFactory`：它负责在锁定镜像中分别
启动干净 Baseline 和只读 startup Overlay Candidate，并返回 Artifact import attestation、
哈希化缓存证明和实现 B Event Record 协议的进程入口。
B 不接受 Job payload 传入命令、mount 或任意 Python 入口。

本地测试能验收 A 控制面和 B 证据生产逻辑；只有 C/D Real Adapter 接齐后，才运行 nmz36
上的 M1 Target Lock 实机闭环。

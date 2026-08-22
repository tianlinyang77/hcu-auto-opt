# M1-A 手工 Candidate 控制面

## 这阶段在做什么

M1-A 把一份人工提供的 Triton/Python Overlay 候选，放进一条可恢复、可审计的真实优化
流水线。它解决的不是“自动生成 Kernel”，而是先让团队对同一个 Target、同一个基线、
同一份候选和同一组证据说话。

```text
Formal Stage 0（DEGRADED_MANUAL_INTAKE）
  → Immutable Baseline Epoch
  → Manual Candidate Intake
  → Build（C）
  → Correctness（D 协议；共享 HCU 租约）
  → Raw Performance Measurement（B Harness；独占 HCU 租约）
  → Independent Adjudication（D）
  → EvidenceBundle
  → Human Signoff
```

当前代码完成的是 A 侧控制面、状态机、数据库、API 和公共契约。默认 Adapter Catalog
故意没有注册 M1 Real Profile，因此尚不能把 Fake Adapter 当真实 M1 跑通。B/C/D 的实现
接齐并注册同名 Profile 后，这条链才会开始领取真实 Job。

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
Artifact、正确性原始证据 URI 和性能 Raw Samples URI；漏掉任一项都不能写入 verdict。

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

### 3. 注册唯一 Candidate

```http
POST /v1/manual-candidate/tasks/<task-id>/candidates
Content-Type: application/json

{
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

### 4. 查看证据并签核

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

这两组测试通过只能验收 A 侧控制面；只有 B/C/D Real Adapter 接齐后，才运行 nmz36 上的
M1 Target Lock 实机闭环。

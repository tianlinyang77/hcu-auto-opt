# Walking Skeleton：四人并行前的公共脚手架

## 目的

Walking Skeleton 先证明整套控制流可以运行、失败可以恢复、接口可以替换。它使用 Fake Adapter，**不产生真实 HCU 性能结论，也不代表 Stage 0 已通过**。

```text
Task → Fake Stage 0 → Frozen Baseline → Fake Profile
     → Agent 生成 2 个 Candidate → Build Artifact
     → GPU 正确性淘汰 1 个 → 另 1 个进入 Performance
     → Round Winner → Fake E2E → AWAITING_SIGNOFF
```

## 一键运行

需要 Docker 与 Docker Compose：

```bash
docker compose up -d --build
docker compose run --rm api \
  hcuopt walking-demo --api-url http://api:8000 --external-workers
```

API 文档位于 `http://localhost:8000/docs`。Demo 正常结束时，Task 状态为 `awaiting_signoff`，两个 Candidate 中一个为 `rejected`，一个为 `release_candidate`。Fake performance/E2E 只驱动控制流：EvaluationRun 带 `synthetic=true`，`passed` 为空，并且不包含加速比、延迟、吞吐或置信区间。

## 已冻结的 platform-v1.1 边界

| 边界 | 生产者 | 消费者 | 当前替身 |
|---|---|---|---|
| Stage0Evidence / Hotspot | B | A、C、D | FakeProfiler |
| Candidate / BuildArtifact | C | A、D | FakeCandidateGenerator / FakeBuilder |
| MeasurementSeries | B | D | FakeMeasurementHarness（唯一入口，只返回 not_measured） |
| EvaluationRun / ExecutionAttempt | D/B | A | FakeEvaluator / Worker 执行记录 |
| Task / Job / Lease / Registry | A | B、C、D | PostgreSQL + FastAPI |

B、C、D 后续只替换 Adapter，不复制 Worker 的注册、Claim、Heartbeat、取消、失败重试和 Fencing 逻辑。

## 故障语义

- Job 使用 `idempotency_key` 防止重复创建。
- 每次领取生成新的 `claim_token`；旧 Worker 不能写回。
- GPU Job 同时获得递增的 `fencing_token`；旧租约不能操作设备。
- 正确性 Job 使用 `shared` 语义；只有 Profiler、Performance 和 E2E 申请 `exclusive` 安静拓扑。
- 心跳超时后，Reaper 先执行 Fencing/清理/健康检查，再重新排队。
- Job 完成和流程推进之间使用可重放标记；`/v1/maintenance/reconcile` 可补偿已完成但尚未推进的 Job。
- Baseline Epoch 冻结后，数据库拒绝 UPDATE 和 DELETE。

## 进入四人并行前的签署点

- A：Task、Job、错误码、幂等、Lease/Fencing 与数据库迁移。
- B：Stage0Evidence、Hotspot、Measurement Harness 输入输出。
- C：Candidate、BuildArtifact、构建错误与缓存键。
- D：正确性、Performance、E2E 和 Round 判定输入输出。

任一 Contract 变更需要 ADR、接口两侧负责人和 A 共同批准。

# F1-A Framework Smoke 控制面

## 定位

Framework Smoke 是性能优化之前的真实框架链路。它回答的是“锁定的 Target、源码、No-op 制品、执行、输出一致性和证据能否被同一套控制面可靠串起来”，不回答“Kernel 是否更快”。

当前 A 线只实现控制面、编排、持久化和 Adapter 注入边界。SSH、容器资源清理、Git Worktree、真实构建和 SGLang 请求分别由 #5、#6、#7 提供，不能在控制面中偷偷替换成 Fake。

## 完整链路

```text
POST FrameworkSmokeCreate
  -> 校验 Target Lock 和显式 Adapter Profile
  -> 原子创建 Task、TargetSnapshot、SOURCE_PREPARE Job
  -> SourceSnapshot + framework_smoke Baseline Epoch
  -> No-op Candidate + NOOP_BUILD Job
  -> Candidate SourceSnapshot + ArtifactManifest
  -> FRAMEWORK_SMOKE Job（独占资源 + fencing token）
  -> ExecutionRequest / ExecutionAttempt
  -> output equivalence EvaluationRun
  -> EvidenceBundle + cleanup/health evidence
  -> AWAITING_SIGNOFF
```

Framework Smoke 使用独立状态链，不复用 `PROFILING`、`SEARCHING` 或 `PERFORMANCE`，避免把框架功能验证误写成性能优化结果。

## 关键不变量

- 任务创建必须显式给出 `adapter_profile`；未知 Profile 或缺少六项能力时以 `AdapterUnavailable` 失败。
- Fake Profile 只能显式选择，所有结果必须为 `synthetic=true`，并固定输出 `performance_conclusion=not_measured`。
- Real Profile 遇到 Target Lock 中未关闭的 blocker 时以 `TargetNotReady` 失败。
- Job Claim 同时匹配 `worker_type + adapter_profile`；旧 Worker 不会领取 F1 Job。
- Target、Baseline、Source、Artifact、Execution、Evaluation 和 Evidence 均保存稳定关联。
- 同一个 Job Completion 重放使用稳定 idempotency key，不重复写 Evaluation 或 Evidence。
- Worker 物理重试沿用 Job 内固定的 `evaluation_run_id`；人工 Retest 使用新的 Run ID，并保留历史。
- Cancel 会取消排队/运行 Job、清空 Claim/Fencing 所有权；旧 Claim 不能再写回。
- 控制面在“Job 已成功、Workflow 尚未推进”之间崩溃时，Reconcile 会继续推进同一条链。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/targets` | 列出并完整校验 Target Catalog |
| GET | `/v1/targets/{target_id}` | 获取一个锁定 Target |
| GET | `/v1/adapter-profiles` | 查看当前可选 Profile 和能力 |
| POST | `/v1/framework-smoke/tasks` | 创建任务并原子写入首个 Job |
| GET | `/v1/framework-smoke/tasks/{task_id}` | 查看任务状态 |
| GET | `/v1/framework-smoke/tasks/{task_id}/summary` | 查看全链路关系和人工摘要 |
| POST | `/v1/framework-smoke/tasks/{task_id}/cancel` | 取消并使旧 Claim 失效 |
| POST | `/v1/framework-smoke/tasks/{task_id}/retest` | 创建新的有意复测 Run |

开发环境可以显式使用 `fake-v1-control-flow-only` 打通控制流。该结果不能作为 nmz36 环境、SGLang 功能或性能已经验证的证据。

## 接入 #5 / #6 / #7

三条实现线只需要实现已经冻结的 Adapter Protocol，并使用同一 Profile 名注册 Worker：

- #5：`SourceManagerAdapter`、`BuilderAdapter`、`ArtifactStoreAdapter`；
- #6：`ExecutionAdapter`、`ResourceCleaner`；
- #7：`EvaluatorAdapter` 和 Evidence 内容。

控制面不 fork 这些实现，也不把 Profile 缺失解释成可回退 Fake。真实 Profile 只有在六项能力、Target blocker 和 Contract Test 都通过后才加入默认 Profile Catalog。

#5 的确定性 Hash、No-op、Artifact Store 和清理语义见
[F1-C 源码与制品证据链](f1-c-source-artifact.md)。

# S0-A Target-bound Stage 0 控制面

## 定位

Stage 0 控制面不实现计时器、Profiler 或热补丁算法。它提供三条实现线共用的可信插座：
把探针绑定到不可变 Target Snapshot，通过 Durable Job、Lease/Fencing、原始证据 Hash 和
Adapter provenance 固化结果，并在七类探针齐全后打开 Barrier。

旧 `/v1/tasks/{task_id}/stage0` 仅服务 Fake Walking Skeleton。请求模型固定
`synthetic=true`，任务持久化为 `stage0_authority=synthetic`，报告明确包含
`evidence_authority=synthetic_control_flow_only`。它不能产生正式 Stage 0 授权。

## 对象关系

```text
Task (workflow_type=stage0)
  -> immutable TargetSnapshot
  -> Stage0Run (dry_run | formal)
     -> 7 x STAGE0_PROBE Job
        -> Stage0ProbeRecord
     -> READY barrier
     -> server-side finalize
        -> stage0_evidence
        -> ProjectMode
```

七类 Probe 固定为：`fingerprint`、`timer`、`noise`、`known_signal`、`null_signal`、
`profiler`、`hotpatch`。每种类型在同一个 Run 中只能有一条最终记录；Job completion 和
Workflow reconcile 重放不会重复写记录。

## Dry Run 与 Formal

- `dry_run` 用于验证控制流、探针格式和证据归档，可使用 Fake Adapter 或不申请独占资源；
  它只能停在 `READY`，调用 formal finalize 会被拒绝。
- `formal` 只接受 Real Adapter。每个 Probe Job 必须具有 `lease_scope=exclusive`、
  `lease_id`、`resource_id`、`fencing_token`、原始证据 URI/SHA256 和健康的 fence/cleanup；
  任一条件缺失时 ProbeRecord 不会进入 Barrier。

正式结果由服务端从已完成 Job 的 ProbeRecord 组合，不接受客户端直接提交
`measurement=pass`。当前结构判定包括 Known Signal 必须检出、Null Signal 不得误报，
以及 noise/profiler/hotpatch 的类型化能力字段。#18/#20 将在相同契约上补充测量复算与
统计门限，不需要重新声明公共对象。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/stage0-runs` | 创建绑定 Target Snapshot 的 Run 和七个 Probe Job |
| GET | `/v1/stage0-runs/{stage0_run_id}` | 查看 Run、Target、Job、Probe 和事件 |
| POST | `/v1/stage0-runs/{stage0_run_id}/finalize` | formal Barrier 后由服务端生成 Stage0Report |

ProbeRecord 没有面向普通客户端的直接写入口。Worker 通过现有 Job complete 接口提交
`Stage0ProbeResult`，`Stage0Coordinator` 校验 completed Job 身份后持久化。

## 最终模式

服务端继续复用 `evaluate_stage0()` 将证据归并为：

- `FULL_MVP`；
- `DEGRADED_MANUAL_INTAKE`；
- `CONFIG_ONLY`；
- `STOPPED_MEASUREMENT`。

正式 finalize 会把任务 `stage0_authority` 设为 `formal`；旧 Fake 入口只能设为
`synthetic`。所有模式均保持 `automatic_release_allowed=false`。

## F1 人工签核

Framework Smoke 新增 `/v1/framework-smoke/tasks/{task_id}/signoff`。请求必须绑定属于
同一 Task 且判定通过的 EvidenceBundle，并记录 decision、actor、reason 和 idempotency
key。批准进入 `COMPLETED`，拒绝进入 `REJECTED`；重复请求只能在输入完全一致时返回同一
Signoff，所有决定写入 `task_events`。

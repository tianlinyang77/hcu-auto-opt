# ADR-0002：Stage 0 是前置硬闸门

- 状态：Accepted
- 日期：2026-08-13

## 决策

先完成最小 Framework Gate：公共 Contract、Target Loader、可注入 Adapter、任务执行、日志/证据归档和真实 No-op 链路。随后立即验证计时分辨率、跨进程噪声/MDE、Profiler 能力和热补丁能力。测量失败时停止性能搜索和优化工程；Profiler 或热补丁失败按能力降级，不能用进度压力绕过。

## 原因

没有最小执行框架就无法稳定运行和留存 Stage 0 证据；如果环境无法分辨目标收益，后续搜索、评测和优化结论也没有意义。因此顺序固定为“最小框架 → 真实 No-op → Stage 0 → 优化工程”。Stage 0 必须先于搜索、Agent、Beam、真实候选和性能成果建设，但不先于承载它自身的最小框架。

## 测量证据不变量

2026-08-20 补充以下 fail-closed 约束：

- `timer_resolution_ns` 必须来自重复设备 Event 对的最小正有效间隔；设备时钟到宿主时钟的比例单独记录，禁止把比例冒充分辨率。
- Stage 0 原始证据按 `stage0_run_id/probe_type` 隔离发布；相同内容可以幂等重放，不同内容不得覆盖已有 URI。
- Worker 必须在探针启动前重新计算 Job Target Fingerprint，并与 Adapter 绑定 Target 一致；MeasurementPlan 的环境指纹必须由同一绑定 Target 派生。
- Formal Noise 至少包含一次可验证的进程重启。每个重启组记录 PID 与进程启动令牌，组间身份必须不同，工作负载关闭后必须确认进程已经退出。
- 旧版原始证据仍可读取，但没有实测分辨率或进程身份时不能成为新的 Formal Stage 0 证据。

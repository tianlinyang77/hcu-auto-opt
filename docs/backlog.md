# 首批 Backlog

## Framework Gate / P0

### A

- 冻结 platform-v1.1、Target Loader、Adapter Registry 和 Workflow 接口；
- 接通 Contract、API、Worker 和控制面错误语义；
- 维护公共 Fake Profile 和兼容测试。

### B

- 实现 SSH/Container ExecutionAdapter；
- 实现 Job 日志、超时、取消和进程生命周期；
- 实现真实 ResourceCleaner 与 HCU/NUMA 绑定。

### C

- 实现 SourceManager、固定 Commit 和独立 Worktree；
- 实现 No-op Builder、ArtifactManifest 和本地 ArtifactStore；
- 保证 Baseline 不可修改并输出 Hash 证据。

### D

- 定义 SGLang Workload/Smoke 接口；
- 实现 Baseline 与 No-op 输出一致性检查；
- 输出 EvidenceBundle 和 Framework Smoke 集成测试。

## Framework Gate 退出条件

- TargetSpec → SourceSnapshot → No-op Artifact → ExecutionResult → EvidenceBundle 跑通；
- 真实 SGLang Smoke 可启动、请求、停止并保存日志；
- 失败任务可以取消、Fencing、清理并恢复到已知状态；
- Fake 与真实 Adapter 都通过同一组 Contract Test。

完成以上条件后再进入 Stage 0 的计时、噪声、Profiler 和热补丁探针。

## GitHub Issue 模板

每个 Issue 至少包含：

```text
目标
为什么现在做
输入 / 输出 Contract
验收证据
是否需要 HCU / 独占租约
依赖和 Plan B
负责人 / Reviewer
```

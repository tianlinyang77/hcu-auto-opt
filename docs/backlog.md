# 首批 Backlog

## Week 1 / P0

### A

- 初始化仓库、CI、CODEOWNERS 和 ADR；
- 定义 Stage0Evidence、Task、Candidate、Artifact、Lease；
- 建立 Go/No-Go 记录和停止状态。

### B

- 实现 DCU 环境指纹；
- 实现唯一计时 Harness 的最小版；
- 输出分辨率、σ/CV/MDE；
- 完成 Profiler 能力探针。

### C

- 运行 Apex/Magpie 最小示例；
- 完成 Triton/独立 HIP 热补丁和恢复探针；
- 输出可用 Adapter 边界。

### D

- 定义 Stage 0 判定规则；
- 定义大信号夹具；
- 起草 MeasurementRecord、Holdout 和 ABBA 协议。

## Week 2 / P0

- PostgreSQL Schema 与 Alembic；
- `SKIP LOCKED` Job Queue；
- Worker 注册、心跳、领取、重试；
- Lease/Fencing 状态机和 DCU 清理接口；
- Baseline Epoch；
- Artifact Store 接口；
- Mock 端到端测试。

## GitHub Issue 模板

每个 Issue 至少包含：

```text
目标
为什么现在做
输入 / 输出 Contract
验收证据
是否需要 DCU / 独占租约
依赖和 Plan B
负责人 / Reviewer
```


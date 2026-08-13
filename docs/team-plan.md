# 四人建设计划

## 责任分配

### A：架构与控制面 DRI

- 领域 Contract、ADR、数据库与迁移；
- Orchestrator、PostgreSQL Job Queue；
- Baseline Manager；
- Lease/Fencing 状态机；
- Candidate Registry、签核材料、回滚事件模型；
- Stage 0 Go/No-Go 主持和项目停止状态。

### B：DCU 测量 DRI

- 唯一 Measurement Harness；
- 环境指纹、计时分辨率、σ/CV/MDE；
- DCU Profiler Adapter 与热点数据；
- DCU Fencing 清理和健康检查实现；
- Measurement/Topology Lease 的设备执行规则。

### C：搜索和构建 DRI

- Apex/Magpie 探针和 Adapter；
- 热补丁与恢复探针；
- Candidate Intake、上游查重、Beam/Top-K；
- Agent/Build 安全域、AST 反作弊、Build Cache；
- Artifact、Hash 和 SBOM。

### D：正确性和统计 DRI

- 测量协议和 Go/No-Go 判定方法；
- Kernel 正确性、Shape/Holdout；
- Round Barrier 与 FDR/FWER；
- 模型正确性、ABBA E2E；
- 二分消融和收益归因。

D 只消费 B 的 Harness，不实现计时底层。Registry、签核和回滚元数据由 A 负责。

## 8 周里程碑

| 周 | 目标 | 关键交付 / 退出条件 |
|---|---|---|
| 1 | 前置闸门 | 分辨率、σ/CV/MDE、环境指纹；Profiler 和热补丁探针；Go/No-Go |
| 2 | 公共骨架 | Contract、PostgreSQL 队列、Worker、Baseline、Lease/Fencing、Mock 流 |
| 3 | 夹具闭环 | RMSNorm 正确性夹具 + 大信号性能夹具，证明管道而非业务收益 |
| 4 | 真实热点 | 冻结 Workload/Epoch，真实 Profiling、调用路径、机会评分、目标选择 |
| 5 | 真实候选 | Apex/Agent、最小 Beam、隔离构建、正确性、可信 Kernel 结果 |
| 6 | 轮次可信度 | Search/Holdout、Barrier、FDR/FWER、超时与失败证据 |
| 7 | 模型与 E2E | Logits、Token smoke、自回归、ABBA、消融与归因 |
| 8 | 恢复与交付 | Fencing 故障注入、Registry、签核材料、一键复现和限制说明 |

第 1 周未通过不得进入第 2 周的性能系统扩张。第 6～8 周允许按风险滚动调整，但不可降低 Stage 0 和正确性门槛。

## 周工作方式

- 周一：冻结本周端到端切片和验收证据；
- 周三：接口合流，暴露 Contract 冲突；
- 周五：运行一条更完整的端到端链，失败也必须保存证据；
- 每个实验 Issue 必须区分“探针”“夹具”“业务优化成果”。


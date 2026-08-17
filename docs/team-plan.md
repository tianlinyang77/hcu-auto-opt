# 四人建设计划

## 正式分工前：Stage -0.5 Walking Skeleton

先由 A 搭建公共脚手架，B/C/D 审核并签署各自接口，再进入并行开发。脚手架可以使用 Fake Adapter 验证 Task、Job、Worker、Artifact、Evaluation 和 Lease/Fencing 控制流，但不得把 Fake 数字当作 Stage 0 或性能成果。

退出条件：空库迁移成功、三类 Worker 可注册和领任务、旧 Claim/Fencing Token 被拒绝、Baseline 不可修改、Demo 在 10 分钟内到达 `AWAITING_SIGNOFF`。详见 [Walking Skeleton](walking-skeleton.md)。

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

## 依赖顺序（不作为固定排期）

| 阶段 | 目标 | 关键交付 / 退出条件 |
|---|---|---|
| F0 | 公共接口 | platform-v1、Target Loader、Adapter/Workflow 注入、Contract 测试 |
| F1 | 真实 No-op 框架 | SSH/Container、固定源码、No-op Artifact、SGLang Smoke、证据归档 |
| S0 | 能力闸门 | 分辨率、σ/CV/MDE、环境指纹；Profiler 和热补丁探针；Go/No-Go |
| M1 | 手工候选闭环 | 手工 Candidate、隔离构建、正确性和可信性能结果 |
| M2 | 搜索轮次 | Search/Holdout、Barrier、FDR/FWER、预算和失败证据 |
| M3 | 模型与 E2E | Logits、Token smoke、自回归、ABBA、消融与归因 |
| M4 | 恢复与交付 | Fencing 故障注入、Registry、签核材料、一键复现和限制说明 |

F1 未完成不运行 Stage 0；S0 未通过不投入搜索、Agent、Beam 和真实优化候选，但不得用 S0 阻止完成承载它的 F0/F1 框架。

## 周工作方式

- 周一：冻结本周端到端切片和验收证据；
- 周三：接口合流，暴露 Contract 冲突；
- 周五：运行一条更完整的端到端链，失败也必须保存证据；
- 每个实验 Issue 必须区分“探针”“夹具”“业务优化成果”。

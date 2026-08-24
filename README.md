# HCU Auto Opt

面向 HCU 推理工作负载的自动性能优化平台。系统从真实 Workload 出发，发现瓶颈、生成候选、隔离构建、可信评测，并把可复现证据交给人工签核。

仓库已经完成 F1 Framework Smoke 和 Formal Stage 0，当前进入 **M1 手工 Candidate 闭环阶段**。Formal Stage 0 的模式是 `DEGRADED_MANUAL_INTAKE`：允许人工定位并提交候选，但不开放 Agent 搜索、自动发布或生产灰度。当前第一步是冻结 Baseline、Candidate、Build、正确性、测量、独立裁决和签核的公共接口，再由 B/C/D 接入真实 Adapter。

首个实测目标已经冻结为 SGLang 0.5.12、`HYGON-AI/sglang-das` 固定 Commit 和指定 DTK 26.04 镜像；精确版本、运行拓扑及待解除阻塞见 [nmz36 Target Lock](config/targets/nmz36-sglang-0.5.12.yaml)。Target Lock 使用镜像 digest 与完整源码 Commit，禁止用同名 Tag、`latest` 或其他 0.5.12 镜像替换。

## 当前可运行闭环

仓库已经提供 FastAPI 控制面、PostgreSQL Job Queue、Agent/Build/GPU 三类 Worker、Baseline Epoch、Claim/Fencing Token、Target Catalog、Adapter Profile 和 Framework Smoke Workflow。Fake Demo 与显式 `fake-v1-control-flow-only` Profile 可以跑到 `AWAITING_SIGNOFF`，但只验证控制流，不产生任何性能结论。

```bash
docker compose up -d --build
docker compose run --rm api \
  hcuopt walking-demo --api-url http://api:8000 --external-workers
```

API 文档：`http://localhost:8000/docs`。详细说明见 [Framework Smoke 控制面](docs/framework-smoke-control-plane.md) 和 [Walking Skeleton](docs/walking-skeleton.md)。

M1-A 已提供独立的单候选控制面和 PostgreSQL 契约，但默认 Adapter Catalog 暂不注册 M1 Real Profile；这会阻止任何 Fake 结果冒充真实优化证据。接口和接线边界见 [M1-A 手工 Candidate 控制面](docs/m1-control-plane.md)。

## MVP 范围

- 1 种 HCU、1 个模型、1 个推理框架版本、1 套冻结 Workload。
- 配置优化与可热补丁的 Triton / 独立 HIP 制品。
- 人工签核，不做无人值守生产发布。
- 系统库、驱动、整仓重编译和多机调度进入阶段二。

## 前置 Go/No-Go

最小 Framework Gate 和真实 No-op 链路跑通后、性能搜索与优化工程开始前，必须获得三组证据：

1. 测量：环境指纹、有效计时分辨率、跨进程噪声 σ/CV/MDE。
2. Profiler：当前 DTK 环境能否获得可用的 Kernel 级数据和调用信息。
3. 热补丁：候选能否替换、运行、验证并恢复基线。

测量闸门失败时，任何人均可提出停止；B 可直接停止性能实验；A 是项目 Go/No-Go DRI。恢复需要 A、B、D 三方确认。

## 体系结构

```mermaid
flowchart LR
    CLI["CLI / API"] --> CTRL["控制面\nOrchestrator"]
    CTRL --> DB[("PostgreSQL\n任务 · 候选 · 实验 · 基线")]
    CTRL --> BM["Baseline Manager\n基线管理"]
    CTRL --> LM["Lease + Fencing\n租约与机器恢复"]
    CTRL --> Q["PostgreSQL Job Queue"]

    Q --> AW["Agent Worker\n无 GPU"]
    Q --> BW["Build Worker\n非 Root · 受限网络"]
    Q --> GW["GPU Worker\n不运行 Agent"]

    PROF["HCU Profiler Adapter"] --> HOT["热点与机会评分"]
    HOT --> AW
    AW --> BW
    BW --> GW
    GW --> ROUND["Round Barrier\nHoldout · FDR/FWER"]
    ROUND --> MODEL["模型正确性"]
    MODEL --> E2E["批次 E2E · ABBA"]
    E2E --> REG["Candidate Registry\n证据与人工签核"]

    LM --> GW
    BM --> PROF
    DB --> CTRL
```

更完整的模块、状态和数据流见 [整体架构](docs/architecture.md)。

## 四人责任边界

| 角色 | 主责 | 核心交付 |
|---|---|---|
| A 控制面 DRI | 领域契约、状态机、DB 队列、Baseline、Lease/Fencing、Registry | 可恢复的任务和证据链 |
| B 测量 DRI | 唯一计时 Harness、环境指纹、噪声/MDE、Profiler、HCU 清理探针 | 回答“怎样准确测” |
| C 搜索与构建 DRI | Apex Adapter、候选搜索、Agent/Build 沙箱、Build Cache、Artifact | 安全地产生不可变候选 |
| D 判定 DRI | 正确性、Holdout、FDR/FWER、模型验证、ABBA、消融 | 回答“对不对、真快没快” |

D 必须调用 B 提供的唯一测量 Harness，不允许再实现第二套计时逻辑。详细分工与周计划见 [团队建设计划](docs/team-plan.md)。

## 本地运行

当前骨架只依赖 Python 标准库即可运行核心测试：

```bash
python -m unittest discover -s tests/unit -v
python -m hcuopt.cli stage0-evaluate examples/stage0-pass.json
```

安装开发依赖后：

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

PostgreSQL 并发测试不会使用 SQLite 替代：

```bash
docker compose up -d postgres
set HCUOPT_DATABASE_URL=postgresql://hcuopt:hcuopt@localhost:5432/hcuopt
pytest tests/integration -m postgres
```

不使用 Docker 时，也可以分别启动控制面和内嵌 Worker Demo：

```bash
set HCUOPT_DATABASE_URL=postgresql://hcuopt:hcuopt@localhost:5432/hcuopt
hcuopt db-migrate
hcuopt api
hcuopt walking-demo --api-url http://localhost:8000
```

## 文档入口

- [整体架构](docs/architecture.md)
- [MVP 范围](docs/mvp-scope.md)
- [Stage 0 Go/No-Go](docs/stage0-go-no-go.md)
- [测量协议](docs/measurement-protocol.md)
- [领域契约](docs/contracts.md)
- [公共接口层](docs/public-interfaces.md)
- [F1-A Framework Smoke 控制面](docs/framework-smoke-control-plane.md)
- [S0-A Stage 0 控制面](docs/s0-control-plane.md)
- [S0-C Profiler 与可逆 Overlay 能力探针](docs/s0-runtime-probes.md)
- [M1-C 人工热点与启动时 Overlay 制品链](docs/m1-hotspot-overlay.md)
- [M1-A 手工 Candidate 控制面](docs/m1-control-plane.md)
- [状态机](docs/state-machine.md)
- [团队与排期](docs/team-plan.md)
- [Walking Skeleton](docs/walking-skeleton.md)
- [首批 Backlog](docs/backlog.md)
- [贡献流程](CONTRIBUTING.md)

## 参考项目策略

- Apex：通过 Adapter 复用工作流和候选生命周期，不 fork 到核心代码。
- Magpie：作为评测底座候选，必须经过 HCU 适配与测量协议约束。
- KernelAgent：借鉴诊断、Roofline 和 Beam/Top-K 方法，不直接搬运 CUDA/NCU 实现。

公开项目不等于可直接上线的生产控制面；HCU 工具链、可信测量、Baseline Epoch、资源 Fencing、统计裁决和发布证据仍是本项目的核心工程。

# 公共接口层（platform-v1.2）

## 目的

公共接口层是四条开发线之间唯一允许共享的数据和调用边界。它解决“控制面怎样调用
执行、源码、构建和评测模块”。`platform-v1.2` 仍是 F1 通用底座；M1 已在其上通过专用
Contract 和 opt-in Real Profile 完成一次真实 Build、正确性、性能测量、独立裁决和人工
签核。M1 性能证据不反向改写 `platform-v1.2`，M2 也必须通过版本化新 Contract 增量演进。

当前版本为 `platform-v1.2`。它在 v1.1 上增加了 Target blocker 作用域，以及
Framework Smoke 的 baseline/noop 双执行身份；变更依据见
[ADR-0004](adr/0004-framework-smoke-dual-execution.md)。代码位于：

- `src/hcuopt/contracts/platform_v1.py`：跨模块数据契约；
- `src/hcuopt/targets/`：Target Lock 加载与校验；
- `src/hcuopt/adapters/interfaces.py`：外部能力 Protocol；
- `src/hcuopt/adapters/registry.py`：Adapter 显式注册；
- `src/hcuopt/adapters/profiles.py`：Task/Worker 共用的 Profile 能力目录；
- `src/hcuopt/workflows/interfaces.py`：Workflow 注入边界；
- `src/hcuopt/workers/handlers.py`：Job 到 Adapter 的薄路由层。

## 核心契约

| 契约 | 生产者 | 消费者 | 不变量 |
|---|---|---|---|
| `TargetSpec` | A/B | 所有模块 | 镜像使用 digest，源码使用完整 Commit，禁止自动发布 |
| `ExecutionRequest` | A/D | B | `argv` 是数组而不是 shell 字符串；租约执行必须带资源和 fencing token |
| `ExecutionResult` | B | A/D | 状态、退出码、起止时间和日志 URI 完整；Fake 必须标记 synthetic |
| `AdapterProvenance` | Adapter Owner | A/D/Registry | Profile、能力、实现名、版本和真假类型可审计 |
| `MeasurementSeries` | B | D/A | 原始样本 URI/Hash、协议和环境指纹完整；Fake 只能是 not_measured |
| `EvaluationRun` | D/A | Registry | 有意复测是新 Run，消息重放由 idempotency key 去重 |
| `ExecutionAttempt` | B | A/Registry | 物理重试属于同一个 Run；Framework Smoke 显式标识 baseline/noop |
| `SourceSnapshot` | C | A/B/D | Commit、Tree Hash、Source Hash 和 Worktree 可追溯 |
| `ArtifactManifest` | C | A/D | 内容 Hash、构建配方、SBOM/签名位置和 synthetic 标记完整 |
| `EvidenceBundle` | D | A/Registry | 绑定 Task、Target、Baseline、Candidate、协议版本和原始证据 |
| `Stage0Run` | A | B/C/D | 绑定不可变 Target Snapshot，区分 Dry Run 与 Formal |
| `Stage0ProbeResult` | B/C | A/D | 必须来自 completed Job；Formal 需要真实来源、独占租约和原始证据 Hash |

所有输入模型默认 `extra=forbid`。新增或改名字段必须走 ADR、上下游 Reviewer 和兼容测试，不能在各自模块重新声明同名结构。

F0.5 的证据边界和数据库升级说明见 [可信证据契约](f0-5-trust-contracts.md)。

## Target Lock

Target Lock 是可执行配置，不是说明文档：

```bash
hcuopt target-validate config/targets/nmz36-sglang-0.5.12.yaml
```

Target Loader 会拒绝 Tag-only 镜像、digest 不一致、非完整 Git Commit、路径穿越和自动发布；`ExecutionRequest` 会拒绝不完整租约和非 digest 容器镜像。移动分支只用于说明来源，执行时始终使用锁定 Commit。

## Adapter Registry

Worker 不再在构造函数中写死 Fake Adapter，而是接收一个显式 `AdapterRegistry`：

```python
registry = AdapterRegistry(
    profile="nmz36-framework-smoke-v1",
    executor=real_executor,
    resource_cleaner=real_cleaner,
)
worker = Worker("gpu-1", WorkerType.GPU, api_url, adapters=registry)
```

未注册的 Adapter 必须以 `AdapterUnavailable` 失败，禁止静默退回 Fake。现有 Demo 显式使用 `fake-v1-control-flow-only`，并继续只验证控制流。

## 四条实现线

| 目录/接口 | Owner | 第一实现 |
|---|---|---|
| `targets/`、`workflows/`、Registry | A | Target Catalog、Workflow 选择和 API/DB 接线 |
| `ExecutionAdapter`、`ResourceCleaner` | B | SSH/Container Executor 与真实 Fencing |
| `SourceManagerAdapter`、`BuilderAdapter`、`ArtifactStoreAdapter` | C | 固定 Commit、Worktree、No-op Artifact |
| `EvaluatorAdapter`、`EvidenceBundle` | D | SGLang Smoke、输出一致性和证据归档 |

## Framework Gate

公共接口层完成后，下一条集成链是：

```text
TargetSpec → SourceSnapshot → No-op Artifact
→ Baseline/No-op 双 ExecutionRequest → 双 ExecutionResult → EvidenceBundle
```

该链在 nmz36 的锁定镜像中跑通、失败可恢复、证据可归档后，才进入 Stage 0。Stage 0 拦截性能搜索和优化工程，不拦截搭建这条必要的执行框架。

`SourceManagerAdapter` 还负责 Candidate Worktree 的正常回收与异常恢复；清理只能作用于
受管 Candidate 目录，完成后必须重新校验 Baseline 未发生变化。具体语义见
[F1-C 源码与制品证据链](f1-c-source-artifact.md)。

A 线的持久化、状态、API、取消、复测与 Reconcile 已进入 F1 实现，详见
[Framework Smoke 控制面](framework-smoke-control-plane.md)。默认目录同时声明 Fake 和
`nmz36-framework-smoke-v1`；真实任务仍须通过相应 Target blocker 作用域。

F1 之后的 Stage 0 已建立 Target-bound 控制面和七探针 Barrier，接口与真假证据边界见
[S0-A Stage 0 控制面](s0-control-plane.md)。

## M1 与 M2 接口边界

M1 专用接口位于 `src/hcuopt/contracts/v1.py`、`src/hcuopt/contracts/m1.py`、
`src/hcuopt/measurement/m1_harness.py` 和 `src/hcuopt/evaluation/m1_verifier.py`。真实 M1
Profile 必须由部署方显式组合 A/B/C/D Adapter；默认 Catalog 不提供自动回退。首份真实
证据已签核，因此 `m1-kernel-performance-evidence-v1` 按只读兼容边界管理。

M2a 的无 HCU Scripted 控制面已经实现 `SearchRound`、2–4 Candidate Intake、Candidate/Artifact
Family Freeze、原子 Budget Ledger、Search/Holdout Barrier、Holdout Reveal、Bonferroni FWER、
递归 Round Evidence 和 `scripted_completed`。写入顺序与 Authority 边界见
[M2 Scripted Round 持久化与最终化](m2-scripted-finalizer.md)，完整字段见
[M2 Contract 草案](m2-contract-draft.md)。

这些接口仍不是 Formal HCU 入口：默认服务不配置 synthetic Finalizer，必须由部署方或后续
Scripted Coordinator 显式注入 Evidence Reader；synthetic Evidence 不能进入 Formal Signoff。
面向 CLI/Web 的 Operator Facade 继续以版本化 Profile、Plan Preview 和可重建 Read Model 提供
受控外观，不复制领域状态；草案见 [M2 Operator Contract 草案](m2-operator-contract-draft.md)。

OX-1 已提供 `m2-operator-v1` 的 synthetic Profile Registry 和持久化 Plan Preview。当前默认
Catalog 只注册 Scripted Target/Workload/Measurement 三类不可变 Profile；公共接口包括
`GET /v1/operator/identity`、Profile list/show，以及
`POST /v1/operator/round-plans:preview`。Preview 只在 PostgreSQL Authority 与 Candidate Package
全部核验后返回 `start_allowed=true`，否则保存明确阻塞项；它固定 synthetic、禁止自动发布，
不运行 HCU、不创建 Task/SearchRound，也不生成 Holdout nonce/commitment。

部署若未注入与 Target Profile 完全匹配的 Candidate Package Store，Preview 会安全阻塞。

OX-1 的 durable Scripted StartIntent 通过
`POST /v1/operator/round-plans/{preview_id}:start` 和
`GET /v1/operator/start-intents/{intent_id}` 提供。它只消费不可变 Preview，并复用现有
SearchRound/Candidate/Intake Close 权威接口；D-owned Scripted Plan Authority 由部署显式注入，
未配置时安全失败。StartIntent finalized 只允许后续控制面继续处理，不代表已经执行 Build、
测量或裁决。CLI 和 Summary/Report 已提供 synthetic 操作闭环，但它仍不是 Formal 或真实
一键优化完成声明。

OX-1 CLI 和 Operator Read Model 使用同一 API：

- `GET /v1/operator/search-rounds/{round_id}/summary`
- `GET /v1/operator/search-rounds/{round_id}/report`
- `GET /v1/operator/search-rounds?limit=<1..100>`（只列出 finalized synthetic StartIntent）
- `hcuopt profile list|show`
- `GET /v1/operator/workloads`、`GET /v1/operator/workloads/{id}/versions/{version}` 与
  `hcuopt workload list|show`
- `GET /v1/operator/hotspots`、`GET /v1/operator/hotspots/{hotspot_id}` 与
  `hcuopt hotspot list|show`
- `hcuopt round draft|plan|start|status|report|run`

`round draft` 从可信 Profile、Hotspot Authority 和已验证 Candidate Package 生成 Plan Spec；后续
命令从 Preview/Start 文件自动提取 ID 与 Hash，不要求人工复制。`round run` 另存非性能性质的
真实操作成本指标；部署配置和 fail-closed 边界见 [OX-1 Scripted Operator CLI](operator-cli.md)。

## M2b Agent/Apex Proposal 接口

M2b 的第一层公共接口位于 `src/hcuopt/contracts/agent_v1.py` 和
`CandidateGeneratorAdapter.generate_proposals()`。输入固定 Target、Stage 0、Baseline、Hotspot、
Workload、Profiler Evidence 与 `KnowledgeSnapshot`；输出只能是带 Patch/意图/风险/provenance
的 `CandidateProposalBatch`。

Proposal 固定需要人工复核、禁止 Formal Intake、没有性能结论且不能自动发布。Apex Plan 只
拥有 generator、并发、有限重试、去重和 generation budget；HCU、Measurement、Holdout、
Barrier、FWER、Signoff 和 Release 均不在该 Protocol 中。完整决定见
[ADR-0011](adr/0011-m2b-agent-apex-proposal-boundary.md)。

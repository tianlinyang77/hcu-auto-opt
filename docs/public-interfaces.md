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
的 `CandidateProposalBatch`。每个 Proposal 和 Batch 都必须保存完整 `request_hash`，消费方从权威
Store 重读 `CandidateGenerationRequest` 后调用 `candidate_generation_request_hash()` 复算；只有
`request_id` 相同而 Hash 不同必须 fail closed。

`src/hcuopt/agent/patch_identity.py` 是 `normalized_patch_v1` 的唯一实现：输入必须是 strict UTF-8
且不得含 NUL；CRLF/CR 统一为 LF，删除 Git `index` 行，删除 `---`/`+++` 行 Tab 后时间戳，去除
每行末尾空格与 Tab，最终只保留一个尾部 LF。C 和 D 必须重读 `patch_uri` 原始字节并分别复算
原始 SHA256 与规范化 SHA256，不能接受生成器自报结果，也不能复制一套私有规范化函数。

Proposal 固定需要人工复核、禁止 Formal Intake、没有性能结论且不能自动发布。Apex Plan 只
拥有 generator、并发、有限重试、去重和 generation budget；HCU、Measurement、Holdout、
Barrier、FWER、Signoff 和 Release 均不在该 Protocol 中。完整决定见
[ADR-0011](adr/0011-m2b-agent-apex-proposal-boundary.md)。

人工决策必须写入 `CandidateProposalReviewRecord`，冻结 Proposal/Request Hash、原始/规范化
Patch Hash、Baseline、Hotspot、replacement point、审核人、决定、原因、时间、幂等键和审核
证据。只有 approved 记录可生成 `CandidateProposalPromotionReceipt`。晋级回执仍固定
`formal_intake_allowed=false`，只引用现有 `CandidateSourcePackageRef`、M2a
`source_family_hash` 及 `BusinessCandidateFamilyVerifier` 的持久化证据/Provenance；后续 Formal
Intake 必须继续消费既有 M2a Family Authority，不能把回执本身当成 Candidate 或 Family。
Review 与 Promotion ID 均绑定完整记录内容；Store 还会冻结幂等键到内容 Hash 的映射，并在
每次重读时复算内容 ID 与其引用 Evidence，防止发布后改写决定或回执。

C 的实现边界位于：

- `src/hcuopt/adapters/agent_knowledge.py`：按内容 Hash 保存 Knowledge source，并从部署方 Store
  独立重读 Snapshot/Source Hash；Skill 只能作为带版本和许可证的只读知识输入，不能被导入为
  运行时代码；
- `src/hcuopt/adapters/agent_generator.py`：Proposal Patch/Batch 的不可变内容寻址 Store，以及供
  CI 使用的 deterministic synthetic Generator；
- `src/hcuopt/adapters/agent_promotion.py`：消费 A 的 `awaiting_review` + retained Proposal，重读
  Batch/Patch、记录不可变人工决策，通过现有 `SourceManagerAdapter` 创建并清理隔离 Candidate
  Worktree，在完整 Baseline 源码树上应用单文件受限 Patch，并发布现有 M1/M2a
  `CandidateSourcePackageManifest` / `CandidateSourcePackageRef`；其中 `candidate_source_hash`
  是应用 Overlay 后的完整 Candidate Worktree Hash，不是 Overlay 文件目录 Hash；
- `src/hcuopt/adapters/business_candidate_family.py`：继续作为 2–4 个 business Package 的唯一
  source-family Verifier，并公开真实 Adapter Provenance 供 Promotion Receipt 冻结。

受控晋级固定为两阶段：

```text
retained Proposal
  → immutable human Review
  → reviewed Candidate Source Package
  → 2–4 member Business Family verification
  → Promotion Receipt
```

Proposal、Review Record 和 Promotion Receipt 始终是不同对象。Package 发布和 Family 验证不会
触发 Build、HCU、Measurement 或 Formal Intake；fake/synthetic、未保留、未批准、Baseline 漂移、
越界路径及 Family Authority 漂移全部 fail closed。

A 的 Generation Authority 另外公开 `GenerationRunStartRequest`、`GenerationRun`、
`GeneratorAttempt`、`GenerationAttemptClaim`、`GenerationBudgetLedgerEntry`、
`CandidateProposalRef` 与 `GenerationRunStatusView`。CLI 是：

- `hcuopt agent-generation-start <start-request.json>`；
- `hcuopt agent-generation-status <generation-run-id>`；
- `hcuopt agent-generation-reconcile <generation-run-id>`。

它们只管理无 HCU 的 Proposal 生成状态。数据库迁移仍由 `hcuopt db-migrate` 显式执行；FastAPI
不暴露对应写路由。Proposal 在所有 generator 收敛前保持 `pending`，避免把并发完成顺序误当成
去重权威；barrier 后的 retained/duplicate 仍需 D 独立复算。

B 的 `AgentRunnerAdapter` 位于 `src/hcuopt/adapters/agent_runner.py`，是上述 Generator 下面的
非持久化执行边界。它接收绝对 executable、结构化 argv、白名单环境、只读输入和单次生成预算，
返回 Proposal bytes 与 `AgentRunEvidence`；不直接创建 `CandidateProposalBatch`。Local-command
实现同时校验 executable/argv prefix allowlist、禁止覆盖内部环境变量、禁止 shell，并在 input、
timeout、输出/token 超限、畸形 usage、非零退出或 cleanup 未证实时
failure closed。Deterministic 实现仅用于 synthetic CI。该接口不扩展 `agent_v1`，也不拥有
Candidate、Package、HCU、Measurement、Holdout 或发布权限。

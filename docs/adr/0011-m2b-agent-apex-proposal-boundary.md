# ADR-0011：M2b Agent/Apex 只生成待审 Proposal

- 状态：Proposed / dev-only Contract implementation authorized
- 日期：2026-08-31
- 跟踪：GitHub Issue #112、#113、#114、#115、#116
- 依赖：ADR-0009、ADR-0010
- 授权边界：项目所有者把无 HCU Agent/Apex MVP 提前到本周；真实 Formal 激活仍由 #102 单独授权

## 背景

M2a 已有 Scripted Search/Holdout、Barrier、FWER、Evidence 和 Formal Signoff 地基，但真实业务
Candidate Family、Formal Adapter、Real Profile 和首轮真实 Formal 尚未完成。原计划要求真实
M2a 先跑通再开始 M2b，目的是避免同时调试候选生成、测量和统计控制。

当前项目决定提前建设 Agent/Apex 软件能力，但不能因此把开发生成结果变成 Formal Candidate，
也不能让 Agent 越过已经形成的测量、证据和签核边界。因此需要一个独立的 Proposal 层：先把
生成、调度、去重、预算和 provenance 做成可测试的上游，再由现有 C Intake 和 M2a 控制面承接。

## 决策

### 1. Agent 输出 Proposal，不输出权威 Candidate

`CandidateGeneratorAdapter` 只接收不可变 `CandidateGenerationRequest`，输出
`CandidateProposalBatch`。Proposal 保存优化意图、理由、风险、Patch URI/Hash、规范化 Patch
Hash、触碰路径和生成来源。Proposal 与 Batch 都保存完整 `request_hash`；C/D 必须从权威 Store
重读 Request 并复算，不能只信任 `request_id`。Proposal 还固定：

- `review_required=true`；
- `formal_intake_allowed=false`；
- `performance_conclusion=not_measured`；
- `automatic_release_allowed=false`。

Proposal 不是 SourceSnapshot、Candidate、Artifact 或性能结论。C 必须从批准的 Proposal 重新
读取 Patch、验证 Baseline/Hotspot/replacement point、重建源码并发布内容寻址业务 Package。
只有这个 Package 才能进入 #110 的 source family verifier 和后续 M2a Intake。

人工决定使用 `CandidateProposalReviewRecord`，同时冻结 Proposal/Request Hash、原始/规范化
Patch Hash、Baseline Epoch/Source Hash、Hotspot、replacement point、审核人、决定、原因、时间、
幂等键和审核证据。rejected 或缺少审核记录的 Proposal 不能晋级。

approved Proposal 的受控晋级使用 `CandidateProposalPromotionReceipt`。回执只能引用既有
`CandidateSourcePackageRef`、`BusinessCandidateFamilyVerifier` 产生的 `source_family_hash` 和
持久化 verifier 证据/Provenance，不定义第二套 Candidate、Artifact 或 Family。回执本身继续
固定 `formal_intake_allowed=false`；后续权威 Intake 仍以现有 M2a Family 为唯一输入。

### 2. Skills 形成不可变知识快照，但没有系统权威

`KnowledgeSnapshot` 只保存批准的 skill、仓库文档和 Operator Evidence 的名称、版本、来源、
许可证与内容 Hash。产品运行时不从用户 Skills 目录 import 代码，`runtime_code_import_allowed`
固定为 false。知识可以影响 Agent 推理，但不能覆盖 Target、Stage0Run、Baseline、Hotspot、
Measurement Plan、Holdout 或 Evidence Authority。

### 3. Apex-like 只负责编排生成

`ApexGenerationPlan` 冻结 generator 列表、Adapter Profile、并发、超时、有限重试、去重策略和
生成预算。Apex-like Coordinator 可以启动多个 Agent、等待全部 attempt 终态、规范化 Patch、
去重并生成审阅顺序；它不能执行 Build、正确性、Search/Holdout 测量、Barrier、FWER、Signoff
或发布。

### 4. 生成预算与 HCU/Round 预算物理分离

`GenerationBudget` 只限制 generator attempt、墙钟、输出字节、token 和 Proposal 数。它不得
复用或修改 Round Budget Ledger，也不产生 HCU Lease 秒数。B 的 Agent Runner 固定无 HCU、无
Holdout 权限，并对超时、子进程、输出超限和临时目录执行 failure-closed cleanup。
本地生成器无论超限、失败还是根进程正常退出，Runner 返回前都必须验证整个受管进程域已退出；
Windows 使用启动前绑定的 kill-on-close Job Object，POSIX 使用独立进程组。

#### B 线运行时接口

`AgentRunnerAdapter` 是非持久化运行时接口，不是 `agent_v1` 的第二套 Proposal Contract。一次
`AgentRunRequest` 只包含 attempt/run 身份、完整 Generation Request Hash、绝对 executable、
不可变 Generator Artifact 路径与预期内容 Hash、结构化 argv、白名单环境变量、只读输入文件和
attempt/time/input/stdout/stderr/total-output/token 上限。实现必须同时校验 executable、
Generator Artifact 内容 Hash 和 argv prefix allowlist，使用 `shell=false`；工作目录由 Adapter
创建，不能由调用方指向仓库、Holdout 或部署目录。调用方不能覆盖 Runner 自有的 input、usage、
request hash 和 Windows 系统环境变量。所有浮点预算必须是有限值，整数预算拒绝 bool/float 等
运行时类型漂移。

`AgentRunResult` 只返回可选 Proposal bytes 和公共版本化 `RunnerExecutionRecord`。Record 冻结 `attempt_id`、
`generation_run_id`、Generation Request Hash、attempt number、Runner provenance/profile identity、
Generator Artifact Hash 与实际 executable Hash，同时记录墙钟、输出字节、reported token、退出码、
argv/input Hash、脱敏摘要、进程树终止和临时目录清理状态，并固定
`performance_conclusion=not_measured`。环境值、凭据、原始 Holdout 和 HCU/Round Budget 不得进入
Record。timeout、输出/token 超限、非零退出、usage 畸形或 cleanup 未证实均不得返回可用
Proposal bytes。

#### A/B/C/D 共享 Runner Execution Receipt

B 的部署侧 `RunnerExecutionReceiptStore` 把成功原始输出和 `RunnerExecutionRecord` 分别按内容
寻址发布，再生成不可变 `RunnerExecutionReceipt` / `RunnerExecutionReceiptRef`。失败执行只发布
Receipt，不发布 Proposal bytes。Receipt 同时绑定 Attempt/Run/Request/Plan/generator、Generator
Artifact Hash、Runner provenance、退出状态、实际 usage、raw output URI/Hash/bytes 和 cleanup；
任何一项漂移都会改变 Receipt Hash。

A 的 settle 必须先通过 Ref 从部署侧 Store 重读 Receipt，再校验冻结 Plan 中的
`generator_artifact_hash`、Request Hash、预算、cleanup 和 C Batch 所引用的同一 raw output。校验
成功后，A 才能在一个事务中写入 Batch Ref、Proposal Ref、Receipt identity/URI/Hash、Runner
provenance 和 Budget Ledger；失败或超预算时不写入可审 Proposal。C 的 Batch 保留自己的 Generator
provenance，不能复制或替代 Runner provenance。

终态 Attempt 的 Receipt Ref 采用一次性绑定：Repository 必须复算 Receipt 内容 Hash，并逐项校验
Ref 的 Receipt/Attempt/Run/Request 身份后才允许首次结算；幂等重放还必须与已存 Receipt ID、URI、
Hash、Schema、Runner provenance 以及 Batch ID/URI/Hash/raw output/provenance 完全一致。PostgreSQL
只允许 `running → succeeded/failed` 时首次绑定；一旦 Attempt 终态，Receipt、Batch、实际 usage、
raw output、错误和时间等结算证据均不可改写。租约过期产生的无 Receipt `failed` Attempt 继续合法，
但禁止事后补绑 Receipt。

D 不接受调用方另造 Attempt Evidence。D 从 A 的 `GenerationRunStatusView + budget_ledger` 开始，
独立重读 B Receipt、C Batch/Patch，复算 Receipt/Batch/Proposal Hash、实际 usage、barrier、A 的
retained/duplicate 关系及 D 自己的淘汰原因。三层任一字段不一致均 fail closed。

Deterministic Runner 仅用于 CI，固定 synthetic。Local-command Runner 仅允许部署在看不到 HCU
设备和受保护 Holdout 的专用 Worker，并只执行明确 allowlist 的 executable；它不是任意代码的
通用安全沙箱。C 在该接口之后解析 Proposal，仍必须按本 ADR 的 Hash、审核和晋级规则验证，
不能信任 Runner 自报的 Candidate 或性能结论。

### 5. 身份和重放全部内容化

Knowledge Snapshot、Generation Request、Apex Plan、Runner Receipt 和 Proposal 都有 canonical JSON Hash。
集合类字段在 Hash 前按稳定身份排序；改变 Target/Baseline/Hotspot、知识、Patch、意图、Adapter
或预算都会改变身份。重试使用相同 Request/Plan，不允许悄悄扩大输入或预算。

`normalized_patch_v1` 的唯一公共实现位于 `hcuopt.agent.patch_identity`，规则固定为：

1. 输入按 strict UTF-8 解码，拒绝 NUL；
2. CRLF 和 CR 统一为 LF；
3. 每行删除尾部空格与 Tab；
4. 删除 Git `index ` 元数据行；
5. `--- ` 与 `+++ ` 文件头删除首个 Tab 及其后的时间戳；
6. 删除末尾空行并写入唯一尾部 LF。

C/D 都必须重读原始 Patch，先复算原始字节 SHA256，再按上述公共函数复算规范化 SHA256；任一
声明值不一致即 fail closed。路径或代码内容改变必须产生不同的规范化 Hash。

### 6. dev-only 完成不等于真实自动优化启用

本 ADR 允许 Contract、迁移、deterministic CI Adapter、受限 local-command Adapter、Scripted
调度、CLI 和只读 UI。真实 Agent Proposal 晋级为业务 Candidate、真实 M2a Formal Start 或生产
Web 写入口必须继续满足 #102、#110、A/B/C/D 接受和项目所有者资源窗口授权。

### 7. Generation Authority 独立持久化并按轮次收敛

A 使用独立的 `GenerationRun`、`GeneratorAttempt`、`GenerationBudgetLedgerEntry` 和
`CandidateProposalRef`。Run 只允许
`created → running → awaiting_review → completed/failed/cancelled`；Attempt 只允许
`pending → running → succeeded/failed/cancelled`。PostgreSQL 原子领取使用短事务和 claim token，
过期 Attempt 按其完整 reservation 保守结算，再按冻结 Plan 决定是否创建下一次有限重试。
其中墙钟、输出字节和 token 按上限结算；`proposals` 只统计真正进入 Proposal Ref 的输出，失败或
丢弃 Attempt 固定记 0，不能凭 reservation 虚构已接受 Proposal。

Proposal 到达时先标记为 `pending`，不能用并发完成顺序决定保留者。全部 generator 收敛后，A
按 `normalized_patch_hash → generator ordinal → proposal ordinal → proposal hash/id` 排序，一次性
形成 retained/duplicate 结果。D 仍需独立重算，A 的去重结果不是性能或正确性 verdict。

生成预算账本与 Round Budget 使用不同表、不同 Contract 和不同 idempotency key。数据库约束
永久禁止 HCU、Measurement、Formal Intake 和自动发布字段被打开。

`GeneratorAttempt.actual` 不能由 A 或 C 自报：成功执行按 B Receipt 的墙钟、输出字节和 token
结算；失败且没有可接受 Batch 时按 reservation 保守结算。`GenerationBudgetLedgerEntry` 与 Run
汇总必须与 Attempt 一致，D 还会再次用 Receipt 复算成功执行的实际用量。

## A/B/C/D 分工

| DRI | 权责 | Issue |
| --- | --- | --- |
| A | Authority、Plan、状态机、Apex 调度、幂等与恢复 | #113 |
| B | 无 HCU Runner、沙箱、超时、清理和生成预算执行 | #114 |
| C | Knowledge Snapshot、Generator Adapter、Proposal 与 Package promotion | #115 |
| D | 独立 verifier、去重证据、Scripted 验收和只读 UI | #116 |

## 接受条件

- [ ] A 接受状态、幂等、预算和恢复语义；
- [ ] B 接受 Runner 沙箱、超时和 failure-closed cleanup；
- [ ] C 接受 Knowledge、Proposal、Patch 和 Package promotion 边界；
- [ ] D 接受独立验证、去重、Evidence 和 UI 结论边界；
- [x] 项目所有者批准本周建设无 HCU Agent/Apex MVP；
- [ ] 项目所有者在 #102 中另行批准真实 Formal 激活。

## 非目标

- 不复刻或声称复刻闭源 Asari Apex；本系统只实现等价职责的内部调度层；
- 不授权 Agent 访问 HCU、Holdout 明文、生产凭据、签核密钥或发布接口；
- 不在 Agent 内实现第二套 Build、Harness、Barrier、FWER、Evidence 或 Signoff；
- 不把 Proposal 数量、Agent 自评或生成 token 当作性能收益。

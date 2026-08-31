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
Hash、触碰路径和生成来源，并固定：

- `review_required=true`；
- `formal_intake_allowed=false`；
- `performance_conclusion=not_measured`；
- `automatic_release_allowed=false`。

Proposal 不是 SourceSnapshot、Candidate、Artifact 或性能结论。C 必须从批准的 Proposal 重新
读取 Patch、验证 Baseline/Hotspot/replacement point、重建源码并发布内容寻址业务 Package。
只有这个 Package 才能进入 #110 的 source family verifier 和后续 M2a Intake。

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

### 5. 身份和重放全部内容化

Knowledge Snapshot、Generation Request、Apex Plan 和 Proposal 都有 canonical JSON Hash。
集合类字段在 Hash 前按稳定身份排序；改变 Target/Baseline/Hotspot、知识、Patch、意图、Adapter
或预算都会改变身份。重试使用相同 Request/Plan，不允许悄悄扩大输入或预算。

### 6. dev-only 完成不等于真实自动优化启用

本 ADR 允许 Contract、迁移、deterministic CI Adapter、受限 local-command Adapter、Scripted
调度、CLI 和只读 UI。真实 Agent Proposal 晋级为业务 Candidate、真实 M2a Formal Start 或生产
Web 写入口必须继续满足 #102、#110、A/B/C/D 接受和项目所有者资源窗口授权。

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

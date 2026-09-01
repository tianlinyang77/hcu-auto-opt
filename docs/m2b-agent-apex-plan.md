# M2b Agent/Apex 本周建设计划

## 本周交付口径

本周目标是让系统能够从冻结 Hotspot 和证据出发，调用一个或多个可插拔 Agent，经过 Apex-like
调度、有限重试、预算和去重，产出可解释、可人工复核的 Candidate Proposal。最终结果可通过
CLI 和只读 UI 查看，并可在 dev-only 环境中完成 Proposal review、既有 business Source Package
发布和 source-family 验证闭环。

这不表示真实 HCU 自动优化已开启。真实 Proposal 变成业务 Candidate 后，仍必须走 C 的源码
Package、B 的唯一 Harness、D 的独立验证、Round Barrier/FWER 和人工 Signoff。

## 四条并行工作线

| 工作线 | 第一交付 | 第二交付 | 完成信号 |
| --- | --- | --- | --- |
| A / #113 | Contract、ADR、Plan Hash | durable scheduler/reconcile | 崩溃与重放收敛到同一 Run |
| B / #114 | Runner 输入输出与沙箱协议 | local-command Adapter、预算和清理 | Windows/Linux 失败路径全覆盖 |
| C / #115 | Knowledge Snapshot、Proposal | 人工 review 与 Package promotion | 未批准 Proposal 永远进不了 Store |
| D / #116 | verifier 与去重规则 | EvidenceBundle、Scripted E2E、只读 UI | 每个保留/淘汰原因可复算 |

## 合并顺序

1. A 主责、C 联审 `agent_v1` Contract 和 ADR-0011；
2. B/C 在稳定 Contract 上并行实现 Adapter 与 promotion；
3. A 接入 scheduler、状态和 PostgreSQL 恢复；
4. D 接入独立 verifier、Evidence、Scripted acceptance 和 UI；
5. A/B/C/D 在 #112 分别签收 dev-only MVP；
6. 真实 Formal 激活回到 #102/#110，不在本周 MVP PR 中偷开开关。

## A 线当前实现切片

A 线的 durable authority 使用四类 PostgreSQL 记录：

- `agent_generation_runs`：冻结 Request、Plan、Hash、状态、计数和当前预算；
- `agent_generator_attempts`：记录原子领取、claim token、短租约、Batch/Provenance 和安全错误；
- `agent_generation_budget_ledger`：append-only reserve/settle/release，独立于 Round Budget；
- `agent_candidate_proposal_refs`：只保存 Proposal/Patch 引用和 barrier 后的去重处置。

正常操作入口是：

```text
hcuopt db-migrate
hcuopt agent-generation-start <start-request.json>
hcuopt agent-generation-status <generation-run-id>
hcuopt agent-generation-reconcile <generation-run-id>
```

这三个 Agent Generation 命令直接访问 PostgreSQL，Web 写入口仍关闭。B 后续消费原子 claim、
renew 和 settle 方法；C 消费 awaiting_review 的 retained Proposal；D 独立重读 Attempt、预算账本
和 Proposal Ref。A 不从 CLI 启动 HCU Worker，也不创建 RoundCandidate。

## 固定边界

```text
Agent / Apex
  可以：读冻结输入、生成 Patch Proposal、有限重试、去重、记录生成预算
  不可以：写 Round Candidate、占 HCU、测性能、关 Barrier、做 FWER、签核、发布

C Promotion
  可以：复算 Request/Patch、人工批准、构建既有 Source Package、进入既有 source family verifier
  不可以：复制 fixture、跳过 provenance、把 Agent 自评当性能结论

现有 M2a
  继续拥有：Build、Correctness、Search/Holdout、Barrier、FWER、Evidence、Signoff
```

## C 线受控晋级实现

C 不把 Proposal 直接改名为 Candidate，也不创建第二套 Package 或 Family 格式。实现链固定为：

```text
Knowledge Snapshot 内容寻址保存与独立重读
  → deterministic CandidateProposalBatch（CI synthetic）
  → A barrier 产生 retained Proposal
  → C 重读 Request / Batch / Patch / Baseline SourceSnapshot authority
  → 不可变人工 Review
  → 现有 SourceManager 创建隔离 Candidate Worktree并应用 approved Patch
  → 以完整 Candidate Worktree Hash 发布既有 CandidateSourcePackageRef并清理 Worktree
  → 2–4 member BusinessCandidateFamilyVerifier
  → 不可变 Promotion Receipt
```

实现分别位于 `agent_knowledge.py`、`agent_generator.py` 和 `agent_promotion.py`。Promotion 只接受
`awaiting_review` Run 中唯一 retained Proposal；pending/duplicate、rejected、fake/synthetic、
Request/Batch/Patch Hash 漂移、Baseline 漂移和越界 touched path 均拒绝。Family Verifier 继续
重读部署方 Source Package Store，拒绝 fixture、重复 Package 和 Store/Family Authority 漂移。
`candidate_source_hash` 固定表示应用 Overlay 后的完整 Candidate Worktree Hash，不得退化为
Overlay-only Hash。Review/Promotion 使用内容绑定 ID 和幂等索引，Store 重读时复算 ID 与引用
Evidence；相同幂等键不能绑定不同内容，发布后的决定或回执篡改必须 fail closed。

整条 C 链仍为 dev-only，固定 `performance_conclusion=not_measured`、
`formal_intake_allowed=false`、`automatic_release_allowed=false`，不会运行 Build/HCU，也不会进入
Measurement、Holdout、Barrier、FWER、Signoff 或 Release。

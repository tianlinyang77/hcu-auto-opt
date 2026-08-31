# M2b Agent/Apex 本周建设计划

## 本周交付口径

本周目标是让系统能够从冻结 Hotspot 和证据出发，调用一个或多个可插拔 Agent，经过 Apex-like
调度、有限重试、预算和去重，产出可解释、可人工复核的 Candidate Proposal。最终结果可通过
CLI 和只读 UI 查看，并可在 Scripted 环境中完成 Proposal review 到 fixture Intake 的闭环。

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

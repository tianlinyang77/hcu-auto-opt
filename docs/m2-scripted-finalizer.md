# M2 Scripted Round 持久化与最终化

M2-5/A 把 D 线已经冻结的 Search Barrier、Holdout Reveal、Holdout Barrier、Bonferroni FWER
和 Round Evidence 接回 PostgreSQL 控制面。该链只运行 synthetic Fixture，不使用 HCU，也不产生
性能结论。

## 权威边界

- D 负责选择规则、Barrier 内容、Holdout、FWER、Evidence Index 和递归重哈希。
- A 负责行锁、幂等、状态推进、对象持久化和审计事件，不重新解释置信区间或 verdict。
- Worker 不能直接写 Barrier、FWER、Evidence Bundle 或 `scripted_completed`。
- Scripted Bundle 固定 `synthetic=true`、`performance_conclusion=not_measured` 和
  `automatic_release_allowed=false`，不能进入 Formal Signoff。

## 状态与写入顺序

```text
correctness
  → POST barriers/search:close
  → search_barrier
      ├─ 无晋级 → POST scripted:finalize → scripted_completed
      └─ 有晋级
          → POST holdout-plan:reveal
          → holdout_measuring
          → POST barriers/holdout:close
          → holdout_barrier
          → POST multiple-comparison
          → POST scripted:finalize
          → scripted_completed
```

每一步都锁定 `search_rounds` 行。相同 UUID、幂等键和内容 Hash 的重放返回已有对象；相同 Round/
Phase 下不同内容、Candidate/Artifact 漂移或提前调用均拒绝。`round_barriers`、
`round_holdout_reveals`、`multiple_comparison_results` 和 `round_evidence_bundles` 都由数据库触发器
保持 append-only。

`POST /v1/search-rounds/{round_id}/reconcile` 不猜测或补造外部证据。它重新读取持久化 Authority
图，核对数据库状态，并返回唯一安全的 `next_action`，例如 `close_intake`、
`freeze_artifact_family`、`record_holdout_reveal` 或 `finalize_scripted_round`。状态与 Authority 图
不一致时 fail-closed。

`POST /v1/search-rounds/{round_id}/cancel` 只允许取消空闲的非终态 Scripted Round。仍有运行 Job
或 `reserved` Budget 时拒绝，必须先由执行侧完成清理并 `settle/release`；A 不会为了让取消成功而
伪造清理或 Budget Evidence。取消会停止仍在排队的 Job，并把 Round/Task 推进到 `cancelled`。

## Finalizer 检查

最终化事务会重新读取 Search/Holdout Barrier、FWER 和 Budget Ledger，并要求所有 Budget
Reservation 已 `settled` 或 `released`。A 从数据库内容生成 `m2a-round-budget-ledger-v1` 规范文档
并重算 Hash；D Finalizer 再从 Evidence Index 的 URI 重读全部证据、独立复算 SHA256，并重建
完整 `RoundEvidenceBundle`。只有重建结果与提交 Bundle 逐字节一致，Round 才进入
`scripted_completed`。

## 当前边界

默认服务不会自动创建 synthetic Evidence Store；部署或 Scripted Coordinator 必须显式注入
`M2ScriptedRoundFinalizer` 和同一个只读 Evidence Reader。未配置 Finalizer 时最终化 fail-closed。
这一设计避免默认目录静默回退到 Fake Authority，并为后续受保护 Formal Store 保留独立配置边界。

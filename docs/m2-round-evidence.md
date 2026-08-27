# M2 Scripted Round Evidence

M2 D 线把 Search、Holdout 和 FWER 的 synthetic 控制流收敛为一个不可用于正式性能结论的
`RoundEvidenceBundle`。该链只验证批级同步、统计协议、失败保留和证据完整性，不运行 HCU，
不创建 Formal Measurement Ref，也不进入人工签核或发布。

## 两条终止路径

零晋级路径在 Search Barrier 关闭后直接生成 Bundle：

```text
Search Barrier(no_promotable_candidate)
→ Evidence Index recursive verify
→ RoundEvidenceBundle(no_promotable_candidate, synthetic=true)
```

有晋级路径必须完成独立 Holdout：

```text
Search Barrier
→ freeze Holdout Family
→ one-time Holdout reveal
→ Holdout Barrier
→ Bonferroni FWER
→ Evidence Index recursive verify
→ RoundEvidenceBundle(holdout_completed, synthetic=true)
```

零晋级 Bundle 必须完全省略 Holdout Family、Plan reveal、Holdout Barrier 和 FWER。完成 Holdout
的 Bundle 必须同时绑定这些对象，不能缺一个继续。

## Evidence Index

`M2EvidenceIndex` 的每个递归条目保存 role、类型、URI、SHA256、生产者、保留责任和实际可访问性
检查时间。Verifier 会重新读取每个 URI、独立重算 Hash，并要求条目集合与本轮所需证据完全一致。
缺失、额外、重复、不可读或 Hash 漂移都会使 Bundle 构建失败。

索引覆盖：

- Search Plan、Search 输入摘要和 Search Barrier；
- 每个 Candidate 的 Artifact、正确性、Phase Receipt 成员、原始输入、同时期 Baseline、预算、
  清理或失败证据；
- Round Budget Ledger；
- 有晋级时的 Holdout Plan、Reveal、Holdout Barrier 和 Multiple Comparison。

Search 输入摘要保留 synthetic statistics 的原始证据和 Baseline Hash。FWER 的每个
`AdjustedCandidateResult` 同样保留 Holdout 原始证据和 Baseline Hash。因此更换任一
Candidate、Plan、Artifact、Measurement 或 Baseline 身份都无法复用旧 Bundle。

## 安全边界

- Scripted Bundle 固定 `performance_conclusion=not_measured`；
- `evidence_authority=synthetic_fixture_only`；
- `automatic_release_allowed=false`；
- `require_formal_round_signoff()` 对 Scripted 或 synthetic Bundle 返回稳定错误
  `synthetic_round_not_signable`；
- `SyntheticEvidenceStore` 只用于无 HCU 的内容寻址夹具，不替代 Formal POSIX/ACL Evidence Store。

本地验证：

```bash
python -m pytest tests/unit/test_m2_round_evidence.py -q
```

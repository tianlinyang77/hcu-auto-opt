# M1-D 独立正确性与性能裁决

`m1-kernel-correctness-v1` 是 M1 单候选链路的 D-owned 只读协议。它不运行新的计时器，
也不接受 Producer 提交的 `passed`、`speedup` 或其他汇总结论。

当前 PR 提供不依赖 HCU 的核心能力：

- 冻结 Hotspot 的 Shape、Dtype、参考实现 Hash、随机种子、特殊值、重复预算和显式容差；
- 在可信根目录内重新打开并校验 SourceSnapshot、Artifact、进程、缓存、stdout 和规范化输出；
- 区分数值错误 `incorrect` 与证据错误 `invalid`；
- 对已验证的 Baseline/Candidate restart 样本执行确定性 bootstrap，并使用本次 Stage 0 MDE
  输出 `faster`、`slower`、`inconclusive` 或 `invalid`；
- 原子发布 `correctness.json`、`performance.json`、`evidence-bundle.json`、`signoff.md`
  和 `sha256sums.json`。

正确性不是 `correct` 时，性能裁决固定为 `invalid` 且不产生快慢结论。所有报告都固定
`automatic_release_allowed=false`，人工批准也只表示接受证据。

PR-D1 不注册真实 M1 Adapter Profile。#31 和 #32 冻结原始计时证据与 Overlay Artifact 后，
PR-D2 才会接入 `manual_correctness`、`manual_adjudicate` Worker 和 PostgreSQL 闭环。

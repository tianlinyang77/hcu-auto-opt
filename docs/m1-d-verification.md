# M1-D 独立正确性与性能裁决

`m1-kernel-correctness-v1` 是 M1 单候选链路的 D-owned 只读协议。它不运行新的计时器，
也不接受 Producer 提交的 `passed`、`speedup` 或其他汇总结论。

当前实现提供以下核心能力：

- 冻结 Hotspot 的 Shape、Dtype、参考实现 Hash、随机种子、特殊值、重复预算和显式容差；
- 在可信根目录内重新打开并校验 SourceSnapshot、Artifact、进程、缓存、stdout 和规范化输出；
- 区分数值错误 `incorrect` 与证据错误 `invalid`；
- 直接消费 ADR-0006 的唯一 `M1MeasurementEvidence`，不在 D 内复制第二套性能 Schema；
- 从独立控制面上下文核对 Adapter Profile、Baseline Epoch、Target、Workload、Artifact 和
  Performance exclusive Lease；
- 重新打开每个 ABBA acquisition 的 lifecycle、空缓存证明、Overlay import attestation 和
  HCU Event，复算 calibration、单样本 `kernel_elapsed/ns`、完整采样预算及 Stage 0 MDE；
- 对已验证的 ABBA restart effect 执行确定性 bootstrap，并从当前 Workload 的独立
  Baseline restart 均值复算 MDE；裁决门限固定为
  `max(Formal Stage 0 MDE, 当前 Workload Baseline MDE)`，再输出 `faster`、`slower`、
  `inconclusive` 或 `invalid`；
- 原子发布 `correctness.json`、`performance.json`、`evidence-bundle.json`、`signoff.md`
  和 `sha256sums.json`。

这里的 `signoff.md` 是 D 生成的待人工审阅摘要，不是人工决定。人工批准或拒绝的权威记录
来自 Signoff API、PostgreSQL 审计行和 `manual_candidate_signoff_recorded` 事件。M2a 提案
将另外生成内容寻址的 `signoff-decision.json`，但不得回写或重命名已签核的 M1 文件。

正确性不是 `correct` 时，性能裁决固定为 `invalid` 且不产生快慢结论。所有报告都固定
`automatic_release_allowed=false`，人工批准也只表示接受证据。

本模块已经提供 `kernel_correctness` 与 `candidate_adjudicator` Worker Adapter。前者消费
部署方注入的正确性 Evidence Producer，发布可重读 Verification Artifact；后者从控制面
取得 shared/exclusive 两次租约权威信息，重新读取正确性、Verification Artifact 和 B 的
MeasurementSeries 后才写 verdict。`invalid` 同样生成完整 EvaluationRun 和 EvidenceBundle。

默认 Catalog 仍不注册真实 M1 Profile。首个业务 Hotspot 已通过部署方显式组合的
Correctness Evidence Producer 和 C→B `M1WorkloadFactory` 完成 nmz36 Target Lock Formal
闭环及人工签核；Scripted 夹具仍不能作为真实候选结论。签核操作边界见
[M1 人工签核 Runbook](m1-signoff-runbook.md)。

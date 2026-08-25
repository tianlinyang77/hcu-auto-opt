# M1-D 独立正确性与性能裁决

`m1-kernel-correctness-v1` 是 M1 单候选链路的 D-owned 只读协议。它不运行新的计时器，
也不接受 Producer 提交的 `passed`、`speedup` 或其他汇总结论。

当前 PR 提供不依赖 HCU 的核心能力：

- 冻结 Hotspot 的 Shape、Dtype、参考实现 Hash、随机种子、特殊值、重复预算和显式容差；
- 在可信根目录内重新打开并校验 SourceSnapshot、Artifact、进程、缓存、stdout 和规范化输出；
- 区分数值错误 `incorrect` 与证据错误 `invalid`；
- 直接消费 ADR-0006 的唯一 `M1MeasurementEvidence`，不在 D 内复制第二套性能 Schema；
- 从独立控制面上下文核对 Adapter Profile、Baseline Epoch、Target、Workload、Artifact 和
  Performance exclusive Lease；
- 重新打开每个 ABBA acquisition 的 lifecycle、空缓存证明、Overlay import attestation 和
  HCU Event，复算 calibration、单样本 `kernel_elapsed/ns`、完整采样预算及 Stage 0 MDE；
- 对已验证的 ABBA restart effect 执行确定性 bootstrap，输出 `faster`、`slower`、
  `inconclusive` 或 `invalid`；
- 原子发布 `correctness.json`、`performance.json`、`evidence-bundle.json`、`signoff.md`
  和 `sha256sums.json`。

正确性不是 `correct` 时，性能裁决固定为 `invalid` 且不产生快慢结论。所有报告都固定
`automatic_release_allowed=false`，人工批准也只表示接受证据。

本模块仍不注册真实 M1 Adapter Profile。B 只生产不可变的原始 MeasurementSeries，D 只读
复算并写 verdict；C 的真实 startup Overlay 和 D 的 Worker Adapter 接入完成后，才运行
nmz36 Target Lock 闭环。

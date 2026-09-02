# ADR-0013：Formal Plan 由独立编译器重读并冻结

- 状态：Proposed / A2a Contract implemented, no Formal Start enabled
- 日期：2026-09-02
- 跟踪：GitHub Issue #125、总体 Issue #102
- 依赖：ADR-0009、ADR-0010、ADR-0012

## 背景

现有 `OperatorPlanCompiler` 服务于 synthetic Scripted 演示：客户端提交 Hotspot 和 Candidate
列表，编译器通过 Scripted Store 校验后生成 Preview。直接给它增加 `formal` 分支会让两类信任
边界共享一个入口，也会让客户端继续决定真实 Round 的成员，不能满足 Formal 路径对 Target、
Stage 0、Baseline、Workload、Hotspot 和 business Candidate Family 的重读要求。

A1 已建立内容寻址的 Formal Profile 窗口授权，但 Profile 被授权只说明可以解析这组输入，并不
说明客户端提交的运行内容可信，更不等于可以创建 Round 或访问 HCU。

## 决策

### 1. Scripted 与 Formal 使用不同编译器和 Contract

新增 `FormalRoundPlanPreviewRequest`、`FormalResolvedRoundPlan`、
`FormalRoundPlanPreviewView`、`FormalOperatorAuthorityRepository` 和
`FormalOperatorPlanCompiler`。现有 Scripted Contract、Compiler、StartIntent 和数据库写入口
保持不变。

Formal 请求不接受客户端自行挑选的 Candidate 列表，也不携带 Family Manifest 或 Package
内容。它只携带精确 Profile 引用、最多晋级数、幂等键、期望服务身份和期望窗口授权 Hash。

### 2. 候选成员与业务 Authority 必须重读

编译器先把签名窗口中的 `source_family_hash` 交给部署侧
`FormalCandidateFamilyManifestStore`，取回 C 冻结的 Manifest；再通过部署侧
`BusinessCandidateFamilyVerifier` 重读每个内容寻址 Package、重新计算 Family Hash，并与签名
值比较。成员按 `candidate_id` 规范排序，由系统分配连续 ordinal；客户端不能提交或改变 Family
内容与成员映射。

`PostgresRepository.resolve_formal_operator_authority()` 按 Family 中的精确 ID 重读 Target、
finalized Formal Stage 0、冻结的 M1 Baseline/source、Workload 和 business Hotspot，并拒绝
synthetic/fake 证据或任一字段漂移。编译器还会再次比较 Repository 返回值、Profile 与 Family，
避免错误 Repository 实现绕过边界。

### 3. Plan Hash 绑定窗口和全部已冻结输入

Formal Plan Hash 覆盖：

- 三个 Profile Ref 和 A1 窗口授权 Hash；
- 授权 host、resource、起止时间；
- 完整 Family Manifest、重算的 Family Hash 和规范成员映射；
- Repository Authority 与完整 Hotspot Ref；
- Search/Holdout 协议 Hash、selection rule、预算和最多晋级数；
- `formal`、`degraded_manual_intake`、`synthetic=false`、
  `automatic_release_allowed=false`。

Preview 的有效期取本地 TTL 与授权窗口剩余时间的较小值。未来 StartIntent 消费前必须调用
`revalidate()`，重新读取 Catalog、Store 和 Repository；旧 Preview 不能把过期或漂移的 Authority
继续带入启动阶段。

### 4. Preflight 通过不授予执行权

A2a 固定加入 `formal_start_authority_not_bound` 阻塞项，因此即使 Formal Plan 已完整解析，Preview
仍保持 `start_allowed=false`。它不创建 Task/SearchRound，不分配 lease，不访问 HCU，不生成
Measurement/Holdout/FWER/Evidence，不签核，也不发布。

B 的 Formal Adapter、lease/fencing/cleanup receipt 与 D 的 sealed Holdout Authority/受保护证据根
尚未冻结，不能由 A2a 自行发明。它们必须在后续 A3 StartIntent 中作为独立受信输入接入；只有
全部验证后 A3 才可移除上述阻塞项。A4 再补受控 API/CLI、PostgreSQL 并发和崩溃恢复验证。

## 影响

- Real Profile 即使已经注册，也不能绕过 Family、预算、Store 和 Repository 重读；
- Formal 与 Scripted 路径保持物理隔离，Scripted 行为及 Hash 不变；
- A3 获得可重验证的完整 Family Manifest，而不是不可恢复的客户端 Candidate 列表；
- readiness 的 `formal_plan_compiler` 进入 hold，但在 D/A3/A4 完成前仍阻止窗口申请和真实 Round。

## 当前非授权声明

本 ADR 未签发 nmz36/HCU 7 窗口，未注册生产 Real Profile，未创建 Formal Round，未访问 HCU，
未产生性能结论，且继续禁止自动发布。

# M2 搜索轮次建设计划

## 当前授权边界

当前只授权 M2 设计和无 HCU 工程准备。ADR-0009 状态为 Proposed；在四人评审和项目所有者
明确批准 M2a 前，不创建真实多 Candidate Formal Task，不运行 Agent，不产生新的 HCU 性能
结论。字段、表、API 和稳定错误码见 [M2a SearchRound Contract 草案](m2-contract-draft.md)；
草案合入不等于 Contract Accepted。面向操作者的 Profile、Plan、CLI、通知、报告和 Web 分阶段
要求见 [M2 操作面与易用性建设计划](m2-operability-plan.md)。
OX-0 字段、幂等、Preflight、Read Model 和 CLI/API 映射见
[M2 Operator Contract 草案](m2-operator-contract-draft.md)。

## 建设目标

把 M1 的“一个 Candidate 的可信比较”提升为“同一轮 2–4 个 Candidate 的可信选择”，同时
保持以下不变量：

```text
固定 Target / Baseline / Workload
→ 人工 Candidate Family
→ 独立 Build + Correctness
→ Search Measurement
→ Search Barrier
→ 有晋级时：独立 Holdout Measurement → Holdout Barrier + Bonferroni FWER
→ 零晋级时：跳过 Holdout/FWER
→ Round EvidenceBundle
→ Formal：Human Signoff；Scripted：synthetic 验证终态
```

M2a 只回答 Kernel 级候选在冻结 Search/Holdout 下能否可信晋级，不回答模型或服务 E2E 收益。

## 交付切片

### M2-0：设计与 Contract 冻结

Owner：A；B/C/D 必须共同 Review。

交付：

- ADR-0009 从 Proposed 评审为 Accepted 或明确拒绝；
- `SearchRound`、`RoundCandidate`、`RoundMeasurementRef`、`RoundBarrierResult`、
  `MultipleComparisonResult`、`RoundEvidenceBundle`、`RoundSignoffIntent/Signoff` 的字段草案；
- Candidate、Artifact 和条件性 Holdout Family Hash 的冻结输入、时间点与规范化算法；
- nonce-sealed Holdout commitment/reveal 和零晋级终态；
- 状态机、幂等键、错误码、Budget reservation/ledger、Signoff outbox 和 Contract 版本策略；
- Profile ID/Version、Plan Preview、Round Summary 和 Operator API 的边界；
- M1 v1 证据回放兼容测试清单。

退出条件：四人签字、项目所有者只批准进入 M2a 代码实现；尚不批准 HCU Formal。

### M2-1：A 线轮次控制面

Owner：A。

交付：

- 增量 PostgreSQL 迁移，不修改已签核 M1 表语义；
- Round 创建、Candidate Intake、Intake Close、状态查询和取消 API；
- 原子 Barrier Close、零晋级收敛、Budget reservation/ledger、Signoff Intent/Outbox 和 Reconcile；
- `SELECT FOR UPDATE`/幂等/并发 PostgreSQL 集成测试；
- API 返回 source commit、Contract version 和 Adapter Profile 供写前核对。
- 提供可重建的 Round/Candidate/Resource/Evidence Read Model，供 CLI/Web 读取而不复制状态机。

退出条件：两个 Worker 同时关闭 Barrier 只能产生一个逻辑对象；关闭后新增 Candidate、迟到
Worker、旧 Claim/Fencing Token 和超预算 Job 均被拒绝；Signoff 任一崩溃点可恢复，且未复核
Artifact 时 Round 不进入终态。

### M2-2：C 线有限候选与制品

Owner：C。

交付：

- 同一 Hotspot 下 2–4 个人工 Candidate Source Package；
- 每个 Candidate 独立 Worktree、SourceSnapshot、Artifact、Build Cache Key 和清理证据；
- Candidate Family Hash、Artifact Family Hash 和 Intake Close 后不可修改证明；
- Search 后禁止重建/换 Artifact 的 Contract 测试；
- Scripted fixture：no-op、已知快、已知慢和构建失败。

退出条件：同一源码幂等重放返回同一 Artifact；不同字节不能复用 Hash；所有 Worktree 在成功
和异常路径均清理，Baseline 保持干净。M2a 不实现 Agent Generator。

### M2-3：B 线 Phase-aware 测量执行

Owner：B。

交付：

- 在不修改 `m1-kernel-performance-evidence-v1` 的前提下执行 Search/Holdout 两类计划；
- 每个 Candidate/Phase 独立 ABBA、进程、缓存、Measurement ID 和同时期 Baseline；
- 实际样本、墙钟、Harness 时间和 Lease 持有秒数通过 reserve/settle/release 回写 Budget；
- 外部 Runner 门禁、Telemetry 有限重试、Fence/Health 和完整失败证据；
- 拒绝 Search/Holdout URI、Hash、Plan 或 Baseline 身份复用的测试。

退出条件：B 的输出仍无 Producer verdict；任何部分采样、清理失败或预算不足都不能得到
`measured`。

### M2-4：D 线选择、Holdout 与 FWER

Owner：D。

交付：

- Search Plan、带 nonce 的 Holdout commitment/reveal 和访问边界；
- Search Barrier 完整性校验、零晋级路径和最多提升 2 个 Candidate 的预注册规则；
- 重读 B 原始证据，执行单次比较验证和当前 Workload MDE 复算；
- Holdout Barrier 与 Bonferroni FWER；
- 对称的 faster/slower/inconclusive/invalid、`m=0` 拒绝、正确性失败、候选缺失、Family
  Hash 篡改和 Barrier 提前关闭的确定性测试；
- 保存所有候选的 `RoundEvidenceBundle`，不只保存赢家。

退出条件：相同证据重放得到完全相同结果；更换任意 Candidate、Plan、Baseline、Measurement
或 Artifact Hash 均变为 `invalid`，不能降级成无告警的结果。

### M2-5：集成与 Target Lock

Owner：A 负责整合；B/C/D 对自己的证据签字。

按以下顺序推进：

1. 无 HCU Unit/Contract 测试；
2. PostgreSQL 并发、幂等、stale Worker、Budget 和 Barrier 测试；
3. Scripted 2–4 Candidate 全链，覆盖成功、零晋级和失败家族，进入 `scripted_completed` 并证明
   Synthetic 不可签核；
4. 评审 Evidence Index、Runbook 和 Target Lock；
5. 项目所有者明确批准一次 M2a HCU 窗口；
6. nmz36 Formal Round；
7. 四人独立验收和人工 Signoff。

未到第 5 步不占用 HCU 产生 M2 结论。

### M2-OX：操作面与易用性横向轨道

M2-OX 不是独立 Workflow，也不拥有第二套数据库状态。它跨越 M2-0 至 M2-5，把用户输入编译
为已批准的 SearchRound Contract，并把权威事件和 Evidence 转为 CLI/Web 可读结果。

交付顺序：

1. M2-0 冻结 Profile、Plan Preview、Summary、稳定错误与操作成本指标；
2. M2-1 至 M2-4 先完成 Scripted `plan/start/status/report` CLI 和 Read Model；
3. Scripted 稳定后实现只读 Web、通知和失败引导；
4. Signoff Intent/Outbox 与鉴权通过后，才开放受控创建、Cancel 和 Formal Signoff 页面。

退出条件：注册 Profile 的 Scripted Round 无需 SSH、手工 Docker、数据库写入或复制内部
UUID/Hash 即可从 Plan 运行到 Report；Formal 路径继续 fail-closed，且操作者主动操作时间目标
不超过 15 分钟。详细 Contract、分工和指标见操作面计划。

## 四人立即分工

| 人员 | 第一任务 | 依赖 | 不能改 |
| --- | --- | --- | --- |
| A | Round Contract、状态机、迁移草案、Budget/Barrier/Signoff API | ADR-0009 评审 | B 的测量语义、D 的统计算法 |
| B | Phase-aware 执行适配、同时期 Baseline、消耗回流和清理证据设计 | D 的 Plan Contract | verdict、FWER、晋级决定 |
| C | 2–4 人工 Candidate Intake、Family Hash、Worktree/Build Cache/Artifact 设计 | A 的 Round ID 与 Intake Close | HCU 测量、Holdout 内容、签核 |
| D | Search/Holdout Plan、提升规则、Bonferroni FWER 和 Round Evidence 设计 | B 的原始证据 Contract | 第二套计时 Harness、Candidate 源码 |

接口冲突按 ADR + 接口两侧 Owner + A 的流程解冻。签核后的 M1 v1 Contract 不在本阶段解冻。

## 首批测试矩阵

| 层级 | 必测场景 |
| --- | --- |
| Contract | extra field、跨 Round/Phase/Family/Artifact/Plan 绑定、M1 v1 回放 |
| State machine | 第二次 Intake Close、提前 Barrier、零晋级、迟到 Worker、取消、预算耗尽 |
| PostgreSQL | Barrier 唯一、Budget 事件互斥、Signoff Outbox 崩溃恢复、幂等、迁移升级 |
| Scripted | known faster/slower/inconclusive/invalid、零晋级、正确性/Build 失败、独立 synthetic 终态、Formal Signoff 拒绝 |
| Evidence | Holdout commitment 枚举/篡改、Search/Holdout 复用、Baseline 复用、外部引用丢失 |
| Resource | exclusive 冲突、过期 Lease、Fencing、清理失败、外部 Runner 出现 |
| Operability | Plan 无 HCU Preflight、幂等启动、状态/错误可解释、Scripted/Formal 隔离、Cancel 清理、CLI 跨平台 |
| Formal | 固定 Target/Workload、2–4 业务 Candidate、完整家族证据和人工接受 |

## M2a Formal Go/No-Go

A 主持评审，B 对测量可信度拥有停止权，D 对统计/证据无效拥有停止权，C 对源码/Artifact
不可复现拥有停止权。以下任一项未满足都保持 HOLD：

- ADR-0009 Accepted；
- Contract、迁移和四人 CODEOWNER Review 完成；
- Unit、PostgreSQL、Linux/Windows Scripted CI 全绿；
- Holdout 计划在 Search 前以 nonce commitment 冻结，reveal 与访问边界可证明；
- 零晋级、Synthetic Evidence、Signoff 崩溃恢复和 Budget 事件互斥测试通过；
- 轮次预算适配 HCU 时间窗口，且不会临时缩减正式采样计划；
- Target Snapshot、Stage0Run、Baseline、Image 和 Real Adapter Profile 重新核对；
- Formal Plan Preview、Read Model、Report 和资源清理状态可由 Operator API 复核；
- HCU 资源窗口获得明确授权；
- 项目所有者签署 M2a Formal Go。

## M2b 暂停条件

在 M2a 完整 Target Lock 运行和人工签核前，不创建 Agent/Apex 实现 Issue。即使进入 M2b，
Generator 也只提交候选源码包和 provenance；Candidate Intake、Build、HCU 测量、Barrier、
FWER、EvidenceBundle 和 Signoff 继续由现有权威组件控制。

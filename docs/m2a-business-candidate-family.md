# M2a 业务 Candidate Family 冻结合同

## 目的

M2a Formal 需要 2～4 个属于同一业务热点的 startup Overlay Candidate。C 在创建 Formal
Round 之前先发布并复核这些源码包，形成一个不可变的 **source family**。这个步骤只证明
“准备评测哪些真实业务源码候选”，不证明其中任何一个更快，也不访问 HCU。

Scripted 的 `noop`、`known_faster`、`known_slower` 和 `build_failure` 仍只用于控制流测试；它们
是 `candidate_kind=fixture`，不能计入业务 Family。

## 两层 Hash 不应混用

| 身份 | 产生时间 | 内容 | 用途 |
| --- | --- | --- | --- |
| `source_family_hash` | C 完成包复核后、Round 创建前 | Store Authority、Target/Stage 0/Baseline/Hotspot、Profiler、Overlay 路径和 2～4 个业务 Package/Manifest/Source Hash 与优化意图 | 项目所有者和 A/B/C/D 评审“准备提交什么” |
| `candidate_family_hash` | A 创建 Round 并完成 Intake Close 后 | ADR-0009 定义的 Round/Candidate 身份、ordinal 和已验证 Package 绑定 | Round 内 Barrier、Measurement 和 Evidence 权威 |

`source_family_hash` 不伪造尚不存在的 `round_id` 或 `round_candidate_id`。后续 Formal Plan
Compiler 必须从已验证 source family 确定性产生 Round Intake，并在 Close 后保存新的
`candidate_family_hash`；两者都要进入 StartIntent 和最终 Evidence，不能用一个覆盖另一个。

## 已实现合同

- `BusinessCandidateFamilyManifest` 固定 Store、Target Snapshot、Formal Stage0Run、Baseline
  Epoch/Source、Hotspot、replacement point、Profiler Evidence 和唯一 Overlay 文件。
- 成员数固定为 2～4；每个成员绑定 Candidate UUID、`CandidateSourcePackageRef` 和单一优化
  假设。
- Family 固定 `track=triton`、`release_mode=overlay`、`candidate_kind=business`、
  `synthetic=false`、`automatic_release_allowed=false`。
- Candidate ID、Candidate Source Hash、Package Hash 和 Manifest Hash 任一重复都会被拒绝。
- `source_family_hash` 对 Manifest 成员的书写顺序无关，但对任一权威字段、包身份或优化意图
  的变化敏感。

## 独立复核

`BusinessCandidateFamilyVerifier` 不信任 Manifest 自报的摘要。它从部署侧
`CandidateSourcePackageStore` 重新读取每个包，并逐项验证：

1. Store ID/Hash 与部署 Authority 一致；
2. 内容寻址目录、Manifest 和 Overlay 文件都是普通文件且 Hash 匹配；
3. Candidate/Hotspot/Baseline/Source/Profiler/replacement point 与 Family 一致；
4. Package 外层 Hash 由原始 Manifest Hash 和文件清单重新计算；
5. 每个成员必须是 `business`，且所有成员替换同一个批准的 Overlay 文件和挂载目标。

任何文件篡改、fixture 混入、Store/Package/Manifest/Source Hash 漂移或 Authority 不一致都会
抛出 `SourceArtifactError`，不得把 Family 标为 frozen。

## C 线交付顺序

1. 复用历史 M1 Candidate 的可信发布模式，但从当前部署侧 Store 重新发布或导出可重读包；
2. 为同一 allocator 热点准备第二个真实业务 Overlay 假设，并完成源码、许可证和人工复核；
3. 发布两个不同 Candidate UUID/Source/Package/Manifest Hash 的内容寻址包；
4. 生成 `BusinessCandidateFamilyManifest`，由另一个身份运行 Verifier；
5. 保存 canonical Manifest、`source_family_hash`、复核人、时间和 Store Authority 证据；
6. 更新 Formal readiness 和 C 的 `accepted_for_formal_window` 证据。

第二个 Candidate 可以是尚未测量的真实业务实现；它不需要预先声称性能提升。但它必须有明确
源码改动、优化意图、真实业务 replacement point 和完整 provenance，不能为了凑足两个成员
复制 M1 包、改 UUID、改注释或包装 scripted fixture。

## 当前状态

截至 2026-08-31，仓库只有一个历史 M1 业务 Candidate 的正式证据。合同、确定性
`source_family_hash` 和 fail-closed Verifier 已实现并由单元测试覆盖，但第二个业务包与正式
Family Manifest 尚不存在。因此 `business_candidate_family` 继续为 `block`，Formal Profile、
Formal Round、HCU 窗口和 Agent/Apex 均不得开启。

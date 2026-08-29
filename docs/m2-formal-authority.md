# M2a Formal Authority 持久化地基

## 结论

本切片为 M2a Formal Round 增加一条与 Scripted Round 物理隔离的权威证据链。它解决的
问题是：未来真实 Search/Holdout 测量产生 Barrier、Holdout Reveal、FWER 和最终
EvidenceBundle 时，数据库有一个不会把真实证据混进 `synthetic=true` 表、也不会放宽现有
Scripted 约束的落点。

本切片仍然只是 Formal Authority 的持久化地基，不提供 Formal Round 启动能力，不访问 HCU，
不注册 Real Adapter Profile，不实现 Formal Finalizer、Signoff 或自动发布。

## 为什么不复用 0009 表

`0009_m2_round_authority.sql` 是 Scripted Walking Skeleton 的权威边界，其中 Barrier、Reveal、
FWER 和 EvidenceBundle 都要求 `synthetic=true`。直接把这些约束改成同时接受真实数据，会让
已有 Dry Run 表同时承载两种信任等级，审计时无法仅凭表和约束判断证据来源。

因此 `0012_m2_formal_authority.sql` 新增五张独立表：

| 表 | 作用 |
|---|---|
| `formal_round_authority_contexts` | 一次性封存 Round、Stage 0、Target、Baseline、Hotspot、Plans、Family、Evidence Store 和 D Verifier 身份 |
| `formal_round_barriers` | 分别保存 Search 批级 Barrier 和 Holdout 批级 Barrier |
| `formal_round_holdout_reveals` | 只保存 Reveal 后的 Plan Hash、本地受保护 Evidence 引用、Lease 和正 fencing token，不保存明文 Plan 或 nonce |
| `formal_multiple_comparison_results` | 保存 Holdout Family 对应的 FWER 结果 |
| `formal_round_evidence_bundles` | 汇总同一个 Context、Store、Family、Barrier、Reveal 和 FWER 的终局证据索引 |

五张表都固定 `run_mode=formal`、`synthetic=false`、
`automatic_release_allowed=false`，并通过数据库触发器拒绝 UPDATE/DELETE。

## 权威父链

Formal Authority Context 不能只相信调用方传来的 `run_mode=formal`。插入时数据库会同时验证：

1. Search Round 已冻结 Candidate Family 和 Artifact Family，且处于可测量阶段；
2. Search Round Task 是 `search_round`、`stage0_authority=formal`，绑定相同 Target Snapshot、
   Stage 0 Run、Workload 和 Adapter；
3. Stage 0 Run 为 `mode=formal`、`state=finalized`；Stage 0 Task 也持有 Formal Authority；
4. Stage 0 Evidence 明确 `synthetic=false`，并且 Run ID、Protocol Version、Protocol Hash 和
   Formal Report 一致；
5. Baseline Epoch 与 Round 的 Target、Stage 0、Workload、Configuration、Image 和 Adapter
   完全一致；Baseline SourceSnapshot 干净且非 Synthetic；
6. Hotspot 属于同一 Baseline，Candidate Kind 为 `business`，Replacement Point 与 Round 一致；
7. Context 中的 Candidate/Artifact Family、Search/Holdout Plan、Selection Rule 与冻结 Round
   完全一致，且所有路径都禁止自动发布。

Context 写入后，数据库仍允许 Round 正常推进状态和写入 Reveal 字段，但禁止修改上述已封存的
身份字段，避免“Context 没变、父 Round 偷换内容”。

## 下游绑定规则

- Search Barrier 必须绑定 Context 的 Artifact Family，并覆盖声明的全部 Candidate；
- Holdout Barrier 必须引用同 Context 下 `members_promoted` 的 Search Barrier，并等待合法
  Reveal 后才能写入；
- Reveal 必须使用正 fencing token 和绝对本地 `file:` URI，并与 Round 中冻结的 Holdout
  Family、Plan Hash、Lease ID、Evidence Hash 一致；
- FWER 必须绑定同 Context、同 Holdout Barrier、同 Holdout Family；
- EvidenceBundle 必须绑定同 Context 和 Evidence Store。走 Holdout 路径时，还要递归绑定
  Search Barrier、Reveal、Holdout Barrier、FWER、Plan Hash 和 Evidence Hash；零晋级路径则
  只能引用 `no_promotable_candidate` Search Barrier。

## 验证范围

单元测试验证不可变 Contract、Context/Payload 重哈希、Formal/Scripted 隔离、本地 Evidence
URI 和 Family 绑定。PostgreSQL 集成测试验证真实迁移及数据库 fail-closed 行为，包括并发写入
同一 Round/Phase 时只有一个 Barrier 成功。

Windows 本地没有 PostgreSQL 和 Docker 时，集成测试会显式 skip，不能把 skip 解释成数据库
通过。正式数据库结论以 GitHub CI 的 PostgreSQL 17 Job 为准。

建议命令：

```bash
ruff check .
pytest tests/unit/test_m2_formal_authority.py tests/unit/test_m2_migration.py -q
HCUOPT_DATABASE_URL=postgresql://hcuopt:hcuopt@127.0.0.1:5432/hcuopt \
  pytest tests/integration/test_m2_formal_authority_postgres.py -m postgres -q
```

## 尚未实现

完成本切片后，`formal_authority_persistence` 只能从 `block` 降为 `hold`，不能标记 `pass`。
后续仍需：

1. 生产 Repository writer 与幂等恢复路径；
2. 独立读取受保护原始证据并重算 Hash 的 Formal Finalizer；
3. Formal Round Signoff、签名决策 Artifact 与 crash-recoverable Outbox；
4. A/B/C/D 审核后的 Real Profile、Formal Plan Compiler 和 Formal StartIntent；
5. 项目所有者对精确主机、设备、时间窗、Candidate Family Hash 和预算的单独授权。

在这些条件全部通过前，readiness 继续是 `HOLD`，Formal Round creation 和自动发布继续关闭。

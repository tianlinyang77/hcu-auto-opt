# M2a Formal Authority 持久化地基

## 结论

本切片为 M2a Formal Round 增加一条与 Scripted Round 物理隔离的权威证据链。它解决的
问题是：未来真实 Search/Holdout 测量产生 Barrier、Holdout Reveal、FWER 和最终
EvidenceBundle 时，数据库有一个不会把真实证据混进 `synthetic=true` 表、也不会放宽现有
Scripted 约束的落点。

`0012` 先提供持久化地基，`0013` 与受保护 Formal Finalizer 补齐无 HCU 的生产写入和终局
验证路径，`0014` 再增加人工 Signoff Intent、内容寻址签名决策 Artifact、Outbox 和终局
Signoff。当前仍不提供 Formal Round 启动能力，不访问 HCU，不注册 Real Adapter Profile，
不开放生产签核入口或自动发布。

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

`0013_m2_formal_finalizer.sql` 进一步要求 Search Barrier 在同一事务中把晋级结果对应的
`holdout_family_hash` 写回 Round：有晋级成员时必须存在 Family Hash，零晋级时必须为空。
Holdout Barrier 只能继续引用这一个 Search 输出 Family。

`0014_m2_formal_signoff_outbox.sql` 另外增加三张签核表：

| 表 | 作用 |
|---|---|
| `formal_round_signoff_intents` | 在第一笔事务中冻结 EvidenceBundle、决策、操作者身份摘要、理由、时间和幂等输入 |
| `formal_round_signoff_outbox` | 保存待发布、已发布、已终结三个阶段以及内容寻址 Artifact 身份，支持崩溃后判定唯一下一步 |
| `formal_round_signoffs` | 在第二笔事务中保存最终签核并把 Round/Task 推进到 `completed` 或 `rejected` |

Intent 的业务输入不可修改，Outbox 只允许受控状态迁移，最终 Signoff 为 append-only。批准只表示
人工接受这份单算子 Formal 证据；它不会提升 Baseline，也不会打开自动发布。

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

## Repository 与独立 Finalizer

`PostgresRepository` 提供五个 Formal 写入边界：

1. `record_formal_authority_context()`：锁定父 Round 后幂等封存 Context；
2. `record_formal_barrier()`：校验 Candidate/Artifact 身份，Search 输出 Family 与 Barrier
   在同一事务冻结，Holdout 只能引用同 Context 的 Search 和 Reveal；
3. `record_formal_holdout_reveal()`：把 Family、Plan、Reveal Lease、fencing token 和
   Reveal Evidence 同一事务写入 Round 与 append-only 表；
4. `record_formal_multiple_comparison()`：重算 Bonferroni FWER 协议和 Result Hash；
5. `finalize_formal_search_round()`：从数据库重新读取 Context、Barrier、Reveal、FWER 和
   Budget Ledger，重新读取受保护原始 Evidence 并重算 SHA-256，成功后只进入
   `awaiting_signoff`。

Finalizer 使用 `m2a-formal-evidence-index-v1`。每条 Evidence 都声明 Producer Role、Producer
ID/Hash、Retention Owner 和访问确认时间。Barrier、FWER 等 D 结论必须由 Context 中锁定的
独立 Verifier 产生；Measurement Producer 不能冒充 D。正式本地 Reader 只接受受保护根目录
下的绝对 `file:` URI，在 POSIX 上使用 `openat/O_NOFOLLOW`，拒绝远程 URI、路径越界、符号
链接、非普通文件、超限文件、读取期间身份变化和 Hash 漂移。

终局 Bundle 的结论范围固定为 `formal_single_operation_only`，并显式声明它不是模型、服务或
端到端性能结论。无论是否存在推荐 Candidate，`automatic_release_allowed` 始终为 `false`。

## 两事务 Signoff 与崩溃恢复

Signoff 不把数据库事务跨越到文件系统写入。流程固定为：

1. `create_formal_round_signoff_intent()` 锁定等待签核的 Formal Round，重新绑定最终
   EvidenceBundle，在同一事务写入确定性 Intent 与 `pending` Outbox；
2. `LocalFormalSignoffArtifactPublisher` 对规范 JSON 决策内容签名，并写入本地内容寻址目录；
3. `record_formal_round_signoff_artifact()` 在独立事务中冻结 URI、SHA-256 和签名信封；
4. `finalize_formal_round_signoff()` 从受保护根重新读取 Artifact、重算 Hash、比对全部冻结输入
   并验签，然后在同一事务写最终 Signoff、终结 Intent/Outbox 和 Round/Task；
5. `reconcile_formal_round_signoff()` 只报告 `create_intent`、`publish_artifact`、
   `finalize_signoff`、`complete` 或人工处置，不猜测外部动作已经成功。

因此在 Intent 后崩溃可以确定性重发 Artifact，在文件已写但数据库未记录时可以重发同一 CAS
内容，在 Artifact 已记录后崩溃可以安全重做终结。重复请求必须使用同一幂等输入；同一个 Key
绑定不同决策、理由或 Evidence 会 fail closed。当前仓库里的 signer/verifier 只有测试实现，
生产认证与密钥托管仍是单独 blocker。

## 验证范围

单元测试验证不可变 Contract、Context/Payload 重哈希、Formal/Scripted 隔离、受保护本地
Evidence Root、Producer/Verifier 角色隔离、Family 绑定、零晋级和 Holdout 两条 Bundle 重建。
PostgreSQL 集成测试验证真实迁移、幂等/并发写入、部分写入恢复、Budget 未终结、Evidence
篡改以及 Evidence Finalizer 只进入 `awaiting_signoff` 的 fail-closed 行为；Signoff 测试再覆盖
批准/拒绝终态、两事务崩溃恢复、Artifact 篡改、幂等输入漂移和三张签核表的不可变性。

Windows 本地没有 PostgreSQL 和 Docker 时，集成测试会显式 skip，不能把 skip 解释成数据库
通过。正式数据库结论以 GitHub CI 的 PostgreSQL 17 Job 为准。

建议命令：

```bash
ruff check .
pytest tests/unit/test_m2_formal_authority.py \
  tests/unit/test_m2_formal_finalizer.py tests/unit/test_m2_formal_signoff.py \
  tests/unit/test_m2_migration.py -q
HCUOPT_DATABASE_URL=postgresql://hcuopt:hcuopt@127.0.0.1:5432/hcuopt \
  pytest tests/integration/test_m2_formal_authority_postgres.py -m postgres -q
```

## 仍然保持 HOLD 的部分

完成本切片后，`formal_authority_persistence` 和 `formal_evidence_finalizer` 都只能保持
`hold`，不能标记 `pass`。后续仍需：

1. A/B/C/D 审核后的 Real Profile、Formal Plan Compiler 和 Formal StartIntent；
2. 生产认证、Signer/Verifier 密钥托管和受保护 Evidence/Signoff Artifact Root；
3. PostgreSQL 17 对 Formal Authority、Finalizer 和 Signoff/Outbox 的恢复/并发验收；
4. 项目所有者对精确主机、设备、时间窗、Candidate Family Hash 和预算的单独授权。

在这些条件全部通过前，readiness 继续是 `HOLD`，Formal Round creation 和自动发布继续关闭。

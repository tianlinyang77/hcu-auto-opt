# M1 接口复盘与 M2 Go/No-Go

## 结论

M1 已完成单人工 Python startup Overlay Candidate 的真实闭环，并由项目所有者接受证据。
当前决策是 **GO_TO_M2_SCRIPTED_IMPLEMENTATION**，不是 GO_TO_M2_FORMAL：ADR-0009 已完成
四线范围评审并由项目所有者接受，允许增量 Contract/迁移、无 HCU Scripted 控制面和测试；
仍不创建真实多 Candidate Formal Task，不运行 Agent，不占用 HCU 做 M2 性能结论。

当前 Contract 字段、持久化、API、错误码和四人签字位见
[M2a SearchRound Contract 草案](m2-contract-draft.md)。该文档已冻结实现边界，但代码尚待交付。

## M1 最终权威状态

| 对象 | 值 |
| --- | --- |
| `main` | `fa91d7f674d7502ae241594570d6fb3d61c91c18` |
| Formal Stage0Run | `dd50c381-75dd-5a64-9211-640c602dc817` |
| Project Mode | `DEGRADED_MANUAL_INTAKE` |
| Task | `da4ecc3e-2c22-5016-941c-c3855343f335` / `completed` |
| Candidate | `de4e9427-340b-5428-bfa7-49aa52a1acae` / `accepted` |
| Measurement | `9b6803e7-f9d5-4080-b028-6510792b3150` |
| EvaluationRun | `45110617-5be7-51a4-8de1-d049c0e00a11` |
| EvidenceBundle | `b673fe55-2742-5edf-aea8-7ef724df0e32` |
| Signoff | `923c24ed-ce3b-54fa-9889-2e6dba39014c` / `approved` |
| 自动发布 | `false` |

详细证据见 [M1 nmz36 Formal 与签核记录](evidence/m1-formal-nmz36-20260825.md)。

## 已证明可继续复用的接口

1. **不可变权威链可用。** Task、Target Snapshot、Stage0Run、Baseline Epoch、SourceSnapshot、
   Candidate、Artifact、Measurement、EvaluationRun 和 EvidenceBundle 能形成同一条可重放绑定。
2. **A/B/C/D 责任边界可执行。** C 只产源码和制品，B 只产原始测量，D 重读证据后裁决，A
   只推进状态、持久化权威和签核。
3. **单一 Measurement Harness 可复算。** 40 次 ABBA、400 个原始样本和 40 个独立进程
   能从设备 Event 重建，Producer 没有写入 verdict。
4. **失败关闭和恢复可审计。** 外部 Runner 门禁、Telemetry 有限重试、失败样本不复用、
   一次性人工基础设施 recovery、Lease/Fencing 和健康检查均留下证据。
5. **人工签核不是发布。** Signoff 只把 Task/Candidate 变为 `completed/accepted`，数据库约束
   继续强制 `automatic_release_allowed=false`。

## 签核后必须冻结的兼容边界

ADR-0006 规定首份被签核的正式证据形成兼容边界。因此：

- `m1-kernel-performance-evidence-v1`、`MeasurementSeries`、M1 Evaluation/EvidenceBundle 的
  既有字段和语义只读冻结；
- 不能把 Search/Holdout、候选族或多重比较字段直接塞入 M1 v1 后再覆盖旧证据；
- M2 通过新 Contract、新表和新协议组合 M1 的单次比较证据；确需不兼容变化时发布新版本，
  并保留 M1 v1 的解析与回放测试；
- `manual_candidate` Workflow 继续保持一个 Task 一个 Candidate，不改造成多候选状态机。

## 发现的接口债务

### P0：进入 M2a 实现前必须解决

| 债务 | 现场事实 | M2 处理要求 |
| --- | --- | --- |
| 部署身份不够显式 | 固定端口上的 API 可能来自旧源码树；端口存活不证明具备当前 Contract | 所有可写服务公开 `source_commit`、Contract 版本和 Profile；写请求前由客户端核对 |
| 签核决定未形成文件 Artifact | D 的 `signoff.md` 实际是待签摘要，人工决定只在数据库 | 新增内容寻址 `signoff-decision.json`，绑定 EvidenceBundle、actor、decision、reason 和幂等键 |
| Evidence 存在跨根引用 | Profiler Trace、Artifact 与裁决文件位于不同根，当前依赖 URI+Hash | 增加 Round Evidence Index；签核前递归验证并声明保留策略，禁止只复制摘要 |
| M1 状态机不能表达批级 Barrier | M1 在 Candidate 级完成裁决，第二个 Candidate 被显式拒绝 | M2 新建 `SearchRound` 和批级 Barrier，不复用 M1 单候选 Task |
| Search 与 Holdout 尚未隔离 | M1 只有一套冻结 Workload 测量 | M2 在候选登记前冻结两套独立计划；Search 样本不得进入 Holdout 结论 |

### P1：M2a Scripted 闭环前解决

- 把 nmz36 一次性 Formal 常量收敛为经审核的部署 Profile/Runbook，不允许 Job payload 传入
  任意命令、Mount 或 Python 入口；
- 轮次预算必须接收实际 Build、样本、墙钟和 HCU Lease 消耗回流，而不是只保存声明上限；
- Barrier 必须保存所有失败、淘汰、`inconclusive` 和 `invalid` 候选，不能只保存赢家；
- 同一 Baseline Epoch 只表示逻辑基线相同，每个 Candidate 仍需自己的同时期 Baseline 样本，
  不得跨候选池化基线或跨行连接曲线；
- M2a 继续只接受 Python/Triton startup Overlay；HIP、`_C.so`、系统库和节点级变更不进入该链。

## 为什么先做 M2a，再做 M2b

多 Candidate 的统计和状态问题与 Agent 生成质量相互独立。如果同时引入 Agent、搜索空间、
Holdout、Barrier 和多重比较，失败时无法判断是候选生成、测量还是统计控制出错。

因此先让 2–4 个人工不可变 Candidate 通过同一条 Round 流程，证明批级同步、独立 Holdout、
FWER、预算和失败证据成立；随后 Agent/Apex 只能作为 Candidate Intake 上游的可替换 Adapter，
不能接触 HCU Worker、Measurement Harness、D 裁决或 Signoff。

## Go/No-Go

| 决策 | 当前状态 | 说明 |
| --- | --- | --- |
| M1 完成 | GO | 正式证据已由人工接受 |
| M2a 无 HCU Scripted 实现与测试 | GO | 不产生新的性能结论，不注册 Real Profile |
| M2a 真实多 Candidate Formal | HOLD | 等待 Scripted、PostgreSQL、Target Lock 退出条件和独立 HCU 授权 |
| M2b Agent Candidate Generator | STOP | 只有 M2a Target Lock 闭环通过后才重新评审 |
| 自动安装/生产发布 | STOP | 当前及 M2 均不授权 |

下一步执行计划见 [M2 搜索轮次建设计划](m2-round-plan.md)，架构决定草案见
[ADR-0009](adr/0009-m2a-round-barrier.md)。

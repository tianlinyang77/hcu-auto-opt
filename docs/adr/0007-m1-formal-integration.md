# ADR-0007：M1 以统一 Real Profile 组合四段可信 Worker

- 状态：Accepted
- 日期：2026-08-25

## 背景

M1-A/B/C/D 已分别提供控制面、唯一性能 Harness、可追溯 Overlay 制品链和独立验证核心，
但各分支尚不能组成一条真实 Worker 链。主要缺口是：正确性只有汇总布尔值、两次 HCU
租约没有作为控制面权威传给最终裁决、D 核心没有 Worker Adapter、B 与 C 之间只有匿名
Callable，以及 `invalid` 证据没有完整 EvaluationRun。

## 决策

1. 统一部署 Profile 为 `nmz36-m1-manual-v1`。默认 Catalog 仍不注册它；只有
   `candidate_builder`、`kernel_correctness`、`measurement_harness`、
   `candidate_adjudicator`、`resource_cleaner` 全部为同 Profile 的 Real Adapter 时，部署方
   才能显式加入 Catalog。
2. Hotspot Intake 必须绑定内容寻址的 `correctness_spec_uri/hash`。该规格冻结独立参考、
   shape/dtype、边界用例、种子、特殊值、重复次数和容差；缺失时不得创建正式 Candidate。
3. Correctness Worker 使用部署注入的 Evidence Producer 执行参考与 Candidate，D Adapter
   只重读原始证据并判定。结果同时保存原始证据 URI/Hash 和 D Verification Artifact
   URI/Hash，不能只保存 `correct=true`。
4. 控制面把 Correctness shared Lease 与 Performance exclusive Lease 的 Job ID、Lease ID、
   Resource ID 和 Fencing Token 持久化到 Adjudication payload。D 必须用这些权威字段重新
   构造上下文，不能从 Producer 汇总反推。
5. 当前资源表对 shared HCU Job 采用串行分配，以获得与 exclusive Job 相同的 fencing 和
   清理闭环；`shared` 仅表示正确性不要求安静计时环境，不表示当前版本支持多个并发共享
   Lease。并发共享池需要单独的资源租约表，留到后续阶段。
6. B/C 的显式边界命名为 `M1WorkloadFactory`，由 C 构造新的 Baseline 或 startup Overlay
   进程，由 B 的唯一 Harness 负责 Event、ABBA、restart、缓存、预算和原始测量证据。
7. `invalid` 也是完整裁决：必须生成 `EvaluationRun(passed=None)` 与 EvidenceBundle，
   保留失败码和原始 URI，然后使 Candidate/Task 失败关闭。它不能退化成 Worker 异常或
   缺少 EvaluationRun 的旁路对象。
8. 所有结果继续固定 `automatic_release_allowed=false`。本 ADR 不开放多 Candidate、自动
   搜索、HIP/`_C.so`、驱动/系统库修改或生产发布。

## 后果

- Scripted 环境可以端到端验证 B/C/D 的公共接口和证据重放。
- 真实 nmz36 闭环仍需在业务 Hotspot 冻结后，实现对应的 `M1WorkloadFactory` 与
  Correctness Evidence Producer；测试夹具不得注册为业务实现。
- M1 集成完成不等于性能优化完成，也不产生可发布候选。

# ADR-0004：Framework Smoke 使用双执行证据模型

- 状态：Accepted
- 日期：2026-08-18

## 背景

F1-A 原结果只能表达一组 `ExecutionRequest/ExecutionResult/ExecutionAttempt`，但 D 的
正确性协议要求 Baseline 和 No-op 分别启动新容器、新 SGLang 进程。把两个进程塞进
同一容器，或用 attempt 序号暗示 variant，都会让持久化证据产生歧义。

Target Lock 同时把 `stage0_not_measured` 当成全局 blocker，导致“尚未做性能测量”反过来
阻止了搭建测量前必需的 Framework Smoke 链路。

## 决策

1. 保留 `FrameworkSmokeResult(result_kind=single)` 兼容已有 Fake Demo。
2. 真实 Profile 强制返回 `PairedFrameworkSmokeResult(result_kind=paired)`。
3. paired 结果必须恰好包含 baseline/noop 两组请求、结果、attempt 和清理证据。
4. `ExecutionAttempt` 与数据库记录增加 `variant=legacy|baseline|noop`；同一 Evaluation
   的 baseline/noop 可以共享物理重试序号，但不能互相覆盖。
5. Target blocker 增加 `blocks` 作用域。Framework Smoke 只检查自身作用域；Stage 0、
   优化搜索和发布继续各自 fail closed。
6. 真实 Profile 顺序运行两个独立容器，在全部运行结束后才生成一个严格等价性判定。
7. 该链固定 `performance_conclusion=not_measured`，不能产生性能结论。

## 后果

- 公共平台契约从 `platform-v1.1` 升为 `platform-v1.2`。
- PostgreSQL 增加 v4 迁移，旧记录自动标记为 `legacy`，无需改写已有证据。
- A 可以无歧义持久化两组 request/attempt，D 可以把证据绑定到两个 attempt ID。
- B 的最终清理证据仍决定资源回到 `available` 或留在 `quarantined`。
- F1 可以在 Stage 0 尚未执行时运行，但仍不得进入性能搜索或自动发布。

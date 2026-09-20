# 正式启动入口：接口缺口与交付顺序

跟踪：#167；关联 #46、#102、#126、#127。

核对基线：`main@3c1fc64e335d7501b92fdba773549bdcd84c20c5`。
本文是实施计划，不是新的执行授权，也不修改 ADR-0015 的既有约束。

## 本次要解决的问题

操作者可以在页面创建 Scripted 演练轮次，但尚不能由该按钮发起正式 HCU 任务。
下一步补的是受保护的正式操作入口及执行状态接线，不重写 Agent、调度器、测量 Harness 或裁决器。

必须区分三条链路：

| 链路 | 已有能力 | 不代表什么 |
| --- | --- | --- |
| M1 单候选 Endpoint Campaign | 服务级证据与人工签核结果页；独立只读托管入口 | 不代表 M2 多候选 Formal Round 已可从页面启动 |
| M2 Scripted | 页面预览、确认、幂等创建、刷新恢复、启动审计 | `finalized` 不是性能评测完成；人工填写 actor 不是正式认证 |
| M2 Formal | 受保护预览、签名 Authority、非执行 StartIntent、B 执行适配器和 D 验真模块 | `ready_for_round_creation` 不是 Round 已创建或 Worker 已启动 |

## 已核对的接口缺口

| 环节 | 当前实现证据 | 下一切片需要补什么 |
| --- | --- | --- |
| 计划 | `operator/formal_plans.py`，部署侧重读和冻结 | 页面只选择已登记对象；展示真实 blocker，不能上传任意 Plan |
| 操作者身份 | `FormalStartActorAssertion`，coordinator 注入 verifier | 部署认证绑定 actor/action/subject/有效期；服务端形成 assertion，浏览器不持签名私钥 |
| B/D 启动授权 | 两个 StartAuthority issuer、部署 Registry/Store | 按冻结 Preview 和幂等键解析；生产身份及注册需部署验证，不能用测试 signer 代替 |
| 正式 Intent | `FormalStartCoordinator.create/reconcile/cancel/recover` | 管理面有实现，但 ADR-0015 明确禁止 Web 写路由；先提交 ADR 增量及上下游评审 |
| 正式状态读取 | `GET /v1/operator/formal-start-intents/{intent_id}` | 复用既有受保护读取；增加部署装配和页面展示，不复用 Scripted actor 标签 |
| Round 创建与派发 | Intent Contract/SQL 固定 `round_creation_allowed=false` | 单独设计事务派发桥及迁移；不能去掉 Literal/SQL 限制冒充接线完成 |
| 物理执行 | `M2FormalPhaseExecutionAdapter` 调用唯一 Harness | 对接真实注册、Lease/fencing/恢复、预算及当期 Target Lock；由 #126 验证 |
| 独立裁决 | D 递归验真、Review 持久化、受保护读取 | 复用 #127 工程交付；当前窗口生产注册和真实证据仍需验证 |

路径均相对 `src/hcuopt/`，除明确标注的文档、Contract 与 SQL。

截至本次核对，#102/#126 为 OPEN，#127 为 CLOSED。#127 的关闭范围是无 HCU 工程验收，
见其 2026-09-07 收尾评论及 `docs/evidence/m2a-no-hcu-engineering-acceptance-20260907.md`。
不重开已完成的工程任务，也不把关闭状态当成本次生产部署就绪证据。

## 实施切片

### S1：受保护 Intent 管理入口（不执行 HCU）

先写 ADR 增量，明确改变 ADR-0015 的“仅进程内写”边界，但保留非执行状态机。
由 A 负责接口和部署认证适配，B/D 复核 Authority 消费边界。

- 请求只携带冻结引用、确认信息和幂等键；actor 从已验证部署身份解析。
- 部署能力限定对象、动作、有效期与服务身份；模型 API Key 不作为操作凭据。
- 未配置部署认证/签名器、对象不匹配、过期或签名失败，在任何写入前拒绝。
- 凭据不进 URL、sessionStorage、日志或证据；如采用 Cookie，必须同时设计 CSRF 防护。
- 同一冻结请求重试复用 Intent；修改任何冻结输入不得沿用旧请求。
- 恢复请求须重新认证；凭据过期不等于丢失幂等键，也不能另建一次执行。
- 页面明确显示“授权校验完成，尚未创建执行轮次”，不显示运行中。

验收：HTTP + 真实 PostgreSQL 独立 schema 验证越权、过期、重复、丢响应、服务重启；
断言 Task/Round/Job/Lease 未创建，三个禁止字段继续为 false。

### S2：Intent 到正式 Round 的事务派发桥

独立 Contract/ADR/迁移评审，不能偷偷扩展 S1 的返回状态。

- 服务端重新验证窗口、readiness 和 B/D 绑定，事务写入唯一 Round 与 durable 派发记录。
- 不承诺消息传输“恰好一次”；采用可重试投递、唯一业务键、幂等领取和 fencing，阻止重复物理运行。
- 区分未派发、已入队、Worker 已领取与已运行；状态来自持久化事实而不是浏览器推断。
- 明确 Cancel 与领取竞争、窗口到期、Worker 崩溃及清理失败语义。
- 身份、窗口、Family、预算或 Target Lock 漂移时拒绝派发，不自动扩大授权。

验收：PostgreSQL 并发和崩溃恢复测试；模拟 Worker 重复领取、过期 fence、事务提交后响应丢失。
本切片无硬件测试不能宣称正式 HCU 执行通过。

### S3：当期环境部署与页面联验

在 #102 readiness 与 #126 执行部署条件满足后，固定 Profile/Family/预算和具体资源窗口，
安排真实执行；沿既有 Harness、D 裁决和人工签核链路收口。

页面按真实事实展示：提交意图 → 授权核对 → 排队 → 执行 → 证据校验 → 待人工签核。
每个阶段保留失败、取消和恢复入口；不能由 Intent ready 推导后续状态。

验收材料同时给出代码版本、计划/环境/制品 Hash、执行回执、原始样本、D 结论和资源恢复证据。
性能结论可能是接受、拒绝或证据不足；链路完成不保证有加速。

## 固定边界

- `automatic_release_allowed=false`，不自动提升 Baseline。
- 不改变机器频率，不停止无关服务或进程。
- 不将既有 Endpoint 签核迁移成新 Formal Round 的授权。
- 不把 Scripted 成功、CI 全绿或 Issue 关闭替代当期实机验收。
- 不把多节点、自进化、自动发布纳入本次接口交付。

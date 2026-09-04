# ADR-0015：M2a Formal StartIntent 采用独立的非执行状态机

- 状态：Accepted
- 日期：2026-09-04
- 相关：ADR-0009、ADR-0012、ADR-0013、ADR-0014，Issue #125/#126/#127

## 背景

A2a 已能从部署侧 Store 与 PostgreSQL 重读并冻结 Formal Plan，但它按设计固定
`start_allowed=false`。B 的 Formal Phase Adapter 与 D 的生产 Evidence Verifier 也只完成了
无 HCU 地基，尚未形成可供 A 消费的窗口级启动授权。把 Scripted `operator_start_intents` 表或
`OperatorStartCoordinator` 放宽会混淆 `fixture` 与 `business` Candidate，并让控制面有机会替 B/D
选择 Adapter、Lease、Holdout 或 Evidence Authority。

## 决定

新增独立的 `m2a-formal-start-intent-v1` Contract、协调器和 PostgreSQL 表：

1. 客户端只提交 Preview、Resolved Plan、owner window、B Authority、D Authority 的内容 Hash，
   不能提交 Plan 内容、Candidate 列表、Adapter 配置、Holdout 内容或签核事实。
2. A 从部署侧对象 Store 重读 A2a Preview、B execution Start Authority 和 D evaluation Start
   Authority，重新计算内容 Hash，并通过三个独立的部署 Verifier 校验 operator、B、D 签名。
3. B Authority 精确绑定 Adapter Profile、候选 Family、预算、host/resource/window，以及
   lease/fencing/cleanup policy Hash；D Authority 精确绑定 Search Plan、密封 Holdout commitment、
   HoldoutPlanAuthority、selection rule、Evidence Root 和独立 Verifier。
   两者还必须绑定由启动幂等键确定的唯一 `intent_id/preview_id/task_id/round_id`，同一份授权不得
   被另一个 StartIntent 复用成第二个 Round。
4. operator、B signer、D signer、HoldoutPlanAuthority 与独立 D Verifier 必须身份和 Hash 分离。
5. Intent 使用确定性 `intent_id/task_id/round_id/member_id`、幂等创建、可重入 reconcile、Cancel
   和启动恢复；人工 reconcile/Cancel 必须由创建 Intent 的同一签名 actor 发起，内部启动恢复仍由部署
   管理进程执行；状态变化与 append-only event 在同一 PostgreSQL 事务提交。
6. 这一切片即使 Authority 完整，也只到 `ready_for_round_creation`。Contract 和数据库仍固定
   `round_creation_allowed=false`、`hcu_accessed=false`、`automatic_release_allowed=false`。
7. Web 只提供注入部署鉴权后的 GET；未配置鉴权返回 503，鉴权拒绝返回 403。Web 不提供 Formal
   create/reconcile/cancel 写路由。受控写调用只允许进程内管理面使用 `FormalStartCoordinator`。

## 失败语义

- 对象暂时不存在、Profile 暂时不可读或授权暂时未生效：保持 `awaiting_authority`，记录稳定
  blocker，后续可恢复。
- Plan/窗口漂移、Profile 撤销或模式错误、畸形 Contract、内容 Hash 错误、签名拒绝、授权复用、
  角色复用：fail closed 为安全的 terminal failure；不保存底层敏感异常。
- production authentication 或任一 Signer/Verifier 未配置：在持久化前拒绝创建。
- replay 只有在请求摘要、actor assertion、五个 Authority 引用完全一致时才幂等成功。

## 后果

A3/A4 的控制面和恢复机制可以先落地，但它不会把 B/D 尚未完成的生产 Authority 伪装成通过。
当前 readiness 只能把 `formal_start_intent` 从 `block` 提升到 `hold`；B #126 与 D #127 形成真实
签名授权、A/B/D 独立复核并重新冻结 readiness 之后，才可能进入实际 Round 创建切片。

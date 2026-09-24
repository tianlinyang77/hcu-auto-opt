# M2a Formal StartIntent

## 这部分解决什么

Formal Plan 生成以后，系统还不能马上创建 Round。A 必须在同一个精确窗口内重新确认：

- A1 的项目所有者 window authorization 和 A2a Resolved Plan 没有漂移；
- B 已授权一个真实注册的执行 Adapter，并冻结 Lease、fencing、cleanup policy 和预算；
- D 已冻结 Search/Holdout、HoldoutPlanAuthority、Evidence Root 与独立 Verifier；
- B/D Authority 同时绑定本次唯一的 Intent、Preview、Task 和 Round，不能换幂等键重复启动；
- 发起人、owner window verifier、B、D、Holdout 和独立 Verifier 不是同一个身份；
- 请求没有偷偷携带另一套 Candidate、Adapter、Plan 或 signoff 事实。

B execution Start Authority 使用 `m2a-formal-execution-start-authority-v2`。它只能在授权窗口已经
开始、A2a Preview/Plan Hash 已经存在以后签发，并且 A 会拒绝未来 `issued_at`；此前 v1 的
“必须在窗口开始前签发”与 Preview 的生成顺序互相矛盾，且尚无生产 v1 Artifact，因此直接停止使用。

D evaluation Start Authority 只能由部署侧 issuer 从已注册的 Search/Holdout commitment、Evidence
Root 和 Verifier identity 形成。普通请求不能传入 Holdout 明文、Evidence URI 或统计规则；D
Authority 也不能替代 Round 结束后的递归 EvidenceBundle/Barrier/FWER 验真。

实现位于：

- `src/hcuopt/contracts/m2_formal_start_v1.py`：actor、B、D 与 StartIntent Contract；
- `src/hcuopt/operator/formal_start.py`：重读、验签、幂等、reconcile、cancel 和恢复；
- `src/hcuopt/operator/formal_start_store.py`：B/D Authority 内容寻址、原子发布与安全重读；
- `src/hcuopt/storage/sql/0018_m2a_formal_start_intent.sql`：独立快照表与 append-only event；
- `tests/unit/test_formal_operator_start.py`：权限、漂移、恢复、只读 API 负向测试；
- `tests/unit/test_formal_start_authority_store.py`：真实 B/D issuer 到 A coordinator 的无 HCU 联合链路；
- `tests/integration/test_formal_start_intent_postgres.py`：PostgreSQL 并发与审计约束。

## 状态机

```text
create（已验证 operator assertion）
  └─ awaiting_authority
       ├─ B/D 对象暂缺或 Profile 暂时不可读 → 保持等待，可恢复 reconcile
       ├─ Hash/签名/窗口/Profile 撤销/授权复用/角色绑定错误 → failed
       ├─ cancel（新的已验证 assertion） → cancelled
       └─ 全部 Authority 精确匹配 → ready_for_round_creation
```

`ready_for_round_creation` 只表示 A3 已把启动输入核对齐。当前 Contract 与数据库继续强制：

```text
round_creation_allowed = false
hcu_accessed = false
automatic_release_allowed = false
```

因此它不是 Formal Round、不是 HCU 执行许可，也不是性能结论。

## 调用边界

默认部署仍遵循下列只读边界。ADR-0024 提出可选的非执行 HTTP 创建入口；开发实现仅在
`create_app(formal_start_management=...)` 显式注入后注册，尚未批准生产开放。
它不会解除 Round/HCU/自动发布禁止字段。详见 `docs/adr/0024-formal-intent-management-api.md`。

Formal 写操作不暴露为 Web API。部署管理进程必须显式构造三个 production Verifier 和部署对象
Store，然后进程内调用 `FormalStartCoordinator.create/reconcile/cancel/recover`；缺少任一个 Verifier
会在写入数据库前失败。人工 `reconcile/cancel` 的签名 assertion 必须属于创建该 Intent 的同一
actor；`recover` 是部署管理进程使用的内部崩溃恢复入口，不接收用户 assertion。

Web 只提供受保护的只读状态：

```text
GET /v1/operator/formal-start-intents/{intent_id}
```

只有部署方给 `create_app(formal_start_read_authorizer=...)` 注入鉴权后才可读取。CLI 同样只有只读
命令：

```bash
export HCUOPT_FORMAL_START_READ_TOKEN='<deployment-issued-token>'
hcuopt formal-start status <intent-id> --json
```

CLI 不提供 Formal start/reconcile/cancel，避免把普通用户入口变成生产执行入口。

部署 Store 的装配顺序为：复用既有 A2a Preview Store → B/D issuer 各自重读部署注册并签发 →
`publish_execution_authority` / `publish_evaluation_authority` 原子发布 → A 只按请求中的 Hash 重读。
Authority 文件共用一个 Hash 命名空间，跨 execution/evaluation 类型读取会被拒绝；Store 不校验签名，
签名始终由 A 注入的独立 production Verifier 校验。Store 不是普通 Web 上传目录，也不接收未带
embedded Hash 与签名的半成品内容。

## 当前实际状态

本切片已证明 test-only B/D issuer、Authority Store、actor assertion 与 A coordinator 可以到达
`ready_for_round_creation`，同时三个禁止字段仍为 false。仓库仍不提交 production key、Real
Adapter registration、production Evidence Root、Holdout nonce 或真实 signed Authority。因此项目
仍是 `HOLD`，不会创建真实 Formal Round；#126/#127 继续开放，等待部署真实注册与独立复核。

# M2a Formal StartIntent

## 这部分解决什么

Formal Plan 生成以后，系统还不能马上创建 Round。A 必须在同一个精确窗口内重新确认：

- A1 的项目所有者 window authorization 和 A2a Resolved Plan 没有漂移；
- B 已授权一个真实注册的执行 Adapter，并冻结 Lease、fencing、cleanup policy 和预算；
- D 已冻结 Search/Holdout、HoldoutPlanAuthority、Evidence Root 与独立 Verifier；
- B/D Authority 同时绑定本次唯一的 Intent、Preview、Task 和 Round，不能换幂等键重复启动；
- 发起人、B、D、Holdout 和独立 Verifier 不是同一个身份；
- 请求没有偷偷携带另一套 Candidate、Adapter、Plan 或 signoff 事实。

实现位于：

- `src/hcuopt/contracts/m2_formal_start_v1.py`：actor、B、D 与 StartIntent Contract；
- `src/hcuopt/operator/formal_start.py`：重读、验签、幂等、reconcile、cancel 和恢复；
- `src/hcuopt/storage/sql/0018_m2a_formal_start_intent.sql`：独立快照表与 append-only event；
- `tests/unit/test_formal_operator_start.py`：权限、漂移、恢复、只读 API 负向测试；
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

Formal 写操作不暴露为 Web API。部署管理进程必须显式构造三个 production Verifier 和部署对象
Store，然后进程内调用 `FormalStartCoordinator.create/reconcile/cancel/recover`；缺少任一个 Verifier
会在写入数据库前失败。

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

## 当前实际状态

本切片不提交 production key、Real Adapter registration、B/D signed Authority 或 owner window。
因此当前项目仍是 `HOLD`，不会创建真实 Formal Round。B #126、D #127 完成后，可以向部署对象
Store 放入它们各自签名且内容寻址的 Authority，再由同一 Intent 恢复核验。

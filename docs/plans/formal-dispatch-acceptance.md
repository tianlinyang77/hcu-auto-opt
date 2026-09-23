# Formal 派发桥实施与验收清单

依据：ADR-0025（Proposed）。此清单是待实现验收标准，不是通过记录。

## 最小交付拆分

| 切片 | 修改位置 | 完成条件 |
| --- | --- | --- |
| S2a 接口与迁移 | contracts、storage/sql、operator/formal_start.py | Dispatch 冻结绑定、版本、取消/恢复语义通过 A/B/D 评审 |
| S2b 原子持久化 | storage/repository.py 或独立 mixin | 单 connection 写 Task/Round/成员/Dispatch/outbox；任意失败全部回滚 |
| S2c 受控消费 | orchestrator、workers、B Adapter 接线 | 重复投递无重复物理执行；未知执行结果先恢复，不盲目重试 |
| S2d 读模型和页面 | api、web | 状态源是持久化事实，领取不显示成实机运行，完成不显示成加速通过 |

不直接复用 Scripted `create_search_round()`：它有 synthetic/fixture 专属校验。
可抽取同连接 SQL helper，但原有 Scripted 回归与 Formal 默认拒绝必须保留。
修改 repository.py 后检查 readiness 清单中的内容 Hash；只在完成审查后更新对应证据，不能关闭校验。

## PostgreSQL 必测矩阵

每个测试使用随机隔离 schema，不清空共享表；禁止用 SQLite 替代锁与并发测试。

| 场景 | 必须断言 |
| --- | --- |
| 两个相同请求并发派发 | 一个 Task、一个 Round、精确成员集合、一个 Dispatch 和一组唯一 outbox 键 |
| 同 Intent 不同绑定 | Conflict；原记录与事件不变 |
| 写 Task 后/写半数成员后/写 outbox 前故障 | 整体回滚，没有可领取对象 |
| 提交成功但响应丢失，再重启进程重放 | 返回原身份，不重复 outbox；不要求重新执行 |
| Cancel 先取得 Intent 锁 | 后续派发拒绝，无可执行 Job |
| 派发先提交、未领取时 Cancel | 阻断后续投递/领取，保留已创建 Round 的取消事实 |
| 领取与 Cancel 竞争 | 只能出现取消成功且未领取，或已领取并进入受控停止路径 |
| 验证后 Intent version/Profile 状态发生改变 | 新派发拒绝，不能沿用旧快照 |
| 等待锁期间授权/Preview 过期 | 使用锁后数据库实际时间拒绝，不以事务开始时间放行 |
| 已派发后旧 reconcile/recover 再跑 | 不重建 Round、不覆盖派发事实；旧写版本不能混跑 |
| Worker 领取后崩溃且无终态 Receipt | recovery_required；没有第二次直接执行 |
| outbox 重投/旧 Fence 迟到 | 旧 Worker 无法写当前结果；无重复物理调用 |
| 资源 Lease 过期但残留进程未清理 | 不重新分配资源、不记为安全取消 |
| 清理失败/预算不足/授权撤销 | 稳定失败码与审计；不扩大预算或自动换资源 |
| 默认部署与 Scripted 队列扫描 | 不创建 Formal 任务，不消费 Formal outbox |

## 非数据库验证

- Contract 拒绝错误版本、额外字段、非 canonical Hash、错误模式、同名不同内容及身份改绑。
- 2–4 个 business 成员的顺序和确定性 ID 与旧 Intent 完全一致，不能改为 fixture。
- 原 Stage0、Baseline、Hotspot、Profile、B/D Authority、独立角色及证据根验真仍生效。
- 不揭示 Holdout，源码/候选/制品 Family Hash 不混用。
- 页面刷新、丢响应、凭据过期、拒绝与取消显示不误导；凭据仍不进入 URL/浏览器存储。
- 单元测试中的调用计数只证明模拟执行幂等；真实 HCU 恢复与 Fence 必须另行验收。

## 放行条件

Contract/ADR 获批、迁移与新旧进程切换方案可执行、PostgreSQL 全矩阵通过，才进入受控部署联验。
实际 HCU 执行仍需当期生产身份、B/D 注册、有效资源窗口和资源恢复机制。
测试全绿不替代这些前提，也不自动合并或自动发布。

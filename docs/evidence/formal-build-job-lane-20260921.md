# Formal 构建 Job 隔离与单次领取

## 改动

- migration 32：general/formal 执行通道不可变，Formal Job 关键绑定不可变。
- 专用 Job 创建：有效 Claim 下创建 CPU/manual_build，绑定候选与领取人，仅一次 Attempt。
- 原预算预留不变；构建日志与 Job 领取在同一事务提交，失败一起回滚。
- 构建成功、结算、制品发布后记 Job 成功；不推进整个 Task，也不触发旧 Workflow。
- 普通领取、失联重排、成功 Job 扫描和通用所有权写入口隔离 Formal Job。

## 验证

本地定向单测最终 80 passed（含新增 Job 11 项、readiness 11 项及原相关 58 项），Ruff 通过。
新用例验证精确候选/领取人绑定、错误通道/状态/Attempt/Worker/Profile 拒绝及静态迁移约束。
这些单元测试使用仓储替身，不能代替 SQL 并发和事务验证。

新增 PostgreSQL 用例已编写：普通 Job 可领取而 Formal 不可领取；普通恢复器不能重排
Formal；通用完成不能代办；通道不可改写；Formal 成功不进入旧 Workflow。
原构建日志测试改为真实专用 Job 创建，并增加幂等入队、领取回滚和单次完成断言。
本轮最初 SSH 两次超时；用户恢复网络后启动数据库回归。
首轮 4 passed / 9 errors，发现误用 worker_type='cpu'，已修为仓库已有 build 类型，
同时修正产品路径、SQL 和夹具，不新增或放宽枚举。
最终 PostgreSQL 组合回归 **13 passed，205.66 秒**：Job 隔离 2 项、构建日志/结算/
Consumer 恢复 7 项、原制品发布 4 项。额外 Worker 资源/租约/完成心跳单测 **28 passed**。
临时隧道已关闭，随机 schema 由测试清理；签名、源码和 Builder 返回值仍是显式夹具，
数据库事务与账本真实，不等于真实 Builder 或 HCU 联验。

readiness 只更新已改 repository.py 的 LF 文本摘要，不改变 HOLD、批准或自动发布权限。
未执行 HCU、未改频率、未重跑 GitHub CI。迁移待审；生产默认禁用。

## 剩余

有界执行/Claim 生命周期；未知失败的人工恢复与预算对账；真实 Builder
联合验证；正确性与 Family 冻结。升级不得混用不知道 execution_lane 的旧 Worker。

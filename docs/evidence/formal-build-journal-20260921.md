# Formal 构建调用日志与单次消费

## 实现范围

- 迁移 31：每个 Intent/Candidate 一个不可重试槽位，绑定领取人、Token、输入 Hash 和已有预算预留。
- 先提交 invoking，再调用已有 Formal Builder；同候选更换输入或 Reservation 被拒绝。
- 保存完整构建输出后才调用原子发布 Store。发布失败可重放原输出，不再次构建。
- Builder 或清理结果不明时进入 recovery_required；不自动重试、释放预算或伪造 build_failed。
- 停止后可留存返回的结果，但发布仍需有效授权；留存不是验收通过。

## 验证范围与限制

数据库使用已有 PostgreSQL 服务，每例独立随机 schema，结束清理自己创建的 schema。
测试包含真实 SQL 事务、预算预留 API、并发唯一调用、终态不可变和制品文件校验。
Builder 返回值及签名/源码来源是显式夹具；没有调用模型、HCU 或证明真实性能收益。

首轮因迁移注册缺漏出现 3 failed / 4 passed，补登记后 7 passed。
新增 Consumer 用例后，旧夹具的 Git tree hash 格式不符合 SourceSnapshot，出现
2 failed / 7 passed；仅修正隔离夹具，不放宽产品校验。最终组合回归：9 passed，112.95 秒。
其中 5 项日志/Consumer 集成用例，4 项原构建发布回归。临时隧道已关闭。

相关单元测试（迁移、Phase Consumer、Phase Prepare）：49 passed。
定向 Ruff 通过；未重跑 GitHub CI，未宣称全仓测试通过。

## 尚未交付

这是默认关闭的单次调用组件，不是生产 Worker 已部署。仍需隔离 Job 领取、预算用量结算、
执行时限与 Claim 生命周期、明确失败回执/恢复入口，以及正确性、Family 冻结和端到端验收。
现阶段不得同时让普通 Worker 消费同一 Job。预算仍保留原 queued Job/Attempt 守卫。
调用者必须保留原输入供输出重放；输入 Hash 不等于原输入备份。

# Formal 持久化阶段日志及消费入口

关联 ADR-0025、PR #169、Issue #167。

## 本次变化

- 迁移 30：不可重开的 Intent/Candidate/Phase 槽位，冻结完整请求、执行身份和领取身份。
- 在调用适配器之前持久化 invoking；可能已经调用时不自动再次执行。
- 失败/成功回执都须与完整请求匹配后保存为 receipt_recorded，重复消费只读回执。
- 未知异常进入 recovery_required，迟到回执不能覆盖它；无重新入队或自动释放。
- 默认关闭的 FormalPhaseConsumer 调用现有 B 适配器，强制注入领取/停止检查点。
- 复用原 Candidate/Phase、B Receipt、预算及测量协议，没有第二套 Harness。

## 验证范围

真实 PostgreSQL：既有测试实例、随机隔离 schema，临时 SSH 回环隧道；
仅清理测试自身 schema，无 HCU、无新增服务容器、无原有数据清空。
首轮新增日志测试 4 passed（41.48s）。
补充 Consumer 重放用例后，与停止/领取/派发/HTTP 集成测试联合执行：
**19 passed（168.51s）**。包括事务回滚、并发唯一性、不可变终态、迟到结果拒绝，
以及重建消费对象后只读回执、执行回调计数仍为一次。临时隧道已关闭。
这些测试的身份/源码与执行回执为显式夹具；数据库事务、唯一约束、触发器和回执文件读写真实。

Consumer 单测使用真实 B 适配器代码及回执存储，Harness/预算/清理/日志为夹具；
验证默认关闭、成功与失败回执重放、异常及回执写入失败后禁止再次调用。
这些调用计数不是物理 HCU exactly-once 证明。
相关后端合并回归：127 passed、2 skipped（19.61s）；跳过为 Windows 符号链接权限依赖。
Ruff 通过；无前端改动。GitHub CI 按用户要求暂不等待，不改变工作流或分支保护。

## 操作边界

本阶段没有开放新的 Web 执行权限，也没有启动生产 Worker。
invoking 只表示可能已调用，receipt_recorded 只表示已保存终态回执，失败仍是失败。
Claim Token 不等于设备 Fence；清理通过仍须由当前 B 资源证据证明。
尚需生产 PhaseRequest 准备与调度装配、运行中停止、当前资源恢复验证。
automatic_release_allowed=false，ADR 待审，PR Draft，不关闭整体 Issue。

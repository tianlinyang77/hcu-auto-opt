# Formal 单次领取：隔离数据库验证

日期：2026-09-21；关联 ADR-0025、PR #169、Issue #167。

## 本次交付

- 迁移 28：单次领取记录、不可变 Worker/Token/时间绑定、追加式审计事件。
- 默认关闭的 `PostgresFormalClaimStore`：重新验签和数据库 Authority 核对，
  同一 Intent 锁内唯一领取，期限不超过授权窗口。
- 领取与取消互斥；已领取时拒绝旧取消，不能冒充资源安全释放。
- 到期只进入 recovery_required，无重新领取、续租、完成或设备执行入口。
- 页面显示“控制面已领取”和“需恢复”，保持物理消费者未启用及自动发布禁止。

## 实测范围

本机测试进程通过短期 SSH 回环隧道访问原有 PostgreSQL 17 测试实例。
每个用例使用随机 schema，仅清理自己的 schema；测试后关闭本轮隧道。
不访问 HCU、不更改频率，不启动新的服务容器。

1. 原派发、HTTP 及前三个领取集成测试联合执行：9 passed（73.09s）。
2. 增加故障注入后重新执行完整 `test_formal_claim_postgres.py`：4 passed（36.60s）。
   覆盖并发唯一领取、进程重建后禁止重领、到期恢复标记、取消竞争、默认关闭、
   TTL 边界、验签撤销、审计失败整体回滚、Worker 身份篡改拒绝和零普通 Job。
3. 正式入口/派发/迁移/readiness 定向后端回归：83 passed。
4. Web lint、56 项单测、构建通过；仍有既存单 chunk 超 500 kB 提醒。
5. Ruff、diff check 通过。按用户要求不等待 GitHub CI；不宣称 Linux 全量已通过。

## 明确未完成

这是受控消费者的领取基础，不是物理消费者或自动优化整体完成。
claim_token 不是设备 Fence，超时不证明资源空闲；没有宣称物理 exactly-once。
尚需停止请求、B 资源 Lease/Fence、真实执行回调与 Receipt、清理证明、受控重试、
执行成功到 D 裁决/人工签核的接线。没有新的实机性能结论。

部署须先迁移并统一升级读写进程；旧页面/状态模型不能读取新增状态。
当前 API 返回的 execution_consumer_enabled 继续为 false。
ADR 仍 Proposed，PR 保持 Draft，不关闭 #167/#102/#126。

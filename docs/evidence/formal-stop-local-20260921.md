# Formal 停止请求与执行前检查

关联：ADR-0025、PR #169、Issue #167。此切片不启用 HCU 执行。

## 实现范围

- 迁移 29 保存追加式停止请求，外键精确绑定当前 Intent/Worker/claim_token。
- 同部署受信服务可请求停止；同请求幂等，不同请求人/原因拒绝覆写。
- `assert_active` 复验当前签名、数据库 Authority、Intent、领取身份和有效期，
  并拒绝停止请求之后的下一阶段检查。停止请求即使在领取过期后也可以保存。
- 页面读取 `stop_requested`，明确它不证明执行停止或资源释放；
  到期进入 recovery_required 后优先显示恢复需求，保留停止审计。
- 没有新增 Web 写权限，不扩张旧 Intent capability，没有停止任何真实进程。

## 验证记录

使用已有 PostgreSQL 测试实例，每个测试随机隔离 schema，完成后只清理自身 schema。
临时 SSH 回环隧道在 finally 中关闭，密码不持久化、不写日志。

首轮四个相关集成文件联合执行：13 passed、1 failed（122.60s）。
失败来自测试夹具给 coordinator 的只读 service_identity 属性赋值；
已改为替换测试 compiler 的身份，不改业务权限判断。
修正后完整重跑 `tests/integration/test_formal_stop_postgres.py`：
**4 passed（42.96s）**，覆盖并发幂等、重建服务读取、旧身份/Token/授权拒绝、
过期停止、数据库不可变保护及插入后异常回滚。

后端正式入口/派发/迁移/readiness 定向回归 83 passed；
Web lint、56 项单测、构建、4 项 Sites 包装测试通过；Ruff/diff check 通过。
构建仍有既存单 chunk 超 500 kB 的非阻塞提示。

## 边界及后续

执行前检查与外部进程启动不是原子操作；检查通过后收到停止请求，仍需
执行器协作取消和 B Fence。**本次不证明运行中的任务已能自动停机。**
清理证明验真尚未接线：必须先冻结实际 Phase/Attempt/Lease/资源绑定，
再复用 B 执行回执和资源健康证明；不能接受客户端自报 cleanup=true 代替。
没有重新入队、自动释放、自动发布或新的性能结论。正式物理 Consumer 仍未完成。
先迁移后统一升级读写服务；ADR 仍 Proposed，PR Draft，Issue 不关闭。

# Formal 原子创建与进度读取：本地替代 CI 验证

日期：2026-09-20；关联 PR #169、Issue #167；依赖 PR #168。

## 本次代码范围

- `prepare_formal_round`：复验既有 Authority，确定性映射 Formal Round 与 business 成员。
- `PostgresFormalDispatcher`：默认关闭，单事务保存完整 intake、创建 outbox 和事件；无通用 Job。
- 迁移 27：创建 outbox、不可变绑定、Intent/Dispatch 取消联动、旧写路径保护。
- 可选受保护只读 API 与页面查询：只显示持久化状态，不从 Intent ready 推导实机执行。

## 验证方法与结果

GitHub 额度耗尽后，按用户要求不再等待或重跑 CI；未删除、弱化仓库检查。
首次提交的 PostgreSQL CI 已通过，但后续提交不借用该结果作为全量验收。

实际替代验证使用 Windows 本机测试进程，经短生命周期 SSH 回环隧道连接原有 PostgreSQL 17
测试实例。每个集成测试新建随机隔离 schema，结束仅清理自身 schema；未迁移或清空原有应用表。
临时隧道测试后关闭，数据库密码未写入源码、日志、报告或凭据文件。

命令：

```text
python -m pytest tests/integration/test_formal_dispatch_postgres.py tests/integration/test_formal_start_management_postgres.py -q
```

6 passed。覆盖真实数据库 Authority 重读、并发唯一创建、重启重放、queued 取消、默认禁用、
事务故障全回滚、锁内过期/版本检查、冻结绑定篡改拒绝、Scripted 调度拒绝 Formal，
以及独立凭据配置加载/撤销。签名和源码使用显式测试夹具，不是生产授权验证。

最后一轮本地定向测试（含派发、HTTP、专用控制台、旧 Intent、迁移与 readiness）83 passed；
前端 lint 通过，56 项单测通过，
生产构建通过，Sites 包装检查 4 passed。构建存在单个 JS chunk 超过 500 kB 的非阻塞告警。

浏览器在本地回环 4198 完成读取计划、确认、提交意图、再次输入测试凭据并查询状态。
页面顶部保留“内存数据库与测试签名”提示，查询显示 `not_created`，不把它冒充数据库联验。
4198 为单独 UI 夹具；上面的 PostgreSQL 验证是自动化测试，两者不是同一次端到端部署。

补充边界：6 项数据库测试通过之后，为同一集成用例追加了 `read_status` 的
not_created/queued/cancelled 三个读取断言。该次补跑在建立凭据阶段遇到测试主机 Docker
包装命令故障，pytest 没有启动，因此新增三个数据库读取断言仍待补跑；不能用前一次
6 passed 冒充这一版已全部验证。只读 HTTP 的测试替身验证和前端浏览器验证已通过。
未尝试替换或绕过测试主机的 Docker 包装程序。

## 未完成项

- outbox Consumer、正式 Worker 领取/续租/Fence/崩溃恢复尚未接通。
- 生产身份/密钥装配、当期执行窗口注册及页面到真实硬件的整体联验未完成。
- 新数据库与接口约束仍需 ADR/上下游评审；PR 保持 Draft，不自动合入。
- 没有 HCU 性能测量、加速结论、Baseline 提升或自动发布。

因此本报告证明的是创建事务和状态读取切片，不是整体 MVP 完成。

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

补跑更新：测试主机 Docker 命令恢复后，在 `ad19569` 上重新执行上述两个测试文件，
结果为 **6 passed（45.87s）**，覆盖最后追加的 `read_status` 的
not_created/queued/cancelled 三个读取断言。临时隧道已关闭，未修改或绕过 Docker 包装程序。
前端重新验证：lint、56 项单测、构建和 4 项 Sites 包装测试均通过。
Scripted 集成冒烟 `tests/integration/test_sglang_smoke_scripted.py`：
13 passed、4 skipped（34.68s）；只验证模拟链路，不代表 SGLang 实机执行。

额外执行 Windows 全量 `python -m pytest tests/unit -q`，结果为
**1746 passed、46 skipped、18 failed（363.21s）**，不能作为全仓通过证明。
失败集中在 F1-C pipeline、GitSourceManager、M1 Candidate Builder、No-op Builder
的符号链接夹具，以及 LocalArtifactStore 的 Windows 只读临时文件清理。
独立复现后者为 `temporary_path.unlink()` 报 WinError 5。
仓库 CI 原本只在 Linux 运行完整 unit 集，Windows 使用定向清单；
这些失败仍予保留，不通过删除测试、放宽制品只读约束或更改系统权限掩盖。
当前本地验证不等于 Linux 全量回归，后续需在 Linux CPU 环境补齐。
随后按 CI 的 Windows 定向清单执行组合回归，中途长期无进度，已中断本次测试进程；
未得到最终汇总，不能记为通过。没有停止预览或其他进程。

## 未完成项

- outbox Consumer、正式 Worker 领取/续租/Fence/崩溃恢复尚未接通。
- 生产身份/密钥装配、当期执行窗口注册及页面到真实硬件的整体联验未完成。
- 新数据库与接口约束仍需 ADR/上下游评审；PR 保持 Draft，不自动合入。
- 没有 HCU 性能测量、加速结论、Baseline 提升或自动发布。

因此本报告证明的是创建事务和状态读取切片，不是整体 MVP 完成。

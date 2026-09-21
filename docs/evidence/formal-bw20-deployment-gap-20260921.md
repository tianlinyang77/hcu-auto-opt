# BW20 Formal 部署核查与升级演练

日期：2026-09-21。核查代码基线：`2833868`。
范围：只读检查现有 BW20 数据库；在另一台现有 CPU 测试容器的随机 schema 中验证升级。
未停止容器、未启动 HCU、未修改频率、未迁移真实数据库。

## 现场事实

SSH 返回主机名 `github-bw20`，项目 PostgreSQL 容器为
`hcuopt-bw20-stage0-db-feb58e54`。使用 `psql -X -v ON_ERROR_STOP=1`
在 `BEGIN READ ONLY` 事务中查询现有表和聚合计数，没有导出凭据或原始任务载荷。

| 检查 | 结果 |
| --- | --- |
| `schema_migrations` 最大版本 | 26 |
| `formal_operator_start_intents` 数量 | 0 |
| `search_rounds` 按 run_mode 聚合 | 无记录 |
| `formal_correctness_%` Job 事件 | 无记录 |
| `formal_round_dispatches` / `formal_dispatch_claims` | 未安装 |
| `formal_dispatch_stop_requests` / `formal_phase_journal` / `formal_build_journal` | 未安装 |
| `jobs.execution_lane` | 不存在 |
| 历史 `manual_correctness` succeeded | 2 条；不是新 Formal 通道执行证明 |

因此当前不能给 recovery reader 绑定真实 Formal invocation。
旧 M1、服务级验收记录仍然有效，但不能改写成 Formal 执行事实。
本机此前的 `preview-formal-intent.py` 引用单元测试工厂，属于夹具预览，不是生产部署。

## 升级演练

新增 `tests/integration/test_formal_upgrade_postgres.py`，在随机隔离 schema 中：

1. 仅应用迁移 1–26，模拟现场数据库结构。
2. 写入明确测试夹具，覆盖 queued/running/succeeded/failed 的旧正确性任务。
3. 应用后续迁移，并再次执行 migrate 检查幂等。
4. 对比完整旧 Job 行，除新增 `execution_lane=general` 外没有改变；新派发表和日志表为空。
5. 验证旧 Job 不能通过 UPDATE 换到 formal 通道。
6. 另一路在迁移 30 注入故障，确认迁移事务整体回滚到 26，旧任务不变；随后恢复正常升级。

Linux/Python 3.10/PostgreSQL 组合回归 **27 passed，36.68 秒**，Ruff 通过。
本演练不是生产数据备份恢复演练，也没有测量生产库 DDL 锁等待或升级停机时间。

## 接下来必须完成的部署步骤

1. 完成 ADR-0024/0025 与依赖 PR 的部署评审，冻结服务代码和配置身份。
2. 做真实库备份及恢复验证，核对运行中的旧服务与 DDL 锁；在受控部署步骤中迁移 27–32。
3. 配置真实签名/能力凭据和当期 Authority，创建真实 Formal Intent/轮次，不使用测试签名器。
4. 运行新通道的受控执行，持久化 invocation；绑定该原始 journal 到只读 recovery reader。
5. 用浏览器读取并核对数据库事实；再验收 Unknown 的物理清理与恢复。

只安装迁移不会启动 HCU，但也不会自动补出这些授权、任务和执行证据。
当前生产消费者保持关闭，不据本次演练声明实机页面联验完成。

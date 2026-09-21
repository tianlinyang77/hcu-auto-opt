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

## BW20 真实备份恢复补验（2026-09-21）

本节补充后续实测，不覆盖上文 CPU 夹具测试的范围说明。
代码基线 `d3c9b00`；在同一项目 PostgreSQL 容器中，从源库 `hcuopt`
执行 `pg_dump -Fc`，恢复到全新隔离数据库，再用单事务执行迁移 27–32。
没有把任何服务或 Worker 接到验收库，没有升级源库或启动 HCU。

| 检查 | 实测结果 |
| --- | --- |
| 私有备份文件 | `/home/github/hcuopt-formal-backup.SgdR2ATt/source.dump`（BW20 主机） |
| 备份大小 | 886959 bytes |
| SHA256 | `0b23ab8061fc2248e297cb37d6a4ca0e1995c503395c4fb7510613823727da99` |
| 隔离验收库 | `hcuopt_formal_restore_20260921091500_479182` |
| 恢复与迁移 | `pg_restore --exit-on-error --single-transaction` 成功；迁移后版本 32 |
| 历史内容 | 52 张旧业务表，升级前后行数和规范化内容摘要一致 |
| 旧 Jobs | 78 条；全部为新增默认通道 `general` |
| 新增表 | 8 张派发、领取、停止、阶段和构建相关表均为零行 |
| 源库复查 | 仍为版本 26 |

比对方法：在恢复库升级前后，对各旧业务表的每行 `to_jsonb` 文本计算 MD5，
按行摘要排序聚合后再计算摘要，同时核对行数；Job 比较仅排除新增
`execution_lane`，迁移台账单独核验。这里 MD5 用于非对抗性数据变化检查，
不是签名或安全完整性证明；备份文件另用 SHA256 标识。
恢复库比对验证的是同一备份快照内升级前后的内容，不声称在线源库在此期间没有新增写入。

第一次摘要比对退出 1：清单遗漏了 3 张新增空事件表
（`formal_dispatch_claim_events`、`formal_phase_journal_events`、
`formal_round_dispatch_events`），并非旧记录变化。保留原始前后摘要，
修正清单后重新查询已升级验收库，52 张旧表全部一致，没有重复迁移或恢复。
私有目录保留 `before.txt`、`after.txt`、`after-corrected.txt` 和 `migration.log`。

备份目录权限 0700、文件权限 0600，原始业务数据未下载到本机或提交 Git。
备份与源库仍在同一主机，**不是异机容灾备份**；数据库数据目录为 tmpfs，
隔离验收库也不是持久部署成果。此次没有验证线上 DDL 锁等待、服务兼容性、
真实 Authority 配置或 HCU 正确性执行；后续部署评审与受控联验仍需完成。

## 可重复执行的结构检查入口

新增 `python -m hcuopt.deployment.formal_schema_check`，用只读快照检查当前 schema
的迁移序列、8 张新表及 Job 通道列，输出不包含业务行或凭据。
详见 `docs/formal-schema-check.md`；它不是部署授权或完整 DDL 完整性证明。
本地单元 3 passed，Ruff 通过；Linux/Python 3.10/PostgreSQL 隔离回归
27 passed（36.35 秒），覆盖升级前拒绝、升级后结构通过以及迁移故障回滚。
本次测试包包含本地未提交的检查器代码，基于 `d3c9b00`，不是该提交本身的原样回归。

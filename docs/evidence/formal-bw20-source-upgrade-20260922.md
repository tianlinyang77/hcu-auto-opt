# BW20 源库升级记录（2026-09-22）

## 简要结论

用户在核对部署计划后指示“按照你的执行吧”。本次执行数据库结构升级，未开启 Formal 服务或 Worker，未生成签名身份、批准执行窗口、签署结果或启动 HCU。BW20 源库已从 26 升至 32，52 张旧业务表的内容摘要不变，78 条旧任务仍为 general 通道。新执行通道缺少真实部署授权配置，不能声称整体实机验收完成。

## 执行与验证

- 主机 `github-bw20` / `10.17.1.20`，容器 `hcuopt-bw20-stage0-db-feb58e54`，源数据库 `hcuopt`。
- 使用提交 `2adf20cc47bfcb34fa5a3140f932537e1640c5d6` 的迁移 27–32；此次没有更改迁移 SQL。
- 升级前检查无其他数据库客户端；重新 pg_dump，恢复到全新隔离库 `hcuopt_upgrade_backup_20260922031328_1334574`，先在恢复库应用迁移并比对旧数据。
- 源库单事务检查数据库身份与完整旧版本数，拒绝其他客户端；按表名顺序 NOWAIT 获取所有 public 表排他锁，lock_timeout=2s、单条 statement_timeout=30s。无杀会话、停止容器或强制等待。
- 事务内先比对源库与新备份恢复库的旧内容，再执行六项迁移，再次核对旧内容、版本和 Job 通道，全部符合才 COMMIT。失败应事务回滚，不盲目恢复旧库覆盖数据。
- 事务后独立只读核对：迁移版本数 32、最大版本 32，Formal 新表存在，jobs.execution_lane 存在，78 条 Job 全部 general，Formal Intent 仍为 0。
- 52 张旧表逐表行数与规范化行内容 MD5 聚合摘要一致；比较排除 jobs 新增 execution_lane 和迁移台账。MD5 仅用于非对抗性数据变化检查；备份及部署包另用 SHA256。

## 保留证据与恢复边界

主机私有目录 `/home/github/hcuopt-formal-upgrade.wj3yBeYQ`：

| 文件 | SHA256 / 内容 |
| --- | --- |
| `source.dump` | `8f886c8027dc61161a664714a3d6d6c63ea64fe65f05f619b0b96e3a3016353c`，886959 bytes，已实际恢复并升级验证 |
| `upgraded.dump` | `aaea4d97cbc10f0217b914690698b8631c55fc36636507ef90d882eda29a293e`，升级后的新备份，pg_restore --list 可读，未另外完整恢复 |
| `expected.csv` / `restored-after.csv` / `source-after.csv` | 三份历史内容摘要一致 |
| `rehearsal.log` / `source-migration.log` | 演练与源库迁移日志，源库日志终止于 COMMIT |

备份权限 0600，目录由 umask 077 创建。备份未下载本机、未提交 Git；旧备份与演练库未删除。
数据库数据目录仍为 `/var/lib/postgresql/data` tmpfs（512 MiB）。本次不重建容器或调整存储；
主机磁盘备份不是异机容灾。升级后若已有新增写入，不可直接用旧备份覆盖源库；应恢复到隔离库后评估切换。

## 代码落盘与未启用项

冻结代码包位于 `/home/github/hcuopt-formal-deployment.iDlSDwDg/source.tar.gz`，包含上述提交的 src/config/pyproject.toml，解压在同目录 code/。
SHA256 `5e0505f8357cf934eddc232c370e695ada36c30ee708d97f7e358190cad94a7c`，本地与远端一致。
这是待配置的源码包，不是已安装依赖或已运行服务的镜像；没有切换旧服务、启动后台 Worker 或暴露网络端口。

限定检查 `/home/github/hcu-auto-opt-runtime`（深度 5，排除源码/测试/文档目录）的常规命名配置文件后，未发现 trust/capabilities/compiler/service-config JSON。
本机已知私有目录只发现旧 endpoint 凭据文件名和模型凭据文件名，未读取这些秘密内容，也未拿它们当 Formal 身份。
此检查不是整个主机不存在授权材料的证明；若材料位于其他位置，应由管理员指定。

仍需落实：

1. 管理员提供或明确授权建立 owner、actor、execution、evaluation 四套独立签名身份，并配置公钥信任；配置密钥不等于批准任何运行或结果。
2. 为真实候选与 BW20 当期条件提供签署的 Profile/窗口、B/D Authority、compiler 与访问能力配置；不能用 CPU fixture、旧机器授权或旧页面令牌顶替。
3. 完成 ADR-0024/0025 的启用评审，配置服务与实际执行适配器，再执行正确性、性能与 D 结果验收。本次没有将 ADR Proposed 改成 Accepted；PR #168/#169 仍 OPEN、无 review。
4. 单独解决源库持久存储与异机备份，再将服务称为可长期部署。

当前成果是实际源库升级和代码包准备，不是性能收益、服务已启用或 MVP 最终验收。

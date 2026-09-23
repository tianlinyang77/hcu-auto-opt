# MVP 网络恢复后的备份与 CPU 联验

验证版本：`a61755328c8ac4cacf1220fc31ef5295c88a1721`。

## BW20 数据备份与恢复

- SSH 返回主机：`github-bw20`；既有 API `/healthz` 返回 `status=ok`。
- 数据库：既有 `hcuopt-bw20-stage0-db-feb58e54`，原库未切换。
- 最新磁盘备份：`/home/github/hcuopt-mvp-backup.GHyBDfe5/source.dump`。
- SHA256：`2a856895e2400b5eb5fd3b2843c037a9c5a0e13f220956710f4441c0eaec5809`。
- 使用独立临时数据库恢复，逐表比较行数及排序后的行内容摘要。
- 全部 61 张 public 表一致；备份前后原库内容一致。
- 临时恢复库及容器内临时 dump 已清理，磁盘备份保留用于回退。
- **当前 PGDATA 仍是 tmpfs，不能将恢复成功宣称为持久化切换完成。**

## CPU / PostgreSQL 回归

- 地址 `10.17.1.2` 本次返回主机名 `github-nmz2`，不能沿用 nmz36 的硬件身份。
- 使用既有专用 `hcuopt-viewer-20260907`，Python 3.10.12。
- 使用专用 PostgreSQL 容器中新建的独立数据库，测试后删除。
- 旧集成测试包含 TRUNCATE，所以未把它们指向既有业务库或已有测试库。
- 验证 Search 选优、Formal Finalizer、来源拒绝逻辑，以及既有 Formal 权限/签署、
  Dispatch、Service、Phase Journal 和正确性交接的 PostgreSQL 回归。
- 结果：**80 passed，12 subtests passed**。两项依赖弃用警告，没有测试失败。
- 新来源与 Search 选优为单元覆盖；此轮不等于新 Campaign 的 PostgreSQL 全链验收。

## 未完成范围

没有执行 HCU 测量、没有额外模型请求、没有新候选签署、没有重跑历史 Campaign、
没有停止他人容器。Search producer、Formal Campaign/Worker 接线、双候选实机和数据库
持久化切换仍需完成。本记录仅解除连接与 CPU 回归阻塞。

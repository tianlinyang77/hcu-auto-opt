# ADR-0001：模块化单体控制面与独立 Worker

- 状态：Accepted
- 日期：2026-08-13

## 决策

采用单一 Python Monorepo、模块化单体控制面、PostgreSQL 持久化队列和三个权限隔离的 Worker 进程。MVP 不引入 Celery、Kafka、Kubernetes 或多仓库拆分。

## 原因

四人团队的主要风险是测量可信度、DCU 适配和跨模块 Contract，而不是服务伸缩。一个数据库同时保存任务与实验事务，更容易实现可恢复状态和证据链。

## 后果

- 模块必须通过 domain Contract 通信；
- Worker 未来可以独立部署，但当前共享代码包；
- PostgreSQL 是生产队列语义的一部分，SQLite 不能替代并发测试。


# Formal 数据库结构检查

部署环境已有 Python 3.10+ 和本项目依赖时执行：

```sh
python -m hcuopt.deployment.formal_schema_check
```

通过现有私有配置提供 `HCUOPT_DATABASE_URL`，不要将密码写入命令参数、截图或 Git。
工具在一个只读、可重复读事务中检查连接的 `current_schema()`：

- 迁移版本序列是否与当前代码一致（本版 1–32）；
- 8 张 Formal 新表是否存在且为普通表或分区表；
- `jobs.execution_lane` 是否为非空 text，默认值为 `general`。

只返回结构元数据，不返回业务行、DSN 或底层异常。连接超时为 5 秒，
单条 SQL 超时为 5 秒。不会调用迁移器、创建任务、启 Worker 或访问 HCU。

退出码：0 为上述结构检查通过；1 为存在缺项或版本不匹配；2 为配置或查询失败。
`execution_authorized` 和 `automatic_release_allowed` 始终为 false。

这不是完整 DDL 完整性证明：不核验所有函数、触发器、索引或约束的定义，
也不验证签名器、Authority、资源窗口、运行中旧服务兼容性或恢复证据。
不能据此跳过 ADR/部署评审或批准执行。未来版本数据库不自动兼容旧检查器。

回归将检查器接入版本 26 → 32 的真实 PostgreSQL 隔离 schema 升级测试：
升级前报告缺项，升级后结构通过，但执行许可仍为 false。

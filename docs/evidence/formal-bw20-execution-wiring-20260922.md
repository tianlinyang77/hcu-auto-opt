# Formal BW20 执行接线与当前边界

后续更新：用户授权推进后，源库已实际升级至 32，见 [源库升级记录](formal-bw20-source-upgrade-20260922.md)。下方版本 26 是本次接线时的历史核查，不再是最新部署状态；真实授权配置及实机整链验收仍未完成。

## 本次接线

- `FormalCorrectnessDriver.execute_bw20_prepared` 从已领取 Job 的持久化材料读取 Target，构建真实 BW20 producer 与独立 D 校验器，沿原 correctness journal 回写。不能输入任意 Target 或借用其他 driver 的租约。
- 该入口复用 allocator 的既有替换点与 oracle，不是任意热点的通用采集器；其他热点必须配套各自的 producer 与校验协议，不能改名冒用。
- Formal 不经过旧通用 Worker，因此显式补上原 Worker 的执行前设备守卫：实时租约 → BW20 空闲 auto 检查 → 再次检查租约 → 原采集器。租约失效或设备忙时不进入采集器。不改频率、不停止其他任务。
- `FormalDeploymentRuntime.phase_consumer` 将已有 B 执行适配器接入同一 claims、PostgreSQL 材料读取器及回执 journal。它不申请设备、不签发授权，执行回执也不等于 D 接受结论。

## 当日实机只读核对

通过 SSH 到 `github@10.17.1.20`，确认主机 `github-bw20`。对现有 PostgreSQL 执行 READ ONLY 事务：

- schema 最大版本仍为 **26**；Formal Intent 数 **0**，Search Round 无记录。
- `formal_round_dispatches`、`formal_dispatch_claims`、`formal_dispatch_stop_requests`、`formal_build_journal`、`formal_phase_journal` 均不存在；jobs 无 execution_lane。
- 历史 manual_correctness 成功 2 条、manual_performance 成功 1 条，是旧流程记录，不是本次 Formal 验收。

因此本次没有启动 HCU、迁移源数据库或生成生产签名。当前仍需部署审定的数据库升级与真实 Formal 配置/授权，才能执行本次新链路；不能把已通过的隔离恢复演练当作源库已升级。

## 验证范围

新增 CPU 测试覆盖缺少回调、租约过期、设备忙、遥测期间过期、正常委托；覆盖禁用/外来 journal 拒绝及持久化 Target 接线；覆盖性能消费者 claims/reader/receipt store 同源与禁用无执行。测试使用明确标注的夹具，不声称 HCU 正确性或性能验收。

本地定向测试 38 passed；远端 Linux/Python 3.10 的既有 PostgreSQL/运行时回归 133 passed（隔离测试 schema，上传工作树快照，未访问 HCU）。新增成功委托测试随后在本地验证；不能把远端回归描述成新 producer 的实机采集。Ruff 与 diff 检查通过。

尚未完成：新的 BW20 Formal 实机全链路，以及由该链路产生并被 D 接受的性能结果。单独接通 producer 或回执存储不等于这两项已完成。

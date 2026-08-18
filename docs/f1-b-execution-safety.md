# F1-B 远程执行与资源安全

## 定位

F1-B 只回答“一个类型化 `ExecutionRequest` 怎样在锁定容器中可靠执行”。它不定义
SGLang Workload、不判断 Baseline/No-op 是否等价，也不产生任何性能结论。

```text
ExecutionRequest
  -> Target / digest / mount / environment 校验
  -> 控制面 fencing heartbeat
  -> SSH 或目标机本地 Docker argv
  -> 独立受管容器
  -> stdout / stderr / identity / topology 证据
  -> fence + health check
  -> available 或 QUARANTINED
```

## Adapter

- `ContainerExecutionAdapter`：适合 Worker 与 Docker 位于同一目标机的部署方式。
- `SSHExecutionAdapter`：使用系统 OpenSSH 连接远端，再复用同一容器执行逻辑。它强制
  `BatchMode=yes` 和 `StrictHostKeyChecking=yes`，只支持密钥或 Agent 等非交互认证；
  密码不得写入仓库、配置或证据。
- `ContainerResourceCleaner`：只识别带 `io.hcuopt.*` 标签的受管容器，不会停止或删除
  机器上的其他容器。

两种执行方式都使用结构化 `argv`。本地进程固定 `shell=False`；OpenSSH 边界使用
`shlex.join` 对每个远端参数逐项引用，不接受调用方提供任意 Shell 命令字符串。

## 启动门禁

真实容器启动前必须全部满足：

1. `target_id` 与 Target Lock 一致；
2. 镜像引用与 Target Lock 的 immutable reference 完全一致；
3. `docker image inspect` 同时匹配 Image ID 和 Registry digest；
4. 挂载源不能位于 Target Lock 禁止的 `/data`；
5. `ExecutionRequest.environment` 不能携带 Token、密码、私钥等 Secret；
6. 独占请求必须带 `resource_id + fencing_token`；
7. HCU 7、NUMA 7、CPU 112-127 由 Target Lock 生成 Docker 参数，调用方不能覆盖。

受管容器固定带请求 ID、资源 ID 和 fencing token 标签，并使用 digest、`--pull=never`、
`--init`、`no-new-privileges`、CPU/NUMA 绑定以及 HCU 可见性变量。一次物理执行写入一个
新的 attempt 目录，重试不会覆盖历史 stdout/stderr。

## 超时、取消与 Fencing

- 超时和取消只会按确定性容器名删除本 Adapter 创建的容器。
- Worker 执行前和写回前各做一次控制面 heartbeat；周期 heartbeat 丢失时立即调用
  Executor cancel，然后做 fence 和健康检查。
- Adapter 内还维护进程级 token high-water mark，旧 token 在 Docker 启动前即失败；
  最终权威判断仍由 PostgreSQL 中的 claim/fencing token 完成。
- 迟到的 Worker 即使本地命令成功，也不能通过最终 heartbeat 和 complete 写回。

## 清理与隔离

Cleaner 只清理同一资源上 token 小于或等于当前代次的受管容器；更高 token 的容器
必须保留。健康检查同时要求：

- 没有当前资源的受管残留容器；
- `hy-smi` 对 Target Lock 指定设备检查成功；
- 本流程未修改时钟，因此不执行无依据的“恢复时钟”动作。

数据库不再把所有退出路径伪装成 `fake-resource-cleaner-v1 / healthy=true`：

- 有完整且健康的 fence/health 证据才回到 `available`；
- 缺证据、清理失败、健康失败、Worker 丢失或外部取消先进入 `quarantined`；
- `/v1/resources/{resource_id}/cleanup` 只接受当前 fencing token 的补充清理报告，健康后
  才重新开放调度。

## 验证边界

本地单元测试覆盖 digest、argv、拓扑参数、Secret 拒绝、超时、取消、旧 token、
新一代容器保护以及 Worker heartbeat 丢失。真实验收使用：

```bash
HCUOPT_RUN_TARGET_LOCK=1 \
HCUOPT_F1B_OUTPUT_DIR=/home/github/lyt/Asari/dcu-auto-opt/results/f1b-target-lock \
pytest -q tests/integration/test_f1b_target_lock.py
```

真实测试只执行 `python --version` 功能 Smoke；证据固定写
`performance_conclusion=not_measured`，不能当作性能收益。

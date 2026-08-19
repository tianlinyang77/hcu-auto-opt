# F1 真实 Walking Skeleton

## 目的

这条链证明 A/B/C/D 能在同一 Target Lock 下协作，不证明 Kernel 更快：

```text
Target Lock
  -> 固定 Commit + 干净 Baseline
  -> 隔离 No-op Worktree + 确定性源码归档
  -> Baseline 新容器运行 SGLang Smoke
  -> No-op 新容器挂载归档后运行同一 Smoke
  -> 严格比较文本、finish reason、token 数
  -> 双 request / 双 attempt / EvidenceBundle
  -> fence + health check
  -> AWAITING_SIGNOFF 或 REJECTED
```

No-op 归档只作为可追溯制品只读挂载，不改变 SGLang 行为。本阶段验证的是“真实框架能否
被安全串起来”，不是热补丁机制或性能收益。

## 统一 Profile

Profile 名固定为 `nmz36-framework-smoke-v1`，包含六项真实能力：

- C：`GitSourceManager`、`NoopBuilder`、`LocalArtifactStore`；
- B：`ContainerExecutionAdapter`、`ContainerResourceCleaner`；
- D：`SGLangSmokeEvaluator`；
- A：FastAPI、PostgreSQL、Workflow 和人工签核状态。

三个 Worker 进程使用相同 Profile，但只领取各自类型的 Job。示例命令均应在 nmz36 的
仓库 Worktree 中运行，并把输出目录放在 `/home/github` 下：

```bash
hcuopt db-migrate
hcuopt api --host 0.0.0.0 --port 8000

hcuopt worker --id f1-agent --type agent \
  --adapter-profile nmz36-framework-smoke-v1 \
  --target-lock config/targets/nmz36-sglang-0.5.12.yaml \
  --output-dir /home/github/hcu-auto-opt-results

hcuopt worker --id f1-build --type build \
  --adapter-profile nmz36-framework-smoke-v1 \
  --target-lock config/targets/nmz36-sglang-0.5.12.yaml \
  --output-dir /home/github/hcu-auto-opt-results

hcuopt worker --id f1-gpu --type gpu --resource-id hcu-7 \
  --adapter-profile nmz36-framework-smoke-v1 \
  --target-lock config/targets/nmz36-sglang-0.5.12.yaml \
  --output-dir /home/github/hcu-auto-opt-results
```

运行前仍须确认 HCU 7 没有被其他流程占用。该流程不做 Docker prune、不停止非
`io.hcuopt.*` 容器，也不修改设备时钟。

## 闸门边界

Target blocker 按作用域处理：镜像身份和磁盘可用性会阻止 Framework Smoke；设备安静
窗口和 Stage 0 测量状态只阻止后续计时、优化和发布。任何结果固定为
`performance_conclusion=not_measured`。

## D 线真机验收

项目 Owner 已临时放行 HCU 7 用于 F1 功能 Smoke；每次运行仍必须即时确认设备空闲。
显式 Target Lock 测试会顺序执行 Baseline/No-op 两个新容器，复核严格等价、证据哈希和
最终显存及受管容器清理状态：

```bash
HCUOPT_RUN_TARGET_LOCK=1 \
HCUOPT_F1D_OUTPUT_DIR=/home/github/hcu-auto-opt-results/f1d-acceptance \
PYTHONPATH=src \
python -m pytest -q tests/integration/test_f1d_target_lock.py
```

该测试不得与 Stage 0 或性能测量同时运行，也不得停止现有非受管容器。失败时保留
`HCUOPT_F1D_OUTPUT_DIR` 下已经产生的证据，并先检查 HCU 7 和受管容器清理状态。

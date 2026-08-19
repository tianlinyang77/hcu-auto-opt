# S0-C Profiler 与可逆 Overlay 能力探针

## 目标和边界

S0-C 实现 `profiler`（G0-P）和 `hotpatch`（G0-H）两类 `Stage0ProbeResult`。
它只报告当前 Target Lock 中实际观察到的能力，不产生性能提升结论，也不实现第二套计时器。
任何耗时采集都必须在 #18 合入后复用统一 `MeasurementHarness`。

真实探针统一使用 Adapter Profile `nmz36-stage0-v1`，原始证据以规范 JSON 原子写入：

```text
<worker-output>/stage0/<stage0_run_id>/profiler.json
<worker-output>/stage0/<stage0_run_id>/hotpatch.json
```

文件落盘后为只读，并将 `file://` URI 和 `sha256:<digest>` 写入
`Stage0ProbeResult`。同一路径只能幂等重放完全相同的内容，不能覆盖已有证据。

## G0-P：Profiler 能力

### 使用 profile-llm-torch skill

SGLang torch profiler 的捕获和分析遵守 `profile-llm-torch` skill 的 `triage` 工作流：

- 默认分别捕获 prefill 和 decode，不能使用混合的 `legacy` workload；
- 默认预热 10 步、采集 5 个 active step；
- 默认 prefill 为 `4090 -> 1`，decode 为 `1 -> 2048`；
- 已知真实 benchmark 分布时可以覆盖长度，但必须把选择写入原始证据；
- 分析工作目录必须保留 `terminal_commands.log`、`analysis_stdout.txt` 和
  `torch_profiler_analysis.md`；
- 优先使用 rank-local trace，不能把缺失的 shape、dtype 或源码位置人工补齐。

`ProfilerCapabilityProbe` 执行结构化 argv，不通过 shell 拼接命令。每个候选工具配置包括：

```yaml
name: profile-llm-torch
version_argv: [python, /opt/dcu-opt-skills/profile-llm-torch/scripts/analyze_llm_torch_profile.py, --help]
profile_argv:
  - python
  - /opt/dcu-opt-skills/profile-llm-torch/scripts/run_triage_report.py
  - --work-dir
  - /data/hcuopt/stage0/profiler-analysis
  - --input
  - /data/hcuopt/stage0/traces
  - --framework
  - sglang
  - --model
  - Qwen2.5-0.5B-Instruct
output_format: torch_trace
output_path: /data/hcuopt/stage0/traces/rank-0.trace.json.gz
timeout_seconds: 900
```

对于已有 trace，`profile_argv` 运行 skill 的报告入口，`output_path` 指向用于机器复核的
rank-local trace。也可以配置返回 JSON/JSONL/CSV 的 rocprof 工具。所有命令的退出码、
stdout、stderr、解析错误和真实字段都会进入原始证据。

能力分级规则：

| 结果 | 条件 |
|---|---|
| `FULL` | 实际记录包含 Kernel 名称、耗时、调用次数、shape、dtype、meta、Python 位置和 HIP 关联 |
| `DEGRADED` | 至少包含 Kernel 名称、耗时和调用次数，但详细定位字段不全 |
| `NONE` | 工具不可用、命令失败、格式无法解析，或核心三字段不全 |

## G0-H：可逆 Overlay

G0-H 接受 F1-C 产生的 Baseline Snapshot、独立 Candidate Worktree 和内容寻址 Artifact，
然后执行三个使用同一锁定镜像、租约和 fencing token 的独立容器请求：

```text
纯净 Baseline
  → Candidate Artifact 只读挂载到新容器
  → 纯净 Baseline 恢复验证
```

探针会强制检查：

- Candidate Worktree 与 Baseline 分离，并正确引用 Baseline Snapshot；
- Artifact 引用 Candidate Snapshot、SHA256 匹配且宿主文件不可写；
- Candidate Artifact 只出现在 Candidate 请求中，并且只读挂载一次；
- 三次执行都使用 Target Lock 中的 digest 镜像及当前租约/fencing token；
- Candidate 使用与 Baseline/Recovery 不同的 `HCUOPT_CANDIDATE_CACHE_DIR`；
- Candidate 返回匹配的 `activation_marker` 和 `loaded_artifact_hash`，证明实际加载；
- Candidate 正确性输出 Hash 与 Baseline 一致；
- Recovery 不再带 Candidate 标记，输出 Hash 与 Baseline 一致；
- 恢复后的 Baseline Source Hash 与探针前一致；
- 三次执行后的资源健康检查都通过。

只在进程或容器启动时加载成功时返回 `OVERLAY_ONLY`。只有提供额外的进程内替换证据时
才允许返回 `HOT_PATCH`。任一执行、加载、正确性、恢复或资源健康检查失败都返回 `NONE`。

当前范围不修改镜像内 `site-packages`，也不触碰 `_C.so`、Driver、DTK、系统
BLAS/RCCL。

## 控制面接入

创建 `Stage0Run` 时可在 `budget.runtime_probe` 中携带 G0-P/G0-H 配置；控制面会把这份配置
传给七类 Probe Job，S0-C Adapter 只消费其中的 `profiler` 和 `hotpatch`：

```json
{
  "budget": {
    "runtime_probe": {
      "profiler": {
        "profile_workload": "both",
        "warmup_steps": 10,
        "num_steps": 5,
        "triage_work_dir": "/data/hcuopt/stage0/profiler-analysis",
        "tool_candidates": []
      },
      "hotpatch": {
        "activation_mode": "startup_overlay"
      }
    }
  }
}
```

正式运行仍必须由控制面提供独占 Lease、resource ID、fencing token、Target Snapshot 和健康
cleanup evidence。S0-C 不绕过 #17 的 Barrier，也不根据客户端手填结论宣布通过。

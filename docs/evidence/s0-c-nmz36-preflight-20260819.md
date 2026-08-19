# S0-C nmz36 无设备前置检查（2026-08-19）

## 结论

本次检查只核对 Target Lock 镜像身份和 Profiler 软件入口，没有映射或访问 HCU，因此不是
G0-P Dry Run，更不是 Formal Stage 0 证据，不能解除 `stage0_not_measured`。

检查结果表明：锁定镜像身份匹配，镜像内存在 `torch.profiler` 和 SGLang profiler 源码入口；
未发现 `rocprofv3`、`rocprof` 或 `rocprof-compute` 可执行文件。因此 S0-C 应优先使用
`profile-llm-torch` skill 驱动 SGLang 的 torch profiler 采集与分析，并在获得 HCU 7
执行窗口后根据真实 trace 将 G0-P 判为 `FULL`、`DEGRADED` 或 `NONE`。

## 检查环境

- Host：`nmz36`（`10.17.1.2`）
- Target：`nmz36-sglang-0.5.12`
- 镜像 ID：`sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`
- Registry digest：`sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`
- 容器挂载：`/opt/hyhal:/opt/hyhal:ro`
- HCU 设备映射：无

## 观察结果

```text
Python/Torch: torch 2.10.0
torch.profiler: available
rocprofv3: not found
rocprof: not found
rocprof-compute: not found
SGLang: 0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1
```

锁定镜像中存在以下与 `profile-llm-torch` skill 的 SGLang source map 对应的文件：

```text
sglang/profiler.py
sglang/srt/managers/scheduler_profiler_mixin.py
sglang/srt/utils/profile_utils.py
sglang/srt/utils/profile_merger.py
```

## 约束和下一步

1. 无 `/opt/hyhal` 挂载时，导入 Torch 会因缺少 `librocm_smi64.so.2` 失败；所有后续容器必须保留 Target Lock 中的只读运行时挂载。
2. 无 HCU 设备时导入完整 SGLang 会触发设备初始化并报告没有 HIP GPU；这不表示锁定镜像故障。
3. 下一次实际捕获必须取得 HCU 7 的执行窗口，并将 prefill/decode 分开采集。
4. Dry Run 需要保存 rank-local trace、`terminal_commands.log`、`analysis_stdout.txt` 和 `torch_profiler_analysis.md`。
5. 只有机器复核确认 Kernel 名称、耗时、调用次数和详细定位字段后，才能给出 G0-P 能力等级。

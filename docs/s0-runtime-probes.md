# S0-C Profiler 与可逆 Overlay 能力探针

S0-C 负责回答两个问题：锁定环境能否产出可用于定位 Kernel 的真实 Profiler
记录（G0-P），以及一个 Python/Triton Candidate 能否在新容器中真实生效并在退出后
恢复 Baseline（G0-H）。它不实现第二套计时器，也不发布性能提升结论。

## 一个公开 Profile，两个内部实现

正式 Stage 0 的七类探针统一由 `nmz36-stage0-v2` Worker Profile 领取：

- `fingerprint`、`timer`、`noise`、`known_signal`、`null_signal` 由 S0-B
  Measurement Adapter 执行；
- `profiler`、`hotpatch` 由 S0-C Runtime Probe Adapter 执行。

`compose_nmz36_stage0_registry()` 建立这层路由，并要求七类探针绑定同一个 Target
Fingerprint。每条结果仍会记录实际执行它的内部 Adapter，因此统一 Profile 不会混淆
B、C 的职责和证据来源。

## 配置的信任边界

`RuntimeProbeProfile` 是部署方创建并绑定到 Worker 的冻结配置，包含固定 argv、环境变量、
只读挂载、工作目录、Profiler 工具和 G0-H 三阶段执行方案。它同时绑定：

- `profile = nmz36-stage0-v2`；
- Target ID；
- Target Lock 的 SHA256 Fingerprint。

创建 `Stage0Run` 的外部请求只能提交 `max_wall_seconds`、`max_samples` 等资源上限，不能在
`budget` 中注入命令、环境变量、挂载或完整 `ExecutionRequest`。Worker 也不会读取 Job 中的
`runtime_probe` 字段。控制面会把规范化后的 budget 下发到每个探针；Runtime Adapter 用
`max_wall_seconds` 约束本轮外部命令的总执行窗口，Measurement Adapter 可从同一 payload
消费 `max_samples`。这样可避免调用方借能力探针执行任意命令或伪造探针方案。

所有实际执行仍由 Worker 根据冻结 Profile 构造 `ExecutionRequest`，并强制使用 Target Lock
中的 digest 镜像。`hotpatch` 即使处于 Dry Run 也必须取得独占 Lease、Resource ID 和 fencing
token，因为它会真实启动三阶段容器并需要可验证清理；其余 Dry Run 探针可按需不申请租约。

## G0-P：Profiler 能力分级

Profiler 命令使用结构化 argv，不通过 shell 拼接。每个工具候选需声明版本命令、采集命令、
超时和一种机器可解析格式：JSON、JSONL、CSV 或 PyTorch trace。

每条有效 Kernel 记录必须独立包含：

- `kernel_name`；
- 数值型且有限、为正数的 `duration_value`；
- 明确的 `duration_unit`：`ns`、`us`、`ms` 或 `s`；
- 正整数 `call_count`；
- 来自工具输出的 `kernel_category = kernel`。

不能把不同记录中的字段拼成一条“完整记录”，也不能替工具补写 Kernel 类别、shape、dtype
或源码位置。能力按单条记录分级：

| 结果 | 条件 |
|---|---|
| `FULL` | 至少一条有效 Kernel 记录同时具有 shape、dtype、meta、Python 位置和 HIP 关联位置 |
| `DEGRADED` | 至少一条记录具有全部核心字段，但详细定位字段不完整 |
| `NONE` | 工具不可用、执行/解析失败，或不存在核心字段完整的真实 Kernel 记录 |

PyTorch trace 只接受真实 `kernel` 类别事件，并保留 trace 的微秒单位。使用
`profile-llm-torch` 时仍遵守 stage-separated 的 prefill/decode 采集流程，保留原始 trace、
命令日志和分析结果。host runner 模式会在执行前移除旧 trace，并只接受本次命令新建的普通
文件，同时记录 URI、SHA256 和字节数。容器 executor 的 PyTorch trace 必须同时声明
`output_host_uri`，并存在唯一的 Worker-owned 可写挂载，把容器内 `output_path` 的父目录
映射到 Host 输出目录；缺少显式导出契约会在执行前失败关闭。Formal G0-P 只接受 D 已注册的
`rocprof + rocprof-csv-v1` 或 `profile-llm-torch + torch-trace-v1`，不会把普通 JSON/JSONL
归一化摘要升级成原始 Profiler 证据。

## G0-H：真实 SGLang Python/Triton Overlay

G0-H 依次执行三个使用相同 Target Lock 的隔离容器：

```text
Baseline 固定正确性请求
  → Candidate Artifact 只读挂载到真实 SGLang 替换点并执行同一请求
  → 不挂载 Candidate，重新启动 Baseline 并执行同一请求
```

探针会检查：

- Baseline Snapshot 干净，Candidate 是它的独立 Worktree；
- Artifact 引用 Candidate Snapshot，文件 Hash 与 Manifest 一致且以只读方式挂载；
- 三次执行使用 digest 锁定镜像和同一 Lease/fencing token；
- Candidate 使用独立缓存目录；
- Candidate 模块由本次 SGLang 服务进程组实际导入，导入标记中的模块路径和 Hash 与挂载
  Artifact 一致；
- Candidate 与 Baseline 的固定正确性输出 Hash 一致；
- Recovery 的实现 Hash、输出 Hash 和 Baseline 源码 Hash 均恢复一致；
- 每阶段容器退出后资源健康检查通过。

`sglang_overlay_runner.py` 复用固定的 `sglang_smoke_runner.py` 请求，并输出
`hcuopt-overlay-result-v2` 机器证据。Candidate 模块必须在导入时写出
`hcuopt-sglang-overlay-import-v1` 标记，至少包含 `process_id`、`process_group_id`、
`module_file` 和 `module_sha256`。Formal 模式通过生命周期 wrapper 启动服务进程，原样保存
`/proc/<pid>/stat` 和 `waitpid` 状态；三阶段还分别输出规范化正确性结果和缓存 Namespace
文件。每个 Phase Profile 必须声明独立的 Worker-owned 证据目录、实际实现文件，以及唯一
可写证据挂载。Baseline/Recovery 的实现和缓存必须一致，Candidate 二者都必须不同。

只有上述 SGLang 路径全部通过时才返回 `OVERLAY_ONLY`。仓库中的通用文件挂载 runner 仅能
验证“只读挂载、Hash 和三阶段恢复机制”，其结果会标记
`generic_artifact_mount_passed=true`，但能力仍为 `NONE`，不能授权真实优化。

当前实现不声称支持进程内热替换，因此不会返回 `HOT_PATCH`；也不修改镜像内
`site-packages`，不触碰 `_C.so`、Driver、DTK 或系统 BLAS/RCCL。

## 证据发布

Dry Run 默认使用 Worker 本地内容寻址存储：

```text
<worker-output>/stage0/<stage0_run_id>/<probe_type>/sha256-<digest>.json
```

发布过程使用不覆盖的原子创建语义，可安全处理并发和幂等重放；URI、SHA256、执行上下文、
Adapter 来源、实际执行结果和清理证据都会保留。但本地文件所有者仍可能修改文件权限，
因此这种存储只用于 Dry Run，不被描述为正式不可变证据。

Formal S0-C 必须显式注入 `DeploymentContentAddressedEvidencePublisher`、Typed Telemetry 和
单调时钟。Publisher 绑定一个固定、由 D Reader 允许读取的可信根目录；普通 Worker 输出目录
和 chmod 只读文件不构成 Formal 权限。Profiler 会发布原始版本输出及原始 rocprof CSV/torch
trace；Hotpatch 会先发布 Candidate Artifact，再从这个精确 URI 只读挂载执行，并发布 Source、
Artifact Manifest、三份 `HotpatchPhaseManifestV2`、stdout、标准化输出、缓存、实现文件和进程
生命周期。任何引用缺失、Hash 不一致、Profile 未配置写出目录、工具/parser 不在注册组合中，
都会失败关闭。Producer 摘要仍不进入 D 的判决输入。

## 旧 Dry Run 记录

`docs/evidence/s0-c-nmz36-dry-run-20260820.md` 记录的是旧协议下的探索性运行。它证明了
Profiler 工具可运行和通用挂载链路可工作，但没有证明真实 SGLang 替换点，也不满足当前
`nmz36-stage0-v2` 的 Formal 证据要求，因此不能作为 G0-H `OVERLAY_ONLY` 验收结论。

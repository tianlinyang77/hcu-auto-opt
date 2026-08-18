# SGLang Framework Smoke v1

## 这条开发线解决什么问题

D 线是功能正确性的“裁判”。它不生成优化代码，也不负责 SSH、容器调度或 HCU
资源清理。它只回答一个问题：挂载 No-op Artifact 前后，SGLang 对同一个固定请求的
可观察输出是否严格一致。

一个完整的真实 Smoke 必须由控制面顺序创建两个独立执行：

```text
Baseline 新容器 -> 新 SGLang 进程 -> Ready -> Generate -> Stop
No-op    新容器 -> 新 SGLang 进程 -> Ready -> Generate -> Stop
                                                |
                                                v
                                规范化四个字段并严格比较
```

`platform-v1.2` 已通过 ADR-0004 增加 paired 结果：Baseline 和 No-op 分别拥有
`ExecutionRequest/ExecutionResult/ExecutionAttempt`，数据库以 `variant` 明确区分。
真实 Profile 不再使用旧的单执行结果；旧格式只保留给 Fake 控制流 Demo。

## 两层协议

- `sglang-smoke-v1`：D 线内部 workload、HTTP 请求、响应规范化和 Runner 证据协议。
- `framework-smoke-v1`：现有 A 线 `EvaluationRun` 和 `EvidenceBundle` 公共协议。

两者用途不同，不能用 D 的内部协议覆盖公共控制面协议。

## 固定 Workload

配置位于 `config/workloads/nmz36-sglang-smoke-v1.yaml`，首版固定为：

- 模型：`Qwen2.5-0.5B-Instruct`
- 服务地址：仅容器内 `127.0.0.1:30000`
- Prompt：`The capital of France is`
- `temperature=0.0`
- `max_new_tokens=8`
- `sampling_seed=0`
- 非流式响应

锁定的 SGLang commit 接受的参数名是 `sampling_seed`，不是 `seed`。`seed` 会作为未知
参数导致请求失败。`sampling_seed` 只有在 SGLang 确定性推理模式下才保证生效；首版
不启用尚未在 HCU 上验证的 `--enable-deterministic-inference`，所以最终门禁仍然是
Baseline/No-op 输出的严格比较，而不是假定 seed 能消除所有差异。

服务命令由 Runner 固定组装，不经过 shell：

```bash
python -m sglang.launch_server \
  --model-path /public/opendas/DL_DATA/llm-models/qwen2.5/Qwen2.5-0.5B-Instruct \
  --served-model-name hcuopt-smoke \
  --host 127.0.0.1 \
  --port 30000 \
  --tp-size 1
```

请求为：

```json
{
  "text": "The capital of France is",
  "sampling_params": {
    "temperature": 0.0,
    "max_new_tokens": 8,
    "sampling_seed": 0
  },
  "stream": false
}
```

## 单 variant Runner

Runner 仅使用 Python 3.10 标准库，以只读文件挂进锁定镜像：

```bash
python /opt/hcuopt/sglang_smoke_runner.py \
  --spec /work/input/spec.json \
  --evidence-dir /work/output
```

一次调用只运行 baseline 或 noop 中的一个版本：

1. 拒绝未知或类型错误的 spec 字段。
2. 使用 `shell=False` 启动一个新进程组。
3. 轮询 `/health_generate`；200 表示 Ready，503、连接拒绝和临时超时重试，其他
   HTTP 失败立即终止。
4. 只发送一次 `/generate`，响应最多读取 16 MiB。
5. 无论前面哪一步失败，都在 `finally` 中向整个 POSIX 进程组发送 TERM；宽限期后
   仍存在则发送 KILL。
6. 任何清理失败都会使本次 variant 失败。B 线仍负责容器级超时、取消、fencing、
   最终清理和资源健康检查。

Runner 显式关闭 HTTP 代理，避免访问 `127.0.0.1` 时被宿主代理变量劫持。环境证据只
记录 HCU 设备绑定等白名单变量，不会把 Token、密码等完整环境写入结果。

Runner 拒绝覆盖非空输出目录。物理重试必须写入新的 attempt 目录：

```text
/home/github/hcu-auto-opt/results/framework-smoke/
  <task-id>/
    <evaluation-run-id>/
      attempt-0001/
        baseline/
        noop/
        comparison.json
        evaluation-run.json
        evidence-bundle.json
        report.md
        sha256sums.json
```

## 规范化与通过条件

原始 SGLang 响应可以包含 request ID、output IDs、耗时、缓存和内部调度字段。D 只抽取：

```json
{
  "text": "...",
  "finish_reason_type": "...",
  "prompt_tokens": 0,
  "completion_tokens": 0
}
```

- 文本原样比较，不 trim，不做语义相似度。
- finish reason 类型严格一致。
- prompt/completion token 数必须是非负整数且严格一致；布尔值不当作整数。
- 缺字段、非法 UTF-8、非法 JSON、错误字段类型均判失败。
- request ID、时间戳、耗时、缓存等动态字段只保留在原始证据中，不参与比较。

最终 `EvaluationRun.passed` 必须同时满足：两次物理执行成功、规范化输出一致、证据
完整、清理健康。任一条件失败都不能通过。

## 证据文件

每个 variant 必须产生以下九个文件，包括失败路径：

```text
spec.json
environment.json
start.json
server.log
ready.jsonl
request.json
response.json
stop.json
result.json
```

`response.json` 是 envelope，会同时保存 HTTP 状态、Content-Type、受限长度的原始文本、
解析后的 JSON 和解析错误。因此非 200 或非法 JSON 也有可审计证据。结构化文件使用
同目录临时文件、`fsync` 和原子重命名完成。两边证据齐全后再生成比较、公共对象、
人类报告和覆盖全部文件的 SHA-256 清单；清单不计算自身。

`EvaluationRun` 固定为 `phase=correctness`、`measurement=null`。Evidence 必须绑定 Task、
Target、Baseline Epoch、Candidate、Artifact、两个 ExecutionAttempt ID，并同时包含 B
Executor 与 D Evaluator provenance。真实执行为 `synthetic=false`；Scripted/Fake 执行
必须为 `synthetic=true`。

报告固定包含：

> 本结果仅证明 Framework 功能与 Baseline/No-op 等价性，不构成性能结论。

D 证据不得发布 speedup、latency、throughput、置信区间等性能字段。

## D3 前置门禁

真实 nmz36/HCU7 闭环开始前必须全部满足：

- A 线公共契约能表达并持久化 baseline/noop 两个独立 execution 和 attempt。
- B 线提供真实锁定镜像 Executor、独立容器、超时/取消、fencing、日志 URI、清理和
  `QUARANTINED` 健康语义。
- C 线提供与 Candidate 绑定、哈希可验证的 No-op Artifact 及唯一挂载方式。
- Target Lock 的精确镜像 digest 已在 nmz36 可用，HCU7 已独占，Docker 存储问题已解决。
- A/B 将 task、job、evaluation、resource、fencing、attempt、output dir 和完整
  `TargetSpec` 作为类型化上下文传给 D，D 不从自由 payload 猜测。

在这些门禁完成前，D1/D2 的通过只能称为本地 Scripted 功能验证，不能称为 HCU 实机
验证，更不能称为性能优化成功。

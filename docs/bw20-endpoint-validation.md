# BW20 SGLang 端点验证

## 当前目标

本阶段回答一个比 Kernel 微基准更接近真实使用的问题：已签核的 Agent startup Overlay
Candidate 放进同一套 SGLang 服务后，请求链能否正确运行，以及端点性能是否存在可测影响。

它不会改写 M1 Kernel 结论，也不授权自动发布。allocator 微基准约 16.60% 的收益不能直接
写成 SGLang 端点收益。

## 只读引用的已签核输入

| 对象 | 固定身份 |
| --- | --- |
| Task | `73f6f07d-14ed-5614-a8e5-75e77c4356f0` |
| Candidate | `bbcdc4f0-0369-54d0-b4cd-57763203c272` |
| Baseline Epoch | `844a19f1-caa4-54fd-9232-32b036e2f60f` |
| Target Snapshot | `a8c8a876-5e0a-5dee-b759-de1b05054c63` |
| Candidate Source | `sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2` |
| Artifact | `4f81e3af-5a06-5684-ade0-51fc65bba9d2` / `sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a` |
| EvidenceBundle | `d1c3ca75-46de-5aab-92d3-4a0203f95f8b` / `sha256:efb0177b072557744db4cb169b3a5550094f1760bb14a347702020c56a7e0a0d` |
| Signoff | `e131588e-546c-5553-9095-0ce8fc5d38ea` |

控制面终态为 `Task=completed`、`Candidate=accepted`，并保持
`automatic_release_allowed=false`。

## 冻结工作负载

权威文件为 `config/workloads/bw20-sglang-endpoint-provisional-v1.yaml`。加载时按文件原始
字节计算 Workload Hash，并按 UTF-8 prompt 字节计算 Prompt Hash；文件中不得自报这两个
Hash。

- SGLang：`0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1`
- 镜像 Digest：`sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`
- 源码 Commit：`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`
- Qwen2.5-0.5B-Instruct，TP=1，FA3，page size 64
- 单节点、单 HCU、closed-loop concurrency=1
- Prompt：`The capital of France is`
- 期望 prompt/completion token：5 / 8
- temperature=0、sampling seed=0、ignore EOS=true、非流式
- CPU 64-79、NUMA 4、宿主频率策略保持 `auto`

上述 token 数已从 BW20 最近成功的真实 Framework Smoke 原始响应只读核对。该历史单次
响应只用于冻结请求，不作为本轮性能证据。

## 镜像能力探针

2026-09-16 在不映射 HCU、断网、自动删除的临时容器内按安装元数据和源码完成只读检查：

- `sglang.bench_serving` 和主入口存在；
- 支持 `--output-file`、`--max-concurrency`、`--warmup-requests`；
- 源码包含 stream、TTFT 和 TPOT 路径；
- 精确分发版本与冻结版本一致。

该镜像的 Triton 会在 `import sglang` 时查询 HCU 型号，所以 CPU 探针采用“不导入包”的
源码枚举。这个结果只证明能力存在，不证明真实服务已经运行。临时脚本和容器均已删除。

## 已实现的工程边界

- ADR-0019：端点 Run 独立于已签核 M1，禁止回写和自动发布。
- `EndpointValidationEvidence`：冻结签核引用、Workload、ABBA、新进程、缓存、逐请求
  Hash、生命周期、Overlay、清理和真实 Runner provenance。
- `sglang_endpoint_runner`：第一版非流式 acquisition runner；分开 warmup/正式请求，
  保存逐请求 request/response/sample，token、HTTP、解析或清理异常全部 fail closed。
- 当前 Runner 只产原始证据，`producer_verdict=None`；它不能自报更快。

## 下一步执行顺序

1. 为 Baseline/Candidate 容器补 exact mount、服务进程内 Overlay import attestation、空缓存
   Namespace 和资源恢复证明。
2. 接入新的 Endpoint Validation Run 持久化与独占 Lease/Fencing，不复用已完成 M1 Job。
3. 在 HCU 7 上先运行一个 `PROVISIONAL` 完整 ABBA 组。若请求、Overlay、token、进程或
   清理任一失败，停止，不扩大样本。
4. 探针通过后冻结正式预算，执行 Formal ABBA；D 独立重读后给出
   `faster/slower/inconclusive/invalid`。
5. 页面只展示端点 Run 自己的范围、原始证据和 D 结论，继续显示“自动发布关闭”。

## 2026-09-17 执行状态

首轮 8 组端点延迟 campaign 在完成 6 个组后，因第 7 组收尾资源安全守卫失败而按不重试
语义停止。失败组没有纳入统计，资源已独立恢复为 `available`。部分结果、证据 Hash、结论
边界和重新执行前的修复要求见 [BW20 SGLang 端点延迟采集：6/8 部分结果](bw20-endpoint-latency-partial-result.md)。

在保持原安全阈值的前提下增加“有界等待 + 连续两次空闲确认”后，新建的 v2 campaign 已
完成全部 8 组、32 次独立服务启动和 3,200 次 measured requests。结果为 `inconclusive`，
完整范围、统计量和证据 Hash 见 [BW20 SGLang 端点延迟采集：完整 8/8 结果](bw20-endpoint-latency-result.md)。

## 2026-09-20 正式裁决工程状态

ADR-0020 已冻结 D 的第一版输入和判定纪律。`hcuopt endpoint-adjudicate` 现在可以在证据
所在主机上，通过显式 `--allow-root` 白名单独立重读八个 Endpoint Run 的完整文件树与
Hash Manifest，验证 32 个进程/缓存身份、请求数量、token、Overlay 和清理证据，并按
ABBA group 而不是 request 作为独立单位输出 `faster/slower/inconclusive/invalid`。

该能力目前是正式 D 核心和离线入口；Campaign 的 PostgreSQL 持久化、自动 D Job、页面
展示和人工签核仍在 #157 范围内。因此既有 v2 汇总仍保持
`formal_d_adjudication=false`，不能仅因为代码入口已存在而追认成正式裁决。

ADR-0021 进一步增加了 Campaign 控制面：API 只有在八个 Run 均已成功并完成 Workflow
advance，且 Signed M1、Target、环境、Workload、Plan 与 Hash 全部相同时，才会冻结
Campaign 和完整 D request。绑定写入后由数据库 Trigger 保持不可变。自动 D Job、页面和
人工签核仍是下一步，Campaign 创建本身不会产生 verdict。

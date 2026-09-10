# BW20 单案例环境接入

当前交付范围、审查顺序与剩余四步统一见 [交付状态索引](bw20-delivery-status.md)。

本页延续原有 Target → Source/Artifact → 唯一 Harness → D 独立验真 → 人工签核链路。
新增目标不改变公共 Contract，不自动注册默认可运行的 Real Profile，也不复制旧机器的验收状态。

**最新状态（2026-09-10核对）：修复后的正常API、原Worker/Coordinator、PostgreSQL租约、
BW20执行器与原D联验已通过，新任务 awaiting_signoff。** 输出一致、清理健康、独立证据
核验通过，见 [正常API验收记录](bw20-api-acceptance-result.md)。显式单次Profile准入已实际执行，
默认Catalog没有改变。候选代码未激活、Stage0/性能/签署仍未完成。
现可用正式CLI只读复核历史证据，见 [一条命令复核](bw20-evidence-replay.md)。
此前的 [成对边界验收](bw20-paired-boundary-result.md) 是更早的隔离schema诊断，不能混为同一轮。

**此前单模型功能冒烟也已通过。** 固定 Qwen2.5-0.5B-Instruct
在物理 HCU7 上完成启动、生成健康检查和一次固定请求，返回 HTTP 200 与 8 个输出 token，
随后清理本次服务和容器。它不是 Baseline/No-op 成对验收、完整正确性验证或性能结论。
下面保留此前失败和修复过程，最终证据见末节。

## 已确认与未完成

2026-09-09 的人工授权环境探针已验证：固定 SGLang 0.5.12/Python 3.10 镜像在
github-bw20 上可以导入 PyTorch，单个元素的设备分配、同步和读取成功。它**没有**验证
SGLang 服务启动、模型加载、源码等价性、计时分辨率、噪声或任何加速收益。

| 项目 | 原 nmz36 | 新 BW20 |
| --- | --- | --- |
| 架构 | 原 Target 配置 gfx938 | 实测 gfx936 |
| 物理设备 | HCU7 | HCU7，PCI 0000:b1:00.0，renderD135 |
| NUMA / CPU | 7 / 112–127 | 4 / 64–79 |
| 驱动 | 原 Target 配置 6.4.0 | 实测 6.3.31-V1.5.1 |
| Stage 0 | 已有历史证据 | pending，不可继承 |

只映射 renderD135 的容器中，物理 HCU7 被枚举为逻辑 `cuda:0`。不能在此命名空间
继续设置 `HIP_VISIBLE_DEVICES=7`。探针在分配前核对可见设备数、PCI 地址和架构。

镜像 digest 为 `sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`，
Image ID 为 `sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`。
它包含之前从固定源码 Commit 现编的 SGLang 包，不是另换一个 latest 镜像。
宿主没有 DTK 安装，容器继续使用自己的 DTK；`/opt/hyhal` 按原部署规定只读挂载。
首次缺少此挂载导致 `librocm_smi64.so.2` 导入错误，补齐后通过。

## 如何查看配置和生成复跑命令

在安装本项目的 Python 环境中，从仓库根目录运行：

```bash
python -m hcuopt target-validate config/targets/bw20-sglang-0.5.12.yaml
python -m hcuopt.deployment.bw20_environment config/targets/bw20-sglang-0.5.12.yaml
python -m hcuopt.deployment.bw20_environment config/targets/bw20-sglang-0.5.12.yaml --format shell
```

后两条只打印计划或 POSIX Shell 命令，**不会执行 Docker、申请资源或写入验收状态**。
这是部署诊断工具，不是第二套测量 Harness。输出中 `executed=false`、
`stage0_accepted=false`、`production_profile_registered=false`。

执行打印出的命令前，在获准窗口内用 SSH 确认 hostname、Image ID、PCI/render 绑定和
设备占用均匹配；名称冲突时不要删除已有容器。只在目标主机执行，不在本机执行，且不要
直接 `eval` 未审核输出。原宿主 Python 3.6 不满足本项目要求，不向它安装依赖。

容器断网、只读根文件系统、不提权、无 Docker socket、只映射单卡及 HYHAL 只读目录，
CPU64–79/NUMA4，4 GiB 内存、128 PIDs；任务超时 120 秒，再给终止宽限 10 秒。
执行后复查精确容器名称、HCU7 显存和 KFD PIDs。该工具不停止任何其他容器或进程。

## 配置中的频率不是既成事实

`expected_sclk_mhz=1500`、`expected_mclk_mhz=1800`、`manual` 是从现场支持档位选出的
**待评审测量策略**，没有应用，也没有获得改频率授权。现场读到的是 auto / 600 / 1800 MHz。
`measurement_clock_policy_unapproved` 仍阻塞 Stage 0。环境探针不读这些字段来调整频率。
若策略变化，必须更新目标指纹；不得用空闲瞬时频率当作稳定测量环境。

## 后续按这一条线收口

已在 `/home/github/hcu-auto-opt-runtime/sglang-das` 准备独立的干净 detached checkout：
Commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，Git tree
`a883d7eb4b4667768c19f0c9b0456d41f52da91c`；无 submodule 条目或工作树改动。
`python/sglang/srt/mem_cache/allocator.py` SHA256 为
`ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27`。
这是源码准备证据，不是全树 canonical source hash、wheel 重建或运行包等价性结论。

1. 核对已准备源码与镜像内包/Overlay 的关系，冻结模型和单案例输入。
2. 接入新目标专属部署 Profile；共享已有控制面、Build、Harness 和 D，保留旧 nmz36 的
   硬绑定，不靠改标签借用旧 Adapter。
3. 核对 Worker 的单设备映射、取消/超时、资源恢复和预算闭环，获得当期范围内的执行授权。
4. 针对 gfx936 新目标重新采集 Stage 0，再运行固定案例、形成新 Run 的独立验证和页面结果。

源码与运行包等价性、工作负载、部署接线、时钟策略、测量窗口与 Stage 0 均保持对应 open blocker。
本文件与配置不能自动解除它们，也不能充当 owner Authorization、签名或人工接受。

探针日志曾出现 `/var/log/hylog` 不可写警告，计算正常结束；没有为隐藏警告扩大挂载权限。
结束时观测到短暂 KFD PIDs，其中一个映射 HCU0，另一个未给出设备；复查时均已退出，
无法确认归属。保留这个不确定性，不将瞬时空闲写成整机独占。

## 下一步：一次真实模型请求（不是成对验收）

已有模型位于 `/public/opendas/DL_DATA/llm-models/qwen2.5/Qwen2.5-0.5B-Instruct`。
本次只验证固定镜像能启动该模型并完成一次确定性请求，不是 M1 性能工作负载，
也不代表 Baseline/No-op 成对 Framework Smoke、Stage 0 或完整源码/运行包等价性通过。

本地生成待审核执行包（不会 SSH、不会启动 Docker）：

```bash
python -m hcuopt.deployment.bw20_model_smoke --output results/bw20-model-smoke-review
```

输出包括 `plan.json`、`run-reviewed.sh` 和 `input/`。已有输出目录会拒绝覆盖。
该工具复用原有 `sglang-smoke-v1` 工作负载，设置独立的 BW20 目标/工作负载 ID；
仍使用 `fa3`、page size 64、TP=1、8 个输出 token。不使用测试 stub 或替换 server argv。
`plan.json` 保存这次 Target 指纹与输入文件 Hash；这些是溯源信息，不是签名或权限证明。

执行前需要**新的容器范围授权**，原 120 秒单元素探针的权限不涵盖本次模型加载：

- 仍只使用物理 HCU7、CPU64–79/NUMA4，容器中使用逻辑设备 0；不改频率。
- 16 GiB 宿主内存上限且禁用额外 swap、512 PIDs、4 GiB 临时缓存、2 GiB shm。
  沿用原 workload 的 `mem_fraction_static=0.85`，启动时可能预留约 54 GiB HCU 显存，
  **不是**仅占用约 1 GiB 模型权重大小。16 GiB 是宿主内存上限，不是显存限制。
- 超时 480 秒，TERM 后最多 10 秒再 KILL；单文件写入上限 2 GiB
  （最初 256 MiB 阻塞 AITER，2026-09-09 获批提高，临时空间总量仍为 4 GiB）。
- 只读挂载 HYHAL、8 个固定模型文件、一个 allocator 源文件、验收脚本和 spec；
  模型目录内 README、LICENSE、下载器缓存和临时目录不挂载。
- 唯一可写宿主挂载为新建的
  `/home/github/hcu-auto-opt-runtime/bw20-model-smoke-20260909/output`。
- 容器断网、只读根、不提权、不发布端口、不接 Docker socket；只访问容器内的 localhost。
  `/tmp` 的 4 GiB tmpfs 显式允许 exec，供已冻结依赖的 JIT 动态库加载，仍保留
  nosuid/nodev；此项已于 2026-09-09 单独获批，不改变宿主挂载或其他容器。
  结束自动删除本容器，保留输出目录，不触碰别人进程。

获得授权后，先核对主机、Image ID、PCI/NUMA、源码 Commit、输入 Hash 和 HCU7 占用。
若有无法确认归属的设备进程，不启动；不将此前的空闲快照视为当前窗口证明。
将审核过的 `input/` 和脚本复制到计划指定的新远程目录，再运行 `run-reviewed.sh`。
名称或输出目录冲突时停止，不能删旧容器或旧证据来重跑。执行脚本内的身份检查
不能代替上述资源占用核对和授权。

容器内先重新核对全部模型文件 Hash、保留 wheel Hash、安装版本，以及
wheel/安装目录/固定源码的 allocator 文件一致性，再复用唯一的 SGLang smoke runner。
这些抽查不解除 `source_runtime_equivalence_unverified`，也不注册 Real Profile。

验收以 `output/model-smoke.json` 的结果为准：必须有非空回复、正的输出 token 数、
正常结束原因和成功的进程清理。`preflight.json` 仅代表运行前检查；只有它通过，不能报模型通过。
`output/baseline/` 保存原始请求、响应、server.log 和生命周期记录。
超时、证据缺失、残留进程均不得通过。结束后再次查精确容器名称和 HCU7 占用，
外部清理核对未完成前不要把容器内 `cleanup_succeeded` 当作机器已恢复的充分证明。

### 2026-09-09 实机执行结果：未通过

用户已批准上述模型冒烟容器范围，实际进行了启动尝试：

1. 首次因证据目录写权限失败：宿主目录由 github 用户创建，容器 root 在
   `cap-drop=ALL` 下不能绕过目录权限。已在创建的单一 output 目录增加 root ACL，
   未使用 chmod 777、未加回 capability。原空目录保留为 `output-permission-failure`。
2. ACL 修复后，8 个模型文件 Hash、wheel Hash、包版本、allocator 三方比对、单卡 PCI
   和 gfx936 校验均通过。
3. SGLang 启动失败：AITER 在导入时复制预编译 `.so` 和 `.o` 到 `/tmp/.aiter/jit`，
   触发 `fsize=268435456` 单文件上限，报 `Errno 27 File too large`。
   这是诊断容器限制与依赖行为不匹配，不是模型正确性或优化性能结论。
4. `request.attempted=false`，无生成回复；`cleanup_succeeded=true`、退出码 1，
   容器已经自动删除。HCU7 复查 0% / 2 MiB。

远端原始记录：`/home/github/hcu-auto-opt-runtime/bw20-model-smoke-20260909/output/`。
其中 `model-smoke.json` SHA256 为
`6ee9055d140cd3c58bea549301fe46cc87b9e210d33a701d9091ad9835a61ec7`，
`baseline/server.log` SHA256 为
`d159295233f6f82854bc5763e757602de2801b332acb39f2002f554ef774c551`。
本地取回的对应文件 Hash 一致。无 Stage 0、成对验收或发布状态变更。

最后一次外部检查发现新的 KFD PID 3558391，cgroup 归属另一个 Jenkins/vLLM 容器，
不是本次已删除的模型容器；当时未报告其 HCU 映射。没有停止该进程。
下次运行前需重新核对它与 HCU7 是否冲突。

用户随后批准仅将单文件上限从 256 MiB 增至 2 GiB，仍保留 4 GiB tmpfs、16 GiB
宿主内存和原设备/网络/时间边界。重试前原失败证据保留为
`output-aiter-256m-failure/`，不覆盖原结果。

重试容器中只读核对 AITER 依赖：`aiter/jit` 的 `du -sk` 为 2732924 KiB，
两处 `module_aiter_operator.so` 均为 574292112 字节。`get_user_jit_dir()` 源码确认：
安装目录不可写且未设 `AITER_JIT_DIR` 时会复制整个 jit 目录到用户缓存。
这些大小解释了为什么 256 MiB 单文件上限会阻塞；不是 HCU 内存不足。

### 2 GiB 上限重试：模型已加载，生成健康检查仍未通过

本次确实完成了 Qwen2ForCausalLM 权重加载、KV Cache 分配和 FA3 初始化，
不再出现 AITER 的单文件大小错误。但 `/health_generate` 尚未通过时，Scheduler
在 `write_req_to_token_pool_triton` 路径初始化 Triton HIP driver，加载
`/tmp/triton/.../hip_utils.cpython-310-x86_64-linux-gnu.so` 失败：
`failed to map segment from shared object`。

这是当前首要阻塞。日志与临时挂载 `noexec` 的表现相符，但本次未及时捕获容器
mount flags，**不能将其写成已证实的根因**。已补启动前 `statvfs` 挂载属性记录与
noexec 快速拒绝，26 项本地测试通过。该新增检查尚未在实机运行。
若获准临时 JIT 缓存 `exec` 重试，仍应保留 nosuid/nodev、断网、不提权、4 GiB
临时空间、2 GiB 单文件和原时间/设备/内存边界；该权限调整尚未获准。

另保留一条独立风险：graph capture 日志中 rotary embedding kernel 的 launch 参数
448 超过编译 launch bounds 256。日志随后继续运行，因此不能把它替代为本次退出根因；
后续正确性验证不能忽略它，也不应仅为启动成功直接关闭 graph 或换 attention backend。

最终 `request.attempted=false`、`cleanup_succeeded=true`、server exit -9；日志显示子进程
异常后触发 SIGQUIT，不能只按 -9 就判为 OOM。本容器已删除，HCU7 0% / 2 MiB。
本次 `model-smoke.json` SHA256：
`9a45099af5ea17c305e0cb029fa867bb77dcb9e9f207df118158dfe165c71d64`；
`baseline/server.log` SHA256：
`57ccf3ad485055ef471b658a5d0562faea5e4177ebad6b45616db20db9e9eb10`。
远端 output 与本地取回文件 Hash 一致。结果仍为失败，不解除任何验收闸门。

### 显式 JIT exec 重试：单模型功能冒烟通过

简要结论：2026-09-09，保持固定镜像、模型、FA3、TP=1 和单卡隔离，显式允许临时
JIT 缓存执行后，完成真实请求并清理。可以进入成对功能验收准备，但不能据此宣布
Agent/Apex 优化闭环完成，不能报告加速比，也不能解除 Target 的 Stage 0 等 blocker。

执行详情：

- 用户批准的实际脚本 SHA256：
  `a7539b8c6aec1b25f56a89d38d796d221008e6a41afdbc94ae1990a13e3508ff`。
  容器 ID 为 `5b61c67a945b479b9fae7f1250e18d69fa9314f8b1d166d2fbcaaecd0f5b6e9b`。
- 实测 `/tmp` 为 tmpfs，`statvfs_flags=4102`、`noexec=false`，Docker 记录
  `rw,exec,nosuid,nodev,size=4g`；none 网络、非 privileged、只读 root、16 GiB 内存、
  CPU64–79/NUMA4 均保持。旧失败尝试没有捕获 mount flags，不能追溯断言旧值；
  本次明确 exec 后不再出现 `failed to map segment` / Triton cache load error。
- 启动前 8 个模型文件、wheel、allocator 与 PCI/gfx936 校验均通过。
  预检脚本 SHA256 为 `22eeb2654fb181361e53c0ffcbe63ce6b187cc7944fed3e7bb344b752c4c78a0`。
- 输入：`The capital of France is`。原始输出（含前导空格）：
  ` Paris. It is the largest city in`。HTTP 200，prompt tokens=5、completion tokens=8、
  finish_reason=`length`，符合预设的 8-token 上限，不是异常截断。
  SGLang 自身的启动预热和生成健康检查另有内部请求，不能把日志中所有 `/generate`
  都算作用户负载；本 runner 只发出一次记录在 request.json 中的固定验收请求。
- `model-smoke.status=passed`，`smoke.status=succeeded`，`cleanup_succeeded=true`，
  runner/container 退出码 0；stop.json 记录主动 TERM 后服务退出 -9，日志显示其子进程
  退出触发内部 SIGQUIT 清理，不应误报为服务自然退出 0 或单凭 -9 断言 OOM。
- 容器已自动删除，HCU7 复查利用率 0%、显存 2 MiB；仍存在外部容器的 KFD 进程，
  没有干预它。固定源码 checkout 保持干净。前三次失败证据和旧输入均保留。

原始证据保存在远端
`/home/github/hcu-auto-opt-runtime/bw20-model-smoke-20260909/output/`，本地取回至
`results/bw20-model-smoke-jit-exec-20260909/output/`。关键文件 SHA256（两端一致）：

| 文件 | SHA256 |
| --- | --- |
| model-smoke.json | `0b20ee226388ae3b570d2d077b7dcc5fe896db155c2f6c6b6b88f32ec1256342` |
| baseline/response.json | `b5b1f59666d276540dfd0742a68b048a34f721bfd0967c8eef5cfebd3c66d737` |
| baseline/server.log | `22f91c63c739377097746593cc900e2944b35d4c7829403df7973aa05bb2a193` |

复核覆盖了原始回复与归一化字段一致性、全部 runner 证据文件存在、清理记录和禁止
自动验收/发布字段；这只是本次功能探针的人工复核，不冒充平台 D 的正式裁决。
相关本地回归 85 passed / 4 平台相关 skipped，Ruff 通过。

剩余风险：rotary embedding 的 launch 448 / bounds 256 警告在成功日志中仍存在。
本次仅证明该固定请求可返回，**没有**证明受影响 graph batch 的数值正确性。
后续先定位这条警告并做成对 Baseline/No-op 功能验证，再推进新目标的正式测量准备。
不通过关闭 graph、随意切换 attention backend 或继承旧机器验收状态来隐藏问题。

后续源码诊断、成对验证契约和执行器差距见 [下一步验证](bw20-next-validation.md)。

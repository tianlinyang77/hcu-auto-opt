# BW20：单请求通过之后的下一步

## 最新结果索引（2026-09-09）

后续实机进展已记录于 [成对执行边界验收](bw20-paired-boundary-result.md)：真实源码
制品、原Worker/Handler、PostgreSQL租约、BW20执行器与原D已完成两容器功能比较。
这不是正式API/Profile准入；后续仍按该报告的完整验收清单推进。
以下为此前方案与阶段记录，其中“未上传/未运行”等表述属于当时状态。

## 此前阶段记录

当前进展：用户已批准 rotary HCU 范围并允许同范围继续推进；48/48 数值用例通过，
清理完成，详见 `bw20-rotary-correctness-result.md`。launch-bound警告仍存在，
未解除任何正式门禁。下一步接真实制品与成对执行，不重复已完成的库清点/数值探针。

同日后续：真实 SourceManager/两次 NoopBuilder/Store/候选清理链已完成，详见
`bw20-source-build-result.md`，不再用本地小型Git夹具代替真实制品。
Python源码与wheel/安装目录的完整对照见 `bw20-python-source-parity.md`；
确认存在已记录的get_rope_config导入修复，仍须显式绑定运行时来源。

2026-09-09。本页承接已通过的单模型功能冒烟，不扩大旧授权，不改变 Stage 0、D、
人工签核或发布规则。Skills 只帮助诊断和设计 reference，不提供验收结论。

## 当前完成的无 HCU 工作

- 固定源码的 rotary 调用链、线程计算与成功日志已对照。
- 新增 `config/workloads/bw20-sglang-smoke-v1.yaml`：独立目标/工作负载 ID，
  保持相同 Qwen 模型、FA3、TP=1、page64、确定性输入与 8-token 输出上限。
- 通过原 `SmokeVariantSpec`、`ArtifactManifest`、`build_execution_request()`
  生成基线/No-op 结构化请求的契约测试：不同 request ID、不同证据目录、相同镜像与
  请求参数，No-op 唯一多出只读制品挂载；旧 nmz36 workload 不能混用。
- 新增只读日志分析入口。它只标注警告和源码假设，不是 D，不会清除 blocker：

```bash
python -m hcuopt.deployment.rotary_launch_triage --log /path/server.log --model-config /path/config.json
```

测试中的 ArtifactManifest 是显式夹具，不能当真实 Build/Artifact 证据。

## Rotary 警告：已证实什么

依据冻结 SGLang commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`：

1. `python/sglang/srt/models/qwen2.py`：head_dim 默认 hidden_size / num_heads；
   `get_rope()` 的 rotary_dim 使用 head_dim。
2. `python/sglang/srt/layers/rotary_embedding/base.py`：HIP fallback 分支从
   `sgl_kernel` 导入 rotary_embedding。运行时是否经过该分支或被镜像插件改写，
   仍需确认，不能仅凭源码认定实际二进制归属。
3. `sgl-kernel/csrc/elementwise/pos_enc.cu:176` 的 block 计算为
   `min(num_heads * rot_dim / 2, 512)`。实际模型 config 为 hidden_size=896、
   attention heads=14；TP=1/full rotary 下 head_dim=64、block.x=448。
4. 成功日志中的 warning 也报告 `(448,1,1)` 与 compiled bound=256。

**448 是每个 block 的线程数，不是 batch size，也不是 HCU 核心数量。**
日志出现于 graph capture 的 bs=256 阶段，并不意味着只有 batch256 才受影响。
源码内部按 `i += blockDim.x` 循环，缩小 block 是一个可研究的修复方向，但尚未验证。

更新：r2 已取得实际 `sglang-kernel` 版本及 common_ops 共享库 Hash，详见
`bw20-cpu-inventory-result.md`。仍未证实插件重定向、编译参数，以及 256 上限
来自编译器默认值还是显式配置。当前仓库的 SGLang wheel
溯源不等于底层 sgl-kernel wheel 溯源。不能把这条问题标成“已修复”。

## 不先重编，先补二进制身份与最小正确性验证

下一实验分两步：

1. 无 HCU 分配的镜像内只读清点：sgl-kernel 版本、模块路径、共享库 Hash、
   构建元数据/可用源码与符号。先判明实际库归属；缺少构建来源就记录缺口。
2. 获批设备范围内，使用真正加载的算子与独立 FP32 数学 reference 对比。
   覆盖 Q heads14/KV heads2/head_dim64、BF16，token 数至少 1/8/248/256，
   多个位置与非平凡输入；分别检查 eager、graph capture/replay、Q/K in-place 写入，
   比较前冻结容差。抓取实际 kernel function attributes 和输出误差，不只看 warning。

若证实需要重编共享库，它属于 runtime/vendor 修复，不是让 Agent 越过 MVP 边界
自动替换 `_C.so`。应建立独立的、固定来源的运行时镜像修复，保留旧镜像回退；
获准并通过数值门禁后再更新 Target。不能改原 baseline 或悄悄全局替换系统包。

## 成对框架验证还差哪几条接线

| 项目 | 现状 | BW20 所需处理 |
| --- | --- | --- |
| 请求/制品/证据契约 | 可复用，已有新目标契约测试 | 使用真实 No-op Builder/Store 的制品，不使用测试 Hash |
| 执行器设备映射 | 已新增未注册的 BW20 窄策略，本地 fake-runner 回归通过 | 只映射 renderD135，容器逻辑编号0；通用 nmz36 行为不变；仍需真实 staging 接线 |
| 容器隔离与缓存 | 新策略带入固定隔离参数与8文件挂载白名单 | 断网、只读 root、cap-drop、CPU/NUMA、内存/超时、JIT exec；新策略本身尚未实机运行 |
| 身份与清理 | 单请求已有人工核对 | 在真实执行入口绑定 PCI、镜像与文件 Hash；每个 variant 执行后验证资源恢复 |
| 制品生效语义 | F1 No-op tar 只读挂载，runner 不解包或导入它 | 只称“挂载制品下的成对功能一致性”；真实候选需独立的安装/加载身份与回退证据 |
| 正式放行 | Target blocker 仍 open，未注册 BW20 Real Profile | 不清空 blocker 来使流程通过；接线、授权、正确性风险各自有证据后按原流程评审 |

这次没有新建容器、没有运行成对 HCU 测试、没有改频率、没有改模型或内核源码，
也没有新增自动放行路径。下一轮不能直接拿通用执行器执行这份配置。

## 已实现：独立执行策略（未注册、未实机验收）

`src/hcuopt/adapters/bw20_execution.py` 的 `BW20SmokeExecutionAdapter` 复用
原执行器的镜像检查、日志、超时/取消和 fencing，不复制另一套执行生命周期。
通用执行器只抽出资源参数与附加 metadata 两个扩展点，旧 nmz36 行为保持不变。

- 使用显式注入的 runner，无默认远程连接；Profile 名称带 `unregistered`，
  没有修改生产 Adapter Registry、Target blocker、Stage 0 或发布逻辑。
- 仅接受 BW20 固定镜像/主机/CPU/NUMA，独占租约资源 ID 为
  `bw20-sglang-0.5.12:hcu:7`。未来控制面须绑定同一 ID，不能传其他主机的 lease。
- 仅接受固定 runner argv；容器内 timeout 为 480 秒 + 10 秒 kill grace，
  外层执行器超时最多 480 秒（可更早触发已有 owned-container 清理）。
- 固定 16GiB 主机 RAM/无额外 swap、512 PID、4GiB exec tmpfs、2GiB shm、
  2GiB 单文件上限；保留原执行器 `--init`，不等同于之前人工执行的完全相同 argv。
- HIP/ROCR/HSA 均为逻辑编号0；任何额外环境变量或冲突值被拒绝。
- staging 目录格式必须为
  `/home/github/hcu-auto-opt-runtime/bw20-framework-smoke/<canonical-UUID>`。
  公共只读输入为 `input/sglang_smoke_runner.py`、`input/spec.json`；
  两次唯一可写目录分别为 `baseline`、`noop`。No-op 仅多出只读的
  `input/noop-source.tar`。模型只允许已冻结的8个单独文件，拒绝整个模型目录、
  重复挂载、缺文件、越界路径、可写模型/制品和其他额外挂载。
- metadata 分别写物理7与逻辑0，明确 `runtime_pci_verified_by_policy=false`、
  `production_profile_registered=false`、`automatic_release_allowed=false`。

**仍缺：**受信 staging 必须接原 Builder/Store，校验真实制品 Hash、工作负载内容、
runner Hash、远程文件 realpath/无逃逸链接、目录新鲜性和 output ACL；现场检查 PCI、
设备占用、租约有效性与执行后清理。当前白名单是路径/命令限制，不能证明路径里的内容
可信，也不能证明 PCI 物理绑定。因此新策略不能直接进入正式 Worker Profile。

原 `build_execution_request()` 的全模型目录挂载和 argv 仍是 nmz36 布局；
不能把它的输出直接塞给新策略。现在由 `bw20_pair_prepare` 显式转换8文件挂载与固定 argv，
保留原 `ArtifactManifest`/variant 绑定校验，没有放宽白名单。远程 staging 仍待接入。
No-op tar 仍只是挂载，没有候选代码生效的语义。

## 已准备：无 HCU 的镜像库清点入口

`src/hcuopt/deployment/kernel_binary_inventory.py` 仅用 Python 标准库读取
`sgl-kernel`、`sglang`、`aiter` 的包元数据及 RECORD 所列 `.so` 文件的 SHA256。
它不 import 上述包，不执行 kernel、不下载、不修改库，JSON 输出到 stdout。
缺包、缺 manifest、缺文件、逃逸路径会明确记录，不能填成来源已验证。

获批的无设备、断网、只读容器中可执行：

```bash
python3 -I -S /work/input/kernel_binary_inventory.py
```

最新进展：已获授权完成首轮 CPU 清点，SGLang/AITER 库指纹已取得；详见
`bw20-cpu-inventory-result.md`。`sgl-kernel` 名称未找到，固定源码实际声明
`sglang-kernel`；r2 已单独获准完成，包版本与共享库 Hash 已取得，下一步为数值验证。
更新：清点入口支持 `-S` 下通过 sysconfig 定位包元数据，不执行 site/.pth 启动钩子；
独立进程测试通过。已执行的两轮审批命令见 `bw20-cpu-inventory-review.md`。
输出只能证明安装清单中的文件身份；不证明当前进程加载了哪份库，也不证明源码/
编译参数对应关系。真实 dispatch、launch bound 和数值正确性仍分别待验。

本轮本地验证：相关单元/脚本集成测试 **140 passed / 4 平台跳过**，Ruff、
`git diff --check` 与新模块独立进程 import 检查通过。本机 Python3.12，
不能替代锁定镜像 Python3.10 的实际执行验收；没有启动新的 HCU 测试。

## 新增：可复验的成对准备包（本地，不执行）

`src/hcuopt/deployment/bw20_pair_prepare.py` 消费原有 Source Manager、NoopBuilder、
Artifact Store 产出的 `SourceSnapshot` / `ArtifactManifest`，不重建构建协议。

1. 校验候选 snapshot 的干净状态、repository/commit 与 Target 绑定；要求制品对应
   同一个 snapshot/source hash，具有 No-op recipe、candidate ID、非 synthetic 的
   builder/store provenance 和已发布标记。以上是受信控制面输入的一致性检查，
   不是把自报 metadata 当作签名、来源认证或远程源码已验证的证据。
2. 对原始制品和受信 runner 校验哈希，再复制到全新的本地 `input`；复制后复核
   源/目标哈希。拒绝符号链接或父路径重定向、不覆盖已有目录、不解包或执行 tar。
3. 调用原成对 variant 校验与请求生成器，显式转换为新策略的精确挂载和 argv。
   同一模型/输入/镜像，两个不同 request ID 与输出目录；No-op 多一个只读 tar。
4. 原制品 manifest 保留不动；另记 staged manifest，保留 artifact/candidate/snapshot ID
   与内容哈希，显式记录 `staged_from_uri`，不把本地 URI 偷改成远程已发布制品。
5. 最后才写 `plan.json` 完成标记：原/暂存 manifest、target fingerprint、请求、输入
   哈希、预期8文件模型哈希以及各项“未远程验证/未执行/未放行”状态。中途失败留下的
   部分目录不算可用准备包。输出的 plan SHA256 应由控制面保存在包外，复验时传入，
   不能从被修改的包重新算一个哈希便自称可信。

本地准备示例（命令只读指定来源并创建新的本地目录，不连接 BW20）：

```bash
python -m hcuopt.deployment.bw20_pair_prepare prepare \
  --target config/targets/bw20-sglang-0.5.12.yaml \
  --workload config/workloads/bw20-sglang-smoke-v1.yaml \
  --artifact /trusted/artifact-manifest.json \
  --snapshot /trusted/candidate-source-snapshot.json \
  --runner src/hcuopt/evaluation/sglang_smoke_runner.py \
  --runner-sha256 sha256:THE_REVIEWED_RUNNER_DIGEST \
  --fencing-token CURRENT_CONTROL_PLANE_TOKEN \
  --output /existing/local-parent/new-pair-bundle
```

路径与摘要占位符须换成真实受信输入；程序不帮忙生成假 snapshot/manifest/token。
准备工具须运行在能够读取该 Artifact Store `file://` URI 的 Python3.10+ 控制面环境；
远程 `/home/...` 的 URI 不会自动映射成本机 Windows 路径。本轮只在本地小型 Git 测试
仓库验证上述链路，没有在 BW20 构建新的 SGLang No-op 制品，也没有将准备包上传远程。
Windows 可用 PowerShell 单行调用或反引号续行。准备时的 token 不是未来执行授权，
实际执行前必须重新确认当前 lease；失效则重新准备，不手改冻结的请求。

可对传输后的本地副本复验（摘要来自包外的受信记录）：

```bash
python -m hcuopt.deployment.bw20_pair_prepare verify \
  --directory /existing/local-parent/new-pair-bundle \
  --plan-sha256 sha256:EXTERNALLY_RETAINED_PLAN_DIGEST
```

底层 `verify_prepared_pair(directory, expected_plan_sha256=...)` 执行如下检查：
检查 plan 摘要、精确输入清单、各文件哈希，以及 baseline/noop 输出目录仍为空且
没有重定向。它没有检查远程模型、output ACL、PCI/占用或容器，因此不能直接作为
Worker 的放行凭据。**下一步是远程准备校验和现场窗口检查，然后才是实际成对运行。**

Windows 测试边界：新增测试真实使用 GitSourceManager + NoopBuilder；因既有
LocalArtifactStore 首次原子发布在 Windows 删除只读临时硬链接时会报 WinError 5，
本机显式预置 CAS 文件，再走真实 Store 的校验/复用分支。Linux 测试不预置，覆盖
首次发布；Windows 无符号链接权限时只跳过独立的链接测试。不应把本机结果写成
Linux 首次原子发布或真实 SGLang 制品验收。

本次接线后的完整相关回归：**160 passed / 6 skipped**（含新增2项 Windows
符号链接权限跳过），Ruff、diff 空白检查、新模块独立进程导入和 Python3.10 语法
检查通过。实际测试解释器仍是本机 Python3.12；没有远程模型运行、上传、提交或推送。

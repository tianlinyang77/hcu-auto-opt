# BW20 真实 C 探针配置与候选准备

> 2026-09-14 更新：项目负责人确认所有机器按宿主`auto`模式验收，不做manual锁频。
> `hy-smi -c`只用于观察。下文保留的manual设置/恢复要求属于已经废止的历史方案。

2026-09-11。已完成真实源码、标记候选、只读制品、模型清单和七探针配置组合。
**尚未产生 Profiler trace，也未执行候选加载/恢复或 HCU 性能测量。**

最新进度见[统一部署入口与时钟事务边界](bw20-stage0-bootstrap.md)：API/Worker组合工厂和
只读检查CLI已实现，BW20真实输入检查通过但Target仍拒绝启动；时钟事务协调代码已有CPU测试，
驱动写入和实机恢复尚未接入。下文中的“部署入口待补”是本次之前的状态。

## 2026-09-11 当期镜像 Python 安装关系核验完成

本次在BW20的锁定镜像中重新执行原 `bw20_runtime_binding.py`，不是沿用nmz36或前几天的
验收。一次性CPU容器固定UID/GID1002、CPU64–65/NUMA4、4GiB内存及等额swap上限，
无网络、无HCU设备、只读根文件系统、仅源码/核验器两项只读挂载、cap-drop ALL；
`python3 -I -S`只运行标准库采集，不导入Torch/SGLang工作负载，不修改宿主频率。

结果：源仓库1974个Python文件 → 固定wheel1973个Python文件 → 镜像安装1973个Python
文件，安装包逐文件与wheel完全一致。源码与wheel的差异仅为原先明确声明的4项打包变换：
两个开发脚本未打入wheel、生成`_version.py`、固定Qwen导入修补；不能称为未经修改的全源码
逐字节相等。此核验仍不证明原生扩展的构建来源。

进一步将本轮采集与当前Target/RuntimeProbeProfile绑定：

- 镜像Digest、Image ID、SGLang版本、基线commit/tree匹配。
- `srt/layers/rotary_embedding/base.py` 的源码、wheel和已安装文件一致，Hash为
  `sha256:50b801ec65a3ab07eeb5fc159f462d509a84b9875d1a472f4ad01758dec3d8a8`。
- 当前Baseline、Recovery及Profiler命令的`--expected-hash`均匹配该文件。
- 这只是Python安装文件及候选替换点绑定，不等于该模块已在真实推理进程中加载，
  更不等于Candidate已实际生效；真正进程加载检查仍由原C入口及导入标记执行。

临时容器完整CID `48a833190edf09eac6061b2eb6cd30d43d31529db315ac37e74b0db455e054fc`
已自动删除，并经两次精确CID不存在回读确认；没有停止其他容器，未清理历史证据目录。
原本的operator采集脚本补充内存swap上限、配置核对与严格清理确认，未新增平行采集器。

证据目录：`results/bw20-runtime-binding-30b73362-e189-41af-9c27-4f0b51f2e79d/`。
其中`stage0-binding.json` Hash为
`sha256:f1d239762ef78008c3be45985b0a11dc7d4771cc29b5ee1fe7ea28d6b0d30673`；
它引用完整原始清单、容器inspect、执行范围和退出记录Hash。
本机原核验器+输入复核器回归36passed/2Windows符号链接权限skip。

本次没有修改Target blockers或注册常驻Worker，也未提交/推送。Python安装关系的当期
证据已备齐，下一步不需要重做本项或No-op联验，而是：

1. 将已核对的输入、当期运行时证据和七路Profile接到原API/Worker的明确部署入口，
   注入上一节的`BW20PreparedInputGuard`；不能使用通用CLI的fake fallback。
2. 形成经过审核的Target部署修订；原准备Profile绑定旧Target指纹，若Target修改，
   必须重新绑定并冻结相应配置，不能只改一个布尔值继续使用旧Hash。
3. 正式测量还缺独占窗口与1500/1800MHz manual设置/恢复授权。当前`BW20TimingCleaner`
   只检测时钟是否恢复，没有频率写入/恢复实现；应先实现并验证受控恢复，不能仅消除
   clock blocker就启动。旧源快照也不包含后续新增检查器，最终部署须冻结新版。
4. 原Worker真实租约 → 七探针 → 原D判决 → 真实Agent候选。优化结果页尚无新数据。

## 2026-09-11 后续：输入复核已在 BW20 通过

新增产品入口 `python -m hcuopt.deployment.bw20_runtime_verify`，以操作方独立留存的
`preparation.json` SHA256 为入口，复用原 Controller SourceGuard、GitSourceManager 和
Overlay 校验；不增加平行的 Target 判决或性能判决。

- 原230个控制端文件核验前后均一致；基线和候选 commit/tree/source/clean 状态一致。
- 候选源码中的实际替换文件与只读制品 Hash 一致；12个模型文件共999603354 bytes，
  逐个核对内容、大小和完整清单，没有只核对清单文件自身。
- 配置、Target、workload、SourceSnapshot 与 ArtifactManifest 的 Hash/身份绑定通过。
- 错误准备记录 Hash 在 BW20 真实 CLI 中返回2；未启动 Docker、未使用 HCU。
- 原准备目录和230文件控制端快照没有被覆盖。验证器及更新的C适配器在独立审核目录执行，
  其余依赖使用原已校验控制端；这不是新常驻 Worker 的完整部署。

执行入口 `BW20RuntimeProbeAdapter.run_probe` 现在必须有部署方注入的
`BW20PreparedInputGuard`（参数 `input_guard`）。配置可构造、七路可组合，但未绑定检查器
不能执行。检查在原 Target admission 和活租约检查之后、创建 Job 输出及容器之前执行；
模型哈希完成后再检查一次活租约，防止检查期间租约过期。Job diagnostics 保存本轮复核记录。
通过记录必须绑定当前 Target 指纹与配置 Hash，不能拿另一份准备记录代替。

```python
from hcuopt.deployment.bw20_runtime_verify import BW20PreparedInputGuard

# root/controller 均为部署侧固定的绝对路径，不读取 Job 提供的路径或批准字段。
input_guard = BW20PreparedInputGuard(
    root=prepared_root,
    controller=controller_root,
    preparation_sha256=independently_retained_preparation_hash,
    runner=bw20_local_runner,
)
# 通过 input_guard=input_guard 传入原 BW20RuntimeProbeAdapter。
```

只读独立检查命令（在 BW20 已安装依赖的 Python3.10 环境）：

```bash
python -m hcuopt.deployment.bw20_runtime_verify \
  --prepared <prepared-root> \
  --controller <pinned-controller-root> \
  --preparation-sha256 <independently-retained-sha256>
```

实际结果：Windows关联回归49passed/2symlink权限skip；BW20 Python3.10本轮验证器+C适配器
回归46passed（不同测试集，不能直接相加）；错误pin退出2、真实输入退出0。Ruff通过，两个新
入口的新进程导入确认来自本checkout。首轮Windows的lstat/fstat ctime语义差异导致失败，修复为
跨API比较inode/size/mtime、各自API内比较ctime后通过，未放宽Linux文件变化拒绝。

最终证据：`results/bw20-input-recheck-c43b7ad8-fa4e-4806-bea7-60fad4dd5fe0/report.json`，
SHA256 `37c9f443549a02efc19ad11bd65334f505d22792b19e1392b41b6d13849eb54d`。
先前481433b5复核也通过，但不包含后来增加的C入口强制检查，不作为最终代码覆盖证明。

**边界不变：**这是某一时刻的输入完整性复核，不是并发写入隔离、镜像中模块实际加载证明、
Profiler/Hotpatch结果或性能验收。没有修改Target blockers，没有常驻Worker注册，也没有签核/
提交/推送。原4194页面仍只显示之前已经签核的Framework Smoke，尚未展示本轮输入复核。
下一步仍须完成当期镜像运行时核验、正式部署绑定及测量窗口/频率策略确认，再由真实Worker
租约执行七探针。不能使用这份复核报告消除上述审批和实测要求。

## 当前准备结果

主机 `github-bw20`，当前准备目录：

```text
/home/github/hcu-auto-opt-runtime/bw20-stage0/4548d046-134a-4874-a3f5-9124bdb0c13f/
  controller/             新控制端源码（不是先前的228文件快照）
  inputs/                 输入Target/workload与本轮CPU测试
  runtime-prepared/
    runtime-profile.json  真实C配置，尚未注册到运行中的Worker
    baseline.json         干净基线快照
    candidate.json        独立、未提交的标记候选快照
    artifact.json         内容寻址只读制品
    model-inventory.json  12个模型文件，共999603354 bytes的SHA256清单
    preparation.json      输入绑定与准备范围
    source-logs/worktrees/ 独立候选Worktree
    store/                只读制品存储
```

| 输入 | SHA256（省略前缀） |
| --- | --- |
| Controller manifest | `c697a5d7cf6eacdd4e277610c8113f4898b711d7c09b9f93e0b62714958d7731` |
| C配置 | `92156939d3c13c64881827e17521a9b3b9d4914afa2a571a0dc6f45cb5ecb283` |
| 模型清单 | `92083963691b548a067f4012c4039201435a93a80c46e8a5343a03143ff23f53` |
| 候选制品 | `468945889752514177fc3a81f59f61360d924a2e6ecc4f3fbab1125dc7927f06` |

基线仍为 `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，source hash仍为
`sha256:215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`。
候选仅修改 `python/sglang/srt/layers/rotary_embedding/base.py`，追加导入观测函数，
source hash为 `sha256:3bacaa136006c8a46af5746d0ce67c7a60d134330bdf2a7595b7a8ed5b90e4d7`。

候选 `clean=false`，commit/tree字段仍描述基线，实际改动由source hash绑定；没有创建
伪装成上游提交的新commit。不能把它当成已finalize的M1或Agent优化候选。
原Overlay接受内容固定的独立候选；基线保持干净，制品经原LocalArtifactStore发布为只读。

## 两个 C 探针各做什么

### Profiler

- 使用原SGLang `bench_one_batch`真实模型入口，不制造张量夹具trace。
- Qwen2.5-0.5B-Instruct，TP=1，batch=1，input_len=128，output_len=8，仅采集prefill。
- 保留已有FA3、page_size=64、mem_fraction_static=0.85设置。
- 根据锁定的`bench_one_batch.py`，1次预热运行、1次采集运行；不沿用配置类中10/5的默认值。
- 预期输出 `/evidence/prefill.trace.json.gz`，每Job重新绑定独立输出目录。
- 采集技能仅用于核对采集入口与设备事件要求；原Profiler/D负责判级。本轮没有trace，
  所以尚不能声称捕获到kernel事件或FULL/DEGRADED能力。

### Hotpatch

Baseline → Candidate → Recovery 使用原SGLang smoke请求与原Overlay证据契约。
候选导入时记录自身文件SHA256、PID和进程组；原runner必须核对它确实由SGLang进程组导入。
标记不改变算子计算，没有声明加速。每阶段缓存位于各自新容器tmpfs，候选与基线隔离。

新 `bw20_capability_entrypoint` 在工作负载启动前核对真实模块路径/Hash，再核对容器内
逻辑0对应物理PCI `0000:b1:00.0`、gfx936。运行时路径来自已有镜像证据，仍须这次现场
检查通过，不直接把旧证据算作新候选生效证明。

C executor补齐 `--user=1002:1002` 与创建后User核验，避免容器产生控制端无法读取的root
私有证据。宿主现场确认github为UID/GID1002、kfd/renderD135设备权限666；没有修改权限。
镜像、单render设备、无网络、CPU/NUMA、内存限制、精确CID清理和活租约检查保持不变。

## 实际验证及保留失败

- 本轮新专项40项在Windows与BW20 Python3.10均通过，含真实配置→原请求契约→BW20
  executor的静态检查、导入标记、执行前Hash拒绝、容器UID拒绝和GitHub地址身份负向测试。
- 最终BW20扩大回归 **343 passed / 1 skipped**。包含原GitSourceManager的6项测试；
  唯一跳过为仅适用于非POSIX平台的拒绝用例。本机同组6项因WinError1314无法创建符号
  链接而失败，未通过提权或改测试掩盖；Linux结果与Windows失败分别记录。
- 用当前真实RuntimeProbeProfile、真实制品与部署admission实际组成B五路+C两路；
  没有构造假Worker租约，也没有调用探针执行。
- 原Target验证如预期拒收当前open blockers。组合成功不等于配置已注册/实机已通过。
- Ruff及diff空白检查通过；代码仍未提交/推送。

首次准备 `37fad13f-...` 因origin地址不一致失败，未创建候选。已修复原SourceManager：
仅识别同一个GitHub host/owner/repo的标准HTTPS/SSH克隆地址等价，不忽略其他host、owner、
仓库、URL凭据/端口/query。HEAD、干净状态和source hash检查不变，未改宿主仓库remote。
`0c22488c-...`是修正采集次数前的准备；保留为历史，不用作当前配置。旧目录均未删除。

本地证据：

- `results/bw20-runtime-preparation-4548d046-134a-4874-a3f5-9124bdb0c13f/report.json`
  SHA256：`7c67c84605614ec4f2a35dadd8937d042e78a5fceb2536e7020f16c22cd1fd3c`。
- `results/bw20-prepared-profile-check-d6a9f7f0-87db-4aa4-ae06-b00125ef6235/`：实际七路
  组合、Target拒收和完整回归日志。

## 下一步执行顺序

1. 独立复核当前输入Hash与模型文件清单，完成workload/部署绑定记录；冻结配置本身
   不会自动修改Target。模型目录是共享只读挂载，当前清单不是并发写入隔离机制。
2. 完成锁定镜像中运行时模块的本次核验，不能仅凭准备成功解除源码/运行时等价性闸门。
3. 确认HCU7正式测量窗口，以及manual 1500/1800MHz设置与原状态恢复范围。
4. 在原控制面取得真实租约后跑七探针，原D判决；不绕开闸门直接启动当前配置。
5. 通过后另接真实Agent优化候选，完成正确性、性能、恢复和页面展示。

当前Target未改，常驻Worker未启动，时钟未改，没有停止他人任务。C准备已完成；
七探针正式运行与Agent优化闭环仍未完成。

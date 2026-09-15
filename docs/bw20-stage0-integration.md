# BW20 Stage 0 接入切片

> 2026-09-14 口径修正：BW20及同类机器保持宿主默认`auto`，Stage0只通过`hy-smi`
> 与sysfs采集频率状态，不设置、锁定或恢复频率。下文历史切片中的manual锁频要求已废止。

## 2026-09-14 当前结论：v4 实机通过 G0-M，进入人工候选模式

BW20 物理 HCU 7 已在宿主 `Performance Level: auto` 下完成 `s0-g0-bw20-v4`
七探针正式验收。Run `dc883c86-9aa8-5550-a8ea-0094a9866519` 的七个 Job 均只执行
一次，分别取得连续 fencing token 34--40，Barrier 到达 `ready`，独立 D 判决无失败代码。

v4 没有放宽原闸门：CV 上限仍为 `2%`、MDE 上限仍为 `3%`、Hampel 阈值仍为 `3σ`、
最大异常率仍为 `5%`。本次只把夹具改为 64 MiB FP32 张量和 256 次批内迭代，并将
Event/host 校准改为同一测量进程内的间隔采集，从机制上降低小 kernel 发射、主机调度和
控制端 RPC 对统计量的污染。实测结果：

- G0-M `pass`：CV `0.0052246%`，MDE `0.0065460%`；
- Noise：300 个样本，2 个异常点，异常率 `0.6667%`；
- Null Signal：400 个样本，2 个异常点，异常率 `0.5%`，效应约 `-0.001516%`；
- Known Signal：400 个样本，正确识别约 `100.005%` 的已知慢化，无异常点；
- 四路校准的 `ns_per_tick` 为 `1.0000046`--`1.0009599`，不再出现 v3 的尺度漂移；
- G0-P `degraded`：当前 Profiler 只能支撑人工候选接入；
- G0-H `overlay_only`：候选必须用独立服务进程在启动时加载；
- 最终项目模式 `degraded_manual_intake`，`automatic_release_allowed=false`。

因此当前结论是：**BW20 的测量尺子已通过，可以进入真实 Agent 人工审核候选闭环；
尚未实现自动找热点、自动热更新或自动发布。** `source_runtime_equivalence_unverified`、
`measurement_clock_policy_unapproved` 和 `device_isolation_not_reserved` 仍作为本轮已接受的
Target 风险保留，结论只绑定本次 Target、Workload、协议和预算。

冻结 Deployment 根目录为
`/home/github/hcu-auto-opt-runtime/bw20-stage0/ba2d69ae-387a-44a4-9c9f-ee07a020c2be`，
Deployment SHA256 为
`sha256:91841c9bccbfb0a99cb755295cb01574ab10d48775cc3d5b3a6ea1b89a59a547`，
协议 SHA256 为
`sha256:7fadf7f7840ec42ecc6c47da33caeacf7a9aa278002f67a4361c3a11947efc48`。
完整原始证据保存在
`results/bw20-stage0-v4-acceptance-dc883c86-complete/`，归档 SHA256 为
`sha256:0a96c0eb63c62d868751210740d00e5f36119687a7668e5fd5dc02ba65ce0fff`；
操作回执 SHA256 为
`sha256:22d88d798e3f66d22a5850e063fbdace19354fed05081e6c292c3bd55f66e4d2`，
D 机器报告文件 SHA256 为
`sha256:a6f0833f1935f1f9b814c780a7f26eb30c946434e22fccd0ca8466f4b7f14dc9`。
验收后卡 7 利用率/显存均为 0%，无 KFD PID，受管测量容器已清理，性能级别仍为 `auto`；
保留的 `hcuopt-bw20-stage0-db-feb58e54` 是既有控制面数据库，不是 HCU 作业容器。

## 2026-09-14 历史：BW20 v2 工程修复与待采样状态

已注册 `s0-g0-bw20-v2`，没有修改 v1 文件或历史证据。新协议保持 CV `2%`、MDE
`3%`、ABBA、采样数和其余统计门槛不变，只修正两项不符合 BW20 fleet-auto 现实的机制：

- `required_performance_level: auto` 仍逐次强制；SCLK/MCLK 继续完整记录，但作为 DVFS
  诊断数据，不再要求 `auto` 负载中的动态 SCLK 恒定。
- Event ticks、Linux `CLOCK_MONOTONIC` 前后边界和分辨率样本由同一个测量子进程在一次
  原子命令内采集。控制器不再用 SSH/Docker JSON RPC 包围 Event 读取，因此 RPC 往返
  不会进入校准残差。

新协议 Canonical Hash 为
`sha256:7d7b1f533d75edb21647e87b3d96606112f76c80c9727243d85fd7f32e8cff54`。
`s0-g0-v1`、`s0-g0-v2` 和 `s0-g0-bw20-v1` 的既有 Hash 均保持不变。部署 bootstrap
已显式切到 BW20 v2；旧 v1 Run 的 `stopped_measurement` 结论仍有效，不能用本次代码
修改重解释。下一步必须冻结新 Controller/Target/Protocol 并重新跑七路真实探针，最终
仍由 D 按真实 CV/MDE 与校准残差判定。

## 2026-09-14 实机结论：环境链路完成，D 拒绝当前测量可信度

物理 HCU 7 在宿主 `auto` 下完成七路真实探针，Barrier 到达 `ready`。资源 ID
投影修复后，原 Run `2c97f540-ff57-54f7-bb72-01f2ebf84356` 已由独立 D 正式
finalize；没有重跑 HCU，也没有执行设频、锁频或频率恢复。

最终模式为 `stopped_measurement`，不是环境/容器没跑通，而是当前协议下的测量尺子
未过线：CV `3.2642% > 2%`，MDE `4.0897% > 3%`；四路设备计时校准残差过大，
并且 `observed_stable` 把 `auto` 负载下的 SCLK 变化判成漂移。Profiler 为
`degraded`。热补丁三阶段虽实际执行并恢复，但 D 的严格链路分类仍返回 `none`，须单独
定位，不得把 Job 成功等同于能力闸门通过。

正式报告与修复控制器回执保存在
`results/bw20-stage0-finalize-resource-fix-20260914/`。报告 Hash：
`sha256:c56fa83a052c9a1b336ff5aa4d158d21fec730fec28efe456d07c68542cd40d0`；
finalize 回执 Hash：
`sha256:ac104ca47bcdc378be738d4213b4fe445add9f4772e2a85497ee10860a13159f`。
收尾只读检查为无 KFD PID、HCU 7 `Performance Level: auto`。

下一版必须注册新协议，明确 fleet-auto 只校验 `Performance Level: auto`，不要求动态
SCLK 恒定；同时把设备 Event 校准采样移入测量子进程，消除控制器 RPC 往返对残差的
污染。修复后重新采样，CV/MDE 仍由 D 按阈值判定，不能人工改成通过。

2026-09-10。承接 Framework Smoke 人工签核完成后的第三步，不重复 No-op 功能运行。

## 2026-09-11 后续更新：真实 C 配置、候选与七路组合已准备

已在BW20从干净基线创建真实独立标记候选、只读制品、12文件模型Hash清单和三阶段配置，
并完成真实B/C组合及343通过/1条件跳过的Linux回归；没有执行HCU探针、改时钟或豁免Target。
当前目录/Hash/候选范围见[C运行准备记录](bw20-runtime-preparation.md)。
下文“C配置尚缺”属历史状态；目前缺的是正式部署准入和实机执行，不再是空的C配置。

## 2026-09-11 后续更新：同机 CPU 控制端已部署验证

已在BW20独立venv中完成 **394 passed / 1 skipped** 的Python3.10工程回归，API/Worker/
七探针模块加载、本机Docker只读访问和同机源码staging通过。补齐SQL打包资源与同机runner，
不用SSH登录自己；未启动常驻服务或HCU测量。部署目录、Hash、保留失败和下一步见
[CPU控制端部署记录](bw20-controller-cpu-deployment.md)。
下文“没有远端环境/部署”的描述为当时切片状态；真实C配置与七探针HCU实测仍未完成。

## 2026-09-11 最新：七探针工程组合与显式缓存协议

**本节为当前状态，下文按切片保留历史记录。工程接线已经补齐，不等于七探针实机通过。**
当前代码仍在本地工作树，旧的197文件远端快照不包含本节实现，不能用于本版本验收。

### 本次补齐

- C 使用 `BW20RuntimeProbeAdapter`，复用原 Profiler/Overlay 探针、证据发布器和判决路径。
  配置以独立 SHA256 冻结；每个 Job/lease/fence 使用新输出目录，baseline、candidate、
  recovery 三阶段隔离，复用原候选制品内容校验，不把 Windows 本地路径转交远端 Docker。
- `BW20CapabilityExecutionAdapter` 在创建后、启动前核验容器实际配置；仅映射 kfd 和
  renderD135，容器内三个可见设备变量均为逻辑0。固定镜像、CPUs64–79/NUMA4、无网络、
  private IPC/PID、只读根文件系统、cap-drop ALL，以及内存/PID/tmpfs 上限均有检查。
  当前未运行该 C 容器；实际依赖和 HYHAL 只读挂载仍须通过冻结配置明确提供。
- 创建前、启动前、运行轮询和结果交付使用原 Worker 活租约检查。取消/失租只清理本任务
  完整 CID；创建结果不明不报告资源已释放。阶段之间的 health 只观察，不关闭下一阶段。
- `compose_stage0_profile` 组合 B 五探针与 C 两探针，按探针分派同一 Job 的 cleanup。
  Profile 名为 `bw20-stage0-v1`，只允许 Stage0；B/C 必须使用相同部署 admission。
  不修改默认 Catalog，不豁免 Target blockers，不授予优化或发布权限。
- Source staging 现在同时冻结 Python 源码与包内协议 YAML；上传解包允许清单同步调整，
  其他 YAML 不会被带入。部署必须重新冻结 manifest，不能沿用旧快照的 Hash。

### 缓存口径：注册新版本，不改旧证据

新协议 `s0-g0-bw20-v1` 的 `cache_policy` 为 `allocator_reset_per_batch_v1`：
逐批执行 `synchronize → empty_cache → synchronize`，按原预热和采样方式测量。
**只证明分配器清理，不声称硬件 L2/HBM 冷缓存。** 原 D 继续核对逐样本旁证和操作顺序；
在此协议下接受 telemetry 的硬件缓存状态 unknown，不接受冒称 flushed/cleared 的状态。
新策略仅适用于 BW20；BW20环境改为`auto + observed_stable`，ABBA、采样和统计阈值保持不变。

| 协议 | Canonical SHA256 |
| --- | --- |
| `s0-g0-v1`（不变） | `6638210463ef4cd35c1f15b9f8d6aa8047a59e605b6e2d3cf08e8cce500c34e4` |
| `s0-g0-v2`（不变） | `a976fdd6299e83d9571cf5f3aad93fe9d282fa1772563967a351fb245cb09d64` |
| `s0-g0-bw20-v1`（auto修订） | `9cd83c975208ad164789bc084aab42aa82a4683d988afdbe910cbc492d95fb6a` |

### 部署入口及尚缺材料

以下为组合顺序，不是可直接放行实机的命令：

1. 依据当期证据完成 Target/workload/窗口审查；频率保持宿主`auto`，只读记录且禁止写入。
2. 在 BW20 主机准备项目隔离的 Python3.10 Controller 环境和全新 ControllerBundle。
   已只读确认 `/home/github/.local/bin/python3.10` 为3.10.20、PyYAML6.0.2可用；
   pydantic尚未安装。没有改全局依赖，主机默认 `python3` 仍为3.6.8。
3. 准备并冻结 BW20 的真实 `RuntimeProbeProfile`、命令/挂载、baseline/candidate制品
   及 SHA256。本轮测试使用的 C 夹具不能代替这些材料。Controller、证据路径与 Docker
   必须具有同机文件系统视图；runner 的 BW20 endpoint/访问方式也需现场验证。
4. 构造同一个 `BW20MeasurementAdmission`（显式选择新协议及对应 Hash），分别调用
   `compose_measurement_adapter(...)` 与 `BW20RuntimeProbeAdapter(...)`，再调用
   `compose_stage0_profile(measurement=..., runtime=..., admission=...)`。
5. 将返回的 Profile/Registry 显式接入原应用和 Worker，执行七探针、原 Barrier/D 判决，
   保存本轮证据；不另建队列或另写一套统计结论。
6. Stage0 实测通过后，才继续真实 Agent 候选的加载、正确性、性能、恢复与结果页闭环。

### 验证范围

C 专项 **22 passed**：原 C 的 Profiler 与三阶段 Overlay 证据链使用明确的硬件/执行器
替身；另测具体执行器的请求边界、创建后配置核验、失租清理/客户端回收、超时清理分支、
未知创建拒收、七路组合与清理归属。这不是22次真实容器/HCU运行。
旧协议 Hash 不变、BW20 回执不可绕过、源码包包含协议 YAML 均有回归测试。
最终扩大回归 **378 passed / 7 skipped / 1 warning**（83.66秒）：覆盖 BW20 Stage0、
session/transport、原 Harness、Worker live lease/heartbeat、原协议/统计/verifier、
原 RuntimeProbe 与 Router。7项为 Windows 缺少 POSIX 文件安全接口而跳过，
1项为既有 Starlette 弃用警告；不算 Linux 验收。Ruff、diff 空白检查、新进程导入与
27个 BW20 deployment 文件的 Python3.10 AST 检查通过；AST不等于3.10运行时测试。

本轮没有部署新 Profile、创建远端环境、启动 HCU 测量、修改频率或停止他人任务，
也没有新的 PostgreSQL 联验。**真实 C 配置/同机部署与七探针实测仍待完成。**

## 2026-09-11 历史切片：B 测量部署入口（当时不等于完整 Profile）

新增 `hcuopt.deployment.bw20_stage0_deployment.compose_measurement_adapter`，将原先需要
手工传入的 session builder / admission 回调收束到明确入口。没有新增判决器或任务队列。

- 输入是部署侧冻结的 Target fingerprint、`s0-g0-v2` protocol Hash、workload ID、
  ControllerBundle 和独立确认的 source manifest Hash；不能从 API Job 自行生成这些信任锚。
- 复用原 Target blocker 校验，精确约束五个 B 探针、原协议、当前 Target 和 workload。
  当前仓库 Target 中的 open blockers 仍会拒绝正式运行，不自动改为 resolved。
- 构造 adapter 不访问远端；fingerprint 走原 Harness，不上传源码、不创建 HCU 会话。
  计时探针才按需调用原 staging → guarded session → Docker transport → 原 factory。
- 每个 session 上传前、上传后和原 session.open/create 前均核验真实 Worker 租约；
  每次使用新 UUID 源码目录和容器名，拒绝重复 role/restart 或超过注册协议进程数量。
  会话预算最多480秒，不改原协议采样数/统计阈值；上传耗时不包含在 session 480秒内，
  仍受实际 Job 租约约束，不能把该会话预算当成整个任务的墙钟上限。
- 上传失败/中途失租不返回可启动会话；staging attempt（路径/Hash/检查/失败类型）保存进
  原 diagnostics。失败目录保留用于对账，不盲目覆盖、重试或递归删除。
- 只读源码模式并不防 owning UID/root 改写，既有独立 manifest 检查仍保留。
  当前每 session 独立上传，最多11份/计时探针；共享制品缓存优化暂不扩大到本切片。

专项测试 **21 passed**，覆盖当前 Target 拒收、scope/协议/源码/预算不匹配、上传前后失租、
独立会话身份、原 adapter 的 fingerprint 成功和上传失败 diagnostics；全部明确使用本地
夹具/替身。Ruff、新进程导入路径和 Python3.10 AST 语法检查通过，未声称镜像运行通过。
最终相关扩大回归 **344 passed / 7 skipped / 1 warning**；上述21项已包含其中。
跳过项仍为 Windows 下的 POSIX 文件安全测试，警告为既有 Starlette 弃用提示。

部署调用顺序：

1. 通过已有审查路径冻结 Stage0 Target/workload/窗口/频率策略；不要删除 blockers 冒充获准。
2. 用原 `freeze_controller` 冻结源码，独立核对 manifest；构造 `BW20MeasurementAdmission`。
3. 调用 `compose_measurement_adapter` 获得 B adapter，交给原 Worker/Handler。
4. C 的 Profiler/Hotpatch 部署适配、统一 Profile provenance 与按探针清理路由接完后，
   才能组成原七探针 Router 并显式注册到应用 Catalog。**本入口不注册、不等于第4步完成。**

### 当时的缓存协议缺口及方案（实现状态以文首为准）

查代码确认：`s0-g0-v2.yaml` 声明了 manual 频率要求，但没有声明 cache 清理范围/方法；
原 D 要求 cache 非 unknown 且 cleared_before_sample。现有 BW20 allocator 回执只证明
`synchronize → empty_cache → synchronize`，不能把它解释成硬件缓存已冷却。

建议下一步单独版本化“分配器清理 + 注册预热 + 稳态批计时”协议，明确不声称冷硬件缓存，
使 D 基于逐样本原始操作回执验证该口径；原 v1/v2 的行为与历史结论不改。
新版本应保持原采样、ABBA、置信区间和收益阈值，并增加 scope/操作/时序不符的负向测试。
若选冷硬件缓存口径，则须另行实现并验证设备相关 eviction/flush，不能使用 empty_cache 顶替。
这一方案尚未注册或执行；本轮仍未更改时钟、启动实机计时或运行真实 Agent 候选。

## 2026-09-11 最新：原 D 的 BW20 回执复核接线

本节更新下文“独立 D 回执绑定待做”的历史状态。仍为本地未提交代码；下文已经上传的
197 文件快照不包含本节改动，不能作为当前 D 版本部署验收的依据。

- 原 `Stage0Verifier.verify` 的七探针 Barrier 内，BW20 的 fingerprint/timer/noise/
  known_signal/null_signal 增加 `verify_bw20_diagnostics`，不新增统计判决器。
  旁证通过原 trusted-root/hash reader 读取，并纳入最终 verification input digest。
- diagnostics 绑定本次 primary evidence URI/Hash、资源、lease/fence 和清理回报。
  D 独立核对每个会话的完整 CID、宿主 PID/starttoken、原始 procfs/NSpid/cgroup、
  容器 PID、正常 waitpid，以及与原 lifecycle evidence 的一致性，不复用生产者校验函数。
- 四种计时探针逐样本核对请求、批量次数、时间戳、device ticks、allocator 操作范围与顺序。
  缺失/重复/错轮次、篡改主证据绑定、修改宿主最终快照、未正常回收子进程均拒绝。
  负向测试重新计算旁证 Hash，避免只验证“文件 Hash 不一致”而漏掉内容校验。
- 修复 health 回报缺少 `resource_id` 导致原 Formal reference 拒收的问题。
  原 Worker 的成功输出现通过真实 reference 构造测试；未放宽契约类型或健康要求。
- 修复 calibration session 原先直接强制清理、没有正常 child reap 的收尾缺口：
  有效权限内先正常 close/reap，再精确 CID 清理；失租/异常仍清理所有本任务会话。
  清理成功但缺失正常退出证据时，D 仍拒收；单会话清理异常不阻断其余会话清理。

本机 Python3.12 最终扩大回归通过 **323 passed / 7 skipped**，包括 BW20 transport/
session/adapter、原 Worker lease/heartbeat、Harness/V2、原 D/protocol/statistics。
7 项是 Windows 下缺少 POSIX openat/O_NOFOLLOW 的既有跳过，未当作 Linux 通过。
含四探针正向覆盖的回执专项 **23 passed**；已包含在扩大回归中，不重复相加报总数。
Ruff、diff 空白检查、新进程 D/adapter 导入路径检查通过。

这些是工程和明确夹具测试，不是 BW20 真实 HCU 测量；本切片没有远端部署、时钟写入、
新 PostgreSQL 联验或真实 Agent 候选执行。尚需完成：

1. 注册缓存口径与实现。allocator `empty_cache` 不是硬件 cache flush，原 telemetry
   仍为 unknown，原 D 环境闸门不放宽。
2. BW20 正式 Profile/admission、当期独占窗口、频率设置与恢复范围。
3. 新版本 Linux/锁定镜像联验、主机时钟开销验证、真实五个 B 探针及原 C 两探针。
4. 七探针完成后，真实 Agent 候选加载/正确性/性能/恢复与结果页展示。

## 2026-09-11 后续：原 Worker 任务收尾与缓存操作回执

本节更新前一节“待接任务清理”状态。代码尚未提交/推送，仍未注册正式 BW20 Profile。

### 已实现及验证

- 新增 `BW20Stage0ProbeAdapter`，在原 `JobHandlers.handle_stage0_probe` 中复用原 Probe、
  Harness、证据契约；依赖显式部署 admission 检查与真实 Worker 回调，不提供默认放行。
- `JobHandlers.cleanup` 对实现 `cleanup_probe` 的 Stage0 adapter 使用 job/lease/fence
  绑定的清理路径；其他 adapter 维持原路径。成功、异常和失租均返回 fence/health。
  未知 context、setup 未完成、健康不明不能报释放成功；实际租约结算仍由原 Worker/DB 管理。
- 会话工厂在清理时锁住并关闭后续启动入口；清理后不能再创建下一组测量进程。
- BW20 子进程启用逐测量 allocator 回执：PID、序号、实际同步/empty_cache/同步操作、
  时间边界。Session 核对请求/进程、序号和先后关系，缺失、重复、错 PID、错误时间或
  冒称硬件cache已刷新均拒绝样本。默认 nmz36 worker 不启用此扩展，维持原输出路径。
- 成功和失败均在 `bw20-stage0-jobs/<job UUID>/<fence>/diagnostics.json` 保存原始遥测、
  主机时钟、CID/PID映射、缓存回执和清理证据。原测量证据维持既有目录，不重复嵌套UUID。
- 本地扩大回归 **206 passed**，含原 Worker 成功/失败/失租、原子进程测量循环、
  缓存回执拒绝、会话关闭后拒绝启动、nmz36兼容、原 Harness 和租约检查；
  有1个既有Starlette弃用警告。测试中硬件和部分控制面为替身，不是实机任务闭环。
  原D verifier/statistics/protocol另跑85 passed / 7 skipped；7项需要POSIX
  openat/O_NOFOLLOW，在Windows跳过，未冒充Linux验证。

缓存语义边界：`torch.cuda.empty_cache()` 仅释放未使用的 PyTorch 分配器缓存块。
**这不是硬件 L2/HBM 刷新证据。** 本轮不把 telemetry 的 cache unknown 改成 flushed，
不放宽原 D；仍需明确注册协议下的缓存口径并接入独立复核。

### 当期实机观察与源码准备

用户询问进程归属后，原任务已退出。本轮再次只读采集：HCU7显存2207744 bytes、busy=0、
KFD清单为空，auto/600/1800MHz。此前40.9 GiB记录保留为历史观察，不代表持续占用。
新观察：`results/bw20-telemetry-11ff7cc8-6426-468f-9180-b8c8525be2aa/report.json`，SHA256
`bb8a4f3e0a56bb35a9330e9f43a207d5ffdce8a57c2dedac6293f27e8a889c20`。

197个源码文件（2967911 bytes）已上传到新快照，原目录未覆盖：
`/home/github/hcu-auto-opt-runtime/bw20-stage0/1d5b765a-31e3-4176-87d7-051ac8988318/controller`。
两次独立manifest检查一致：
`sha256:e4b1bcdfa502ffa73b164bbc3cdd370078c54751c75f128b7982a6341f647c50`；archive：
`sha256:312b12eee6d06fb84b56227447474644ef786c6a885f492ce41883ae981c476a`。
source staging中的fence=1只是计划身份，不是实机租约。只读mode不能抵御同UID/root改写。

新快照在固定镜像内通过 **Python3.10.12** 的 worker/adapter/harness 导入检查。
仅CPU、无网络、无HCU、非privileged、UID1002、只读源码挂载；没有import torch。
完整CID `37541a01fc21ad39fc1e664a7577859a8569354221ae30fa69ffcc23f20087d4` 已确认清理。
证据：`results/bw20-controller-import-41d314f0-2b87-4a9a-ba61-884c47123e66/report.json`，SHA256
`d89a82b4d309dd4a6cd196ef1e0fb7febae0c3a0ebfb6476163691cd869a7d8e`。

剩余：独立 D 对进程隔离/缓存回执的完整绑定、正式 Profile/admission、当期测量窗口与
频率设置/恢复授权，然后才能执行真实五探针与 Agent 候选。卡当前空闲不等于这些已通过。
本轮未运行HCU Event、未更改频率、未给出优化收益；没有停止别人的任务。

## 2026-09-11：BW20 Harness 组件接线与只读设备采集

新增 `bw20_stage0_harness.py` / `bw20_stage0_telemetry.py`，保持未注册状态，
不解除 Target 阻断、不执行频率修改、不新增判决器。

- `build_bw20_harness` 复用原 `EvidenceMeasurementHarness`、V2 evidence、
  `Stage0MeasurementProbeAdapter` 和生命周期记录器。校验原 Worker 的租约回调与会话
  fence，设备编号显式绑定 BW20 物理7；不将任意以7结尾的资源名视为同一卡。
- 延迟创建 Event timer，fingerprint 路径不启动 torch/HCU。修复工作负载工厂遗漏 TIMER
  探针的问题；其余计时协议及 D 统计不变。
- HostClock 使用 BW20 主机的整数 CLOCK_MONOTONIC，并固定 boot_id，避免 Windows 与
  Linux 时钟纪元混用。SSH 开销仍进入校准观察，尚未证明远程时钟方案满足正式误差闸门。
- telemetry 每次刷新 CID→宿主 PID/starttoken 的身份映射，保留原始 SMI/sysfs/进程
  观察。归属不明的全局 KFD 进程保守保留为潜在干扰，不当成卡7的确定归属。
- 只关闭本 job 的会话，关闭后再检查显存、负载、进程和初始频率状态。清理不明、
  进程残留、频率改变均不能报健康；没有自动改频率或恢复其他人设备配置的操作。
- cache 状态暂为 `unknown` / `cleared_before_sample=false`，原 D 会拒绝相关正式性能
  结论；没有直接沿用旧 collector 写死的 `flushed=true`。下一步仍须补实际缓存操作回执。

本地相关回归 **180 passed**（1个既有 Starlette 弃用警告），包括原 Harness/Probe 的
fingerprint 证据生成与清理路径、TIMER 分派、失租/超时、PID重用、清理异常和遥测不完整。
该接线用例使用本地测试替身，不是实机任务验收。Ruff 和新进程导入路径检查通过。
补跑原 D verifier/statistics/protocol 与本轮遥测用例：108 passed / 7 skipped。
跳过项为 Windows 下的 POSIX reader/链接相关路径，不能算作 Linux 实机验证；
两组测试有交集，不将180和108相加宣传为独立用例总数。

### 实际主机采集结果（不是 Formal 测量）

在 `github@10.17.1.20` 用主机原 Python3.6 只读采集和读取整数单调时钟；没有创建
容器、分配 HCU、修改频率、停止任务。最终采集记录：

- 卡7显存占用 **43,946,070,016 bytes（约40.9 GiB）**，瞬时 busy=0。
- auto 模式，瞬时 sclk1450MHz / mclk1800MHz；不能使用上次600MHz观察代替当期状态。
- 全局 KFD 清单有14个进程，包括 SGLang、Ray 等；不声称它们全部属于卡7。
- 他人进程 `/proc/<pid>/exe` 不可读，保留 `unavailable` 和明确警告；PID与starttoken
  仍核对。不提权、不把权限错误解释为没有进程。
- **当前卡7并不空闲，未启动正式测量。** 这次结果只证明真实主机采集/解析可用，
  不证明 Stage0、缓存纪律、计时精度、优化收益或资源已获独占授权。

本地证据（results被Git忽略）：
`results/bw20-telemetry-ba81d40a-c1bc-4aad-9ba1-2b4efc8cb5e9/report.json`，SHA256
`98616f9bd0e9d210026488b1499a98e7b83760f40f1d32adc632f42b5c50d4a2`。
此前两次因他人进程exe权限不足失败，原失败收据分别保留于
`results/bw20-telemetry-966941a4-95c9-45e1-8514-04948b8df40d/` 和
`results/bw20-telemetry-398cae64-bae1-4d8e-98f6-f6f4a6fb1c1d/`，不覆盖成成功记录。

待收口：缓存操作证据、独立 D 的进程/缓存映射复核、统一 job Profile 接线与原 Worker
完成/失败清理回报、当期窗口/频率策略及恢复验证。以上未完，不能称为实机优化闭环已跑通。

## 2026-09-11：真实 PostgreSQL 联验及 BW20 源码上传通过

本节更新下方9月10日“未配置数据库连接/未上传”的历史状态，不表示Stage0已通过。

### 实际 PostgreSQL 17.11 联验

复用nmz36现有`hcuopt-m1-postgres-test`，通过独立临时SSH隧道连接。凭据仅在内存和
测试子进程环境中使用，不写入Git、日志或报告。每个用例创建新的随机
`hcuopt_lease_check_<UUID>` schema及最小jobs/resources表，无共享表迁移/TRUNCATE。

7 passed，包括：active检查不续期、过期拒绝、quarantine拒绝、fence变化拒绝、
lease_id缺失拒绝、真实资源行锁等待后重新检查过期、原HTTP Client→API→原Repository
成功/409拒绝路径。并发用例通过pg_stat_activity确认实际Lock等待，不是单纯sleep。
这验证实际PostgreSQL事务和API，不等于已在BW20跑完Worker领取任务到测量/结算。

结束后新增schema残留为空，自己的SSH隧道已退出；没有停止原数据库容器。
证据：`results/live-lease-postgres-dbedfb87-a112-4550-ab5b-6281c3ea9f64/report.json`，
原日志SHA256：`b95ff91b19050afa7a197813a782eb1f2d73d67ccb01871544b12393097a95a9`。
收据记录本次Repository/API/SDK/测试文件哈希，保留一个既有Starlette弃用警告。

### 实际 BW20 源码上传

新增`bw20_stage0_staging.py`：只打包controller的Python源码，生成确定性tar及独立固定
manifest；上传到全新UUID目录，校验archive哈希后逐个安全解包，不调用extractall。
拒绝链接/特殊文件/绝对路径/目录穿越/重复成员/非源码文件/超预算文件；验证文件清单
和内容后将文件设0444、controller目录设0555。失败目录保留对账，不自动覆盖或删除。

实际上传194个源码文件，共2934662 bytes，并通过两次独立的BW20宿主只读清单核验。
远端保留路径：
`/home/github/hcu-auto-opt-runtime/bw20-stage0/0f4ed741-86c5-42f2-9803-20c267243789/controller`。
这是本次冻结快照，不会随本地后续修改自动更新，后续会话须重新核对精确pin。

- archive SHA256：`172a30a73606166fbc65a8aaa83e6e4d9165172479d5467800b733d311fca506`
- manifest SHA256：`608a4eb924b32d445b66e435621ca8df7b4273e48ce53e60df8534d6bec45277`
- 证据：`results/bw20-stage0-upload-0f4ed741-86c5-42f2-9803-20c267243789/report.json`

重要边界：只读mode防误写，不能抵御宿主同UID或root改写；运行期间防写仍须受信
部署所有权及只读挂载策略保障，不能宣称完全不可变。此上传没有docker启动、HCU访问、
频率操作、Profile注册、Target放行、性能结论或自动发布。上传脚本使用的plan fence=1
只是目录/计划身份，不是租约授权；任何后续HCU启动仍须真实Worker租约。

### 固定镜像 CPU 入口加载检查

在同一固定SGLang镜像中，以UID/GID1002、1CPU/256MiB、无网络/无HCU、只读root及
controller单独只读挂载运行Python导入检查。create后验证真实daemon隔离配置，才启动。
实际Python3.10.12，从`/workspace/src/hcuopt/deployment/bw20_stage0_worker.py`加载；
父进程未导入torch，kfd/dri未映射。运行前后源码清单hash一致，完整CID
`8fed85786dfdd1da6250308ba043906e762bddb7cd2e37237cffe3423d4fcc4f`已确认不存在。
仅验证入口导入，不表示torch子进程、HIP Event或整套Profile已运行。

导入收据：`results/bw20-controller-import-9a5e0629-236c-44e0-8a7a-77d26025a8ea/report.json`。
三个实测report SHA256按数据库/上传/导入分别为：

- `56a580c4303edee40638df8086c97f3f6184113eca8a2a30f406476a0cf3ac94`
- `d353878046766768f61ae3c651fe7651da45a56d5d44b4283b9b8ce179e0dcf9`
- `c764b996db50aaddc23cefd183943d63952e8761e378ccaca5e1640f12704774`

本轮本地相关回归161 passed、Ruff及diff检查通过；真实PostgreSQL7项另计，均不作为
HCU计时通过。原始inspect/脚本及凭据依赖保留本地results，不自动随仓库发布。

下一步：将已验证的staging/lease接入BW20 Stage0 Profile及typed telemetry、原Harness
与健康清理，形成单次完整测量任务；不重复已经通过的No-op或CPU导入来代替测量接线。
本轮新增实现与前一轮guard接线仍未提交/推送；原`92e021d`提交不包含这些新增代码。

## 最新：原 Worker 租约检查与源码校验接线（本地验证）

承接已提交的 `92e021d`，本轮新增的仍是未默认注册的部署内部组件：

- 原API新增 `POST /v1/workers/{worker_id}/jobs/{job_id}/lease-check`，沿用原
  `JobHeartbeat` 的claim_token/fencing_token契约，成功返回204。Repository使用原
  job/resource事务锁，核对worker、任务状态、claim、resource owner、lease_id、fence、
  active状态及数据库`clock_timestamp()`下的有效期；只检查，不续期、释放或修改表结构。
  这不是额外登录机制，仍依赖现有内部控制面信任边界；不得直接暴露为公网接口。
- 原Worker在本地`_job_context`注入`assert_live_lease`函数，覆盖输入中的同名字段，
  不传出claim token。检查失败锁定lease_lost状态，检查前后同时检查停止/失租信号；
  任务结束后该函数失效。现有后台心跳继续独立负责续期，不更改已有任务的默认路径。
- `BW20SourceGuard`校验部署方独立固定的controller-manifest SHA256，固定BW20主机和
  UUID目录，检查文件清单、内容hash、缺失/额外文件、目录跳转、非普通文件与大小上限。
  校验程序为只读Python3.6兼容脚本，不导入controller、不安装依赖、不创建远端目录。
- `guarded_job_session`将原Worker能力、源码校验、现有SSH/procfs PID映射接到原
  `BW20TimingSession`；资源、fence、exclusive范围不符或缺少本地租约检查函数时拒绝。
  本地接线测试证明lease/source拒绝发生在Docker create之前。

验证：API→Repository为显式模拟数据库行的单测，不是PostgreSQL实测。扩大回归
98 passed / 5 skipped（5项为未配置`HCUOPT_LEASE_TEST_DATABASE_URL`的PostgreSQL
测试，另有既有Starlette弃用警告）；追加接线后源码/guard模块15 passed，二者有重叠。
Ruff、diff空白检查、新进程不加载torch及Python3.10/宿主3.6语法检查通过。
PostgreSQL测试使用随机专属schema及最小jobs/resources表，不调用迁移或TRUNCATE共享表。

未完成边界：真实PostgreSQL事务/并发联验、受控源码上传及存续期间防写保护、BW20
Stage0 Profile、typed telemetry、健康清理与独立D仍待集成。源码检查只是有界快照，
不能冒充整个运行期的不可变挂载；lease-check也不是物理独占窗口或频率授权。
本轮没有SSH运行、HCU计时、Target放行、页面部署、提交或推送。

## 最新：SSH/Docker 传输与真实 CPU 隔离演练通过

2026-09-10 18:15:34–18:16:02 +08:00，在 `github@10.17.1.20` 使用固定镜像
执行了两条 CPU-only 链路。此节更新下方历史切片中“尚无 transport / 未启动容器”的状态，
但不更新 Stage0 或性能验收状态。

- 新增 `bw20_timing_transport.py`：沿用原 OpenSSHCommandRunner 的 host-key 校验，
  实现 create、start/attach、JSON stdio、完整 CID 的 inspect/remove 及只读 procfs。
  JSON 行、请求、stderr 和响应等待有上限；同步 runner 输出目前为捕获后检查长度，
  不能称为整个 SSH 输出路径已具备流式内存上限。
- 真实隔离：镜像 ID `sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`，
  UID/GID 1002:1002、1 CPU、256MiB/no swap、32 PID、无网络、无挂载、无 HCU，
  只读 root、cap-drop ALL、no-new-privileges、独立 PID/IPC、16MiB noexec tmpfs。
  创建后和运行中均核对实际 Docker 配置，CPU 子进程也报告没有 kfd/dri 设备。
- 正常路径：ready → 宿主/容器 PID 映射 → echo → close → waitpid 成功 → attach 退出0
  → 精确 CID 不存在。宿主子进程872532对应容器PID7。
- 超时路径：同样通过 PID 映射（874636→7）与 echo 后，请求1秒超时触发，
  终止持有的本地 attach 进程，只删除本次完整 CID；daemon确认不存在。
- CPU 使用独立 `hcuopt-stage0-cpu-rehearsal-v1` 协议及 `bw20:cpu-rehearsal` 资源；
  PID绑定默认拒绝该协议，只有演练显式选择后才接受，不能冒充 torch 测量记录。
  标签 fencing=1 是演练归属标记，不是已获取真实 HCU 租约的证据。

本地原始证据位于操作任务目录下
`bw20-cpu-transport-37756ec6-d2b7-4321-a7f3-17092e557d0c/`，含61条命令回执、
两份实际PID绑定、ready/close记录及各文件SHA256；原始inspect只保存在本机，不默认上传。

- `report.json` SHA256：`bc3651bffab4fbde0f91005a3230ec9514d94376daf439b5dff22babc6be57ab`
- 被验 transport SHA256：`762668739557d81bc6b0d4098ab4a6190295b1e0806ed79b9d01c52aa38f8c33`
- 正常容器：`ef3d8d5456d3e0eb92305cc42e552646a198b9fcdfcf0e2f15661ebac9fdde6c`
- 超时容器：`de409edc63db2cdedbc7b4702117d541672d0a1e7bc6e9241f7ba281944010ab`

真实演练暴露并修复了三处单测夹具未覆盖的问题：原CommandResult输出是bytes而非str；
Docker27不接受`--pid=private`（省略该选项使用默认隔离，并继续核验namespace）；
容器不存在回执为`Error response from daemon: No such container: <完整CID>`。
已增加相应回归，SSH255、其他CID和模糊daemon错误仍不能当作删除成功。
失败尝试证据全部保留，未覆盖为通过。此前正常退出但清理误判的CID
`0f2412ec35f2e7f0f4746ff7a3406accf3e4cac41ba5564c237913314874faeb`已独立只读确认不存在。

边界：没有运行torch/HCU计时、改频率、停止其他容器、更新Target blocker、接通数据库
真实租约或默认注册Profile；没有验证实际BW20 GPU Worker与Harness完整联验。
`stage0_accepted=false`、`performance_conclusion=not_measured`、自动发布仍为false。
容器清理通过不等于设备/频率恢复或HCU资源可以释放。

下一步收敛到一条接线：受信源码staging与hash → 原控制面实时lease/fencing →
BW20 Stage0 Profile → 受控会话/原Harness → typed telemetry与独立D → 清理健康确认。
当期窗口和频率协议未满足前不执行正式测量；本轮代码尚未提交或推送。

本轮验证：BW20全模块、Stage0判决/测量/控制面及scripted集成共446 passed / 11 skipped，
保留一个既有Starlette弃用警告。追加真实接口回归后的transport/runtime/session定向测试
104 passed；两组有重叠，不应相加为独立用例总数。全部修改文件Ruff通过、diff空白检查
通过；新进程导入不加载torch，新增模块Python3.10语法检查通过，实际本地解释器为3.12。

## 最新工程切片：受控会话与原 Harness 接口接线

本节更新下方较早的“单卡命令策略与PID映射”进展，不代表已完成实机Stage0。

- 新增 `bw20_stage0_worker.py`。原`measurement/torch_worker.py`只增加可选子进程
  `device_validator`钩子，默认None保留原行为；BW20入口在fork后的被测子进程内检查
  HIP/单可见设备/PCI `0000:b1:00.0`/gfx936，再创建测试张量与Event。父进程不import
  torch，不在初始化HIP后fork。检查设备属性可能初始化驱动上下文，不应称为完全无
  设备接触；拒绝错误设备的测试证明的是尚未创建测试张量。
- 新增 `bw20_timing_session.py`。`BW20TimingSession`依次检查受信staging与实时lease，
  create得到完整CID，读取真实daemon隔离配置，核对后才start和读取子进程ready。
  它要求device attestation和宿主PID绑定；每次请求前后重查租约，非close请求前后
  重查身份映射。超时/取消/失租不返回一个可接受的测量样本。
- 实际容器配置校验覆盖镜像ID/digest、name/CID、标签/fence、private PID/IPC、无网络、
  root只读、cap/privileged、单render、资源限额、环境变量、两个只读bind、tmpfs、
  auto-remove与禁止自动重启；返回命令本身不能替代daemon检查。
- 正常close复核原始proc stat/startticks/waitpid/成功wait status，再确认CID消失。
  异常清理只针对持有的完整CID；没有可信CID、scope变化、daemon不可达或删除未确认，
  都保留`cleanup_complete=false`，不按name或宿主PID杀进程。
- `BW20TimingWorkloadFactory`返回原`DockerTorchEventTimer`和
  `DockerFormalStage0Workload`，兼容原`DockerProcessLifecycleRecorder`；拒绝复用
  进程会话身份，保留失败session以便清理与对账。它不申请/释放租约、不结算预算。

这些是部署内部注入接口，不接受用户提交一个pass布尔值当作证据。staging/lease/inspect/
PID绑定的具体实现必须由受信Worker组成，使用原控制面授权与不可变制品。当前没有默认
SSH/Docker transport，也没有全局Adapter注册，所以不能说真实Worker已能运行Stage0。
容器不存在只是容器级清理确认，不是设备健康、缓存/频率恢复或资源可释放判决。

下一步：

1. 实现具体SSH/Docker transport：create/start-attach/结构化stdio、CID级inspect/remove、
   有界读写、取消和连接断开处理；接受信远端staging/原lease接口，不在旧Python主机装产品。
2. 单独审阅无HCU容器演练范围，用实际PID命名空间/procfs权限验证映射与清理；没有权限
   就失败关闭，不自动sudo或切到host PID。
3. 再接BW20 Stage0 Worker Profile、typed telemetry、原清理健康确认和独立D；固定
   当期Target/协议/预算，审阅正式HCU窗口及频率恢复后才采集。

当前改动均未提交/推送；没有重跑模型或HCU、改频率或修改Target blocker。旧nmz36的
实际计时日志不能用于证明新增可选钩子的实机运行；固定证据回归通过也不等于实机通过。

本切片本地回归200 passed / 7 skipped（原POSIX条件保留），Ruff、diff空白检查通过；
独立进程导入不加载torch，Python3.10语法检查通过。运行测试的实际解释器仍为本机3.12，
容器/设备/SSH相关对象均是测试夹具，不是CPU容器或HCU实机验收。

## 最新工程切片：单卡命令策略与 PID 映射

新增 `src/hcuopt/deployment/bw20_stage0_runtime.py`，尚未注册为 Adapter，也没有实际
启动容器。下列能力已经有本地回归，不能称为 BW20 Stage0 实机验收：

- `build_timing_plan()` 复用固定 Target 身份校验，只生成命令元组，不执行。
  只暴露kfd/renderD135，HIP/ROCR/HSA逻辑0，CPU64–79/NUMA4；固定镜像和只读controller
  源码/HYHAL挂载；private PID/IPC、无网络、只读root、cap-drop、4GiB RAM/no swap、
  128PID、1GiB shm和1GiB noexec临时目录。这是待验的计时工作负载策略，不能冒充已
  批准的模型冒烟容器命令。未添加`--init`：原controller自己做PID1并fork/waitpid。
- 只接受BW20完整资源名及正整数fencing token，不接受nmz36的`hcu-7`。
  生成命令不证明token当前有效，实际启动仍须原控制面的实时租约检查。
- `capture_process_binding()` 核对完整容器ID、name、managed/resource/fence标签、
  Running/StartedAt、private PID；通过原ready的容器PID和startticks匹配宿主直接子进程。
  同时检查NSpid、父子关系、PID namespace、cgroup v1/v2归属；重读容器状态和进程
  startticks，拒绝重启、PID重用、错容器、缺失、权限错误和多个匹配。
- 原procfs/waitpid/Event记录保留容器PID，不改写成宿主PID。新增映射只给宿主telemetry
  使用；它是一次观察，不是长期managed标记，更不是终止任意PID或释放资源的权限。
- inspect和procfs读取可由受信目标主机transport注入。测试覆盖不会误读本地控制面的
  `/proc`；不要求在Python3.6主机安装Python3.10产品。远程transport本身还未接入。

尚需完成后才能运行正式测量：

1. 将计划接入BW20专用进程factory，复用原计时通信/生命周期；不得直接使用旧启动器。
2. 受信staging校验源码清单与Hash、镜像/真实HostConfig和PCI；被测进程在分配设备内存
   前验证物理设备。原torch worker没有BW20专用PCI检查，不能仅凭可见设备数认定身份。
3. 接入只读宿主procfs transport并确认权限可用；权限不足失败关闭，不自动sudo或共享
   host PID。宿主telemetry在每次采样重新验证映射，清理使用当前持有的容器ID和fencing。
4. 为新factory补取消/超时/失租/失败清理接线、CPU-only生命周期验证，再审阅HCU窗口、
   精确命令和频率设置/恢复。当前命令元组不能直接作为执行授权。

未改原`nmz36_runtime.py`、`torch_worker.py`、Harness、D或Target blocker；未提交或推送。

本切片扩大回归：150 passed / 7 skipped，保留原POSIX平台跳过条件；新进程导入与
Python3.10语法检查通过。测试运行在本机Python3.12，procfs/inspect是明确夹具，
尚无真实容器PID映射或HCU计时证据。

## 最新：获准只读预检已运行

用户明确批准脚本通过 SSH 传输并只读运行。2026-09-10 17:29:52 +08:00，
`bw20_stage0_preflight.py` 在主机 Python3.6.8 直接执行完成，SSH退出0，stderr为空。
通过标准输入传输，不在远端落盘；没有创建容器、分配HCU、改频率或停止进程。

- 主机 `github-bw20`、renderD135的PCI `0000:b1:00.0` 匹配；NUMA4。
- gpu_busy_percent=0，VRAM使用2207744 bytes，全局KFD进程清单为空。
- 频率模式auto，sclk600MHz，mclk1800MHz；未设置Target提议的manual1500MHz。
- 以上为非原子瞬时观察，不能证明已经预留测量窗口，不能导入为Stage0 Formal证据。

本地证据目录为操作任务目录
`C:/Users/17920/Documents/Codex/2026-08-11/https-asari-ai-blog-inference-optimization/`，
不随普通仓库clone分发。收据哈希对应本地接收文件：

| 文件 | SHA256 |
| --- | --- |
| 执行脚本 `bw20_stage0_preflight.py` | `a992531a62510488965844274660becb92d21c8da76997b067188faf4bfb55e6` |
| `bw20-stage0-host-20260910-r2.stdout.json` | `7110b546313cac1b3d3574d07bcccf6875ef0866a68c76c3defbe7963d301bc4` |
| `bw20-stage0-host-20260910-r2.stderr.log`（空） | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

### 项目内问题：独立主机预检不能继承产品Python版本假设

首轮退出1，stdout为空；在读取sysfs前即报 `future feature annotations is not defined`。
初版脚本Hash `9736274821af13e0f76f6a093567417d217c5a3152dd4cdd6e158542428befb2`；
失败stderr保留于 `bw20-stage0-host-20260910-approved.stderr.log`，Hash
`5a7b8ebf3bc2d4a3f443ed59d51df7f5e501569990b6329198c4d01585577ef2`。

修复仅删除独立脚本对新式注解语法的依赖，补主机Python版本记录及兼容回归；没有升级
主机Python，也没有降低产品Python3.10要求。新增用例检查3.6语法和注解形态；本地相关
回归33 passed、Ruff通过。实际Python3.6.8成功执行覆盖了仅语法检查无法证明的运行兼容性。
后续工作者应区别主机标准库工具与固定镜像内的正式Harness，不重复安装环境。

下一步仍是下文的隔离启动策略/PID映射接线；正式HCU测量、频率改动和恢复范围尚未授权。

## 预检执行前的接入分析（历史记录）

## 当前事实

- 任务 `b90b2fdb-24da-4e70-98ca-1a64d38bd0af` 已经由用户完成签核，数据库只读回查为
  `completed` 且有签核记录。性能仍为 `not_measured`，不是优化验收。
- 本日主机只读检查：`github-bw20` 的 renderD135 对应 PCI `0000:b1:00.0`；
  瞬时 busy=0，VRAM=2207744 bytes，频率模式 auto，sclk 当前600MHz，mclk1800MHz。
  这些是现场读数，不是独占窗口、频率稳定性或 Formal 证据。
- 已注册的 `s0-g0-v2` 要求 manual 频率模式。Target 中1500/1800MHz只是提议，
  `measurement_clock_policy_unapproved` 和 `device_isolation_not_reserved` 仍然 open。
- 未发送新预检脚本到远端：安全审查要求明确授权该源码 payload 的传输与执行。

## 本次新增的可复用入口

`src/hcuopt/deployment/bw20_stage0_preflight.py` 是标准库独立脚本，只读取 sysfs，
不 import torch，不分配 HCU，不调用 Docker，不写频率，不申请或释放租约。
它记录主机/PCI、NUMA、利用率/显存、原始频率表以及全局 KFD PID 清单；错误显式保留。

- 缺失或拒绝访问不是零占用，未知进程归属不是设备空闲。
- 全局 KFD PID 清单不能证明某 PID 在 HCU7 上执行。
- 输出不是原子快照，也没有绑定正式 TargetSnapshot、lease 或协议。
- 退出0仅表示观察字段采集完整；退出2表示不完整或身份不匹配。两者均不批准运行。
- `stage0_accepted=false`、`automatic_release_allowed=false` 固定；不能导入为 Formal 报告。
- 不对 BW20 套用其他产品纸面规格，设备身份仍以现场和冻结 Target 为准。

审核并获准后可在主机直接运行脚本。不要使用 nmz36 的授权、快照或测量结果替代。

## 已定位的接线差异

| 原有组件 | BW20 的处理 |
| --- | --- |
| `EvidenceMeasurementHarness` / `Stage0MeasurementProbeAdapter` / 独立 D | 复用原实现、原始证据格式与统计协议，不新增判决器 |
| `Nmz36Stage0MeasurementProbeAdapter` | 当前 profile 和 `hcu-7` 资源命名属于原部署；需独立 BW20 绑定，不能直接改 Target 名运行 |
| `_JsonContainerProcess._command()` | 当前映射整个 `/dev/dri`，使用 `--pid=host`、物理 ROCR7；不符合 BW20 单 render/logical0 隔离策略，不得直接执行 |
| `torch_worker` 子进程与 Event | 保留同子进程 Event、procfs starttoken、waitpid；隔离 PID 后需显式解决宿主 telemetry 的身份映射，不能用容器 PID 对照宿主 PID |
| 设备 telemetry / 原清理器 | 绑定物理PCI/7号卡、当前 lease/fencing 和 owned container；未知占用失败关闭，不停止其他容器 |
| G0-H startup overlay | 使用 Baseline/Candidate/Recovery 三个独立执行；Candidate 必须有真实 import/implementation hash 证据，挂载 tar 不算加载 |

## 下一执行顺序

1. 先取得新预检脚本传输/执行授权，保存当期原始输出与脚本 Hash。
2. 在不执行 HCU 的本地测试中补 BW20 启动策略、逻辑/物理编号和 PID 映射；通过命令范围、
   超时、失租、清理及原协议兼容回归。不得把 `--pid=host` 当成普通改名后继承的权限。
3. 提交精确的测量容器命令和窗口/预算，单独确认频率设置与恢复。记录原模式与频率，
   成功、失败、取消和失租都必须走 owned-resource 清理；恢复失败保持资源不可用。
4. 原五个 B 探针（fingerprint/timer/noise/known/null）收集真实数据，经原 D 复核。
   样本预算、阈值、功效和原始批计时口径使用注册协议，不能为了通过降低标准。
5. G0-P 与 G0-H 使用原 runtime probe 契约补证据，原七探针 Barrier 完成 Stage 0 判决。
   测量失败停止接受性能结论，不阻止只读检查与无 HCU 工程。
6. 冻结热点、真实 Agent 产候选，经原 Intake/Build/Harness/D 接受或拒绝；不直接赋予
   Agent/Apex 测量、签核、Worker 或发布权。

## 本地测试与问题记录

新增用例覆盖原始频率不改写、PCI匹配、缺失/权限/读错误、超长数据、KFD归属未知、
不完整退出以及“采集完整不等于放行”。与原协议和环境工具相关回归32项通过，Ruff通过。
测试解释器为本机Python3.12，不代表锁定镜像Python3.10或实机计时通过。

扩大到原 Harness V2、统计与独立 D 回归后：111 passed / 7 skipped；Ruff、diff空白
检查、新进程导入路径核对与Python3.10语法检查通过。跳过项保持原测试条件，未放宽验证。

项目内排查经验：主机存在 `/opt/hyhal/bin/hy-smi`，没有 `rocm-smi`，不要为此安装
工具或改主机环境。Windows测试不能创建含冒号的PCI目录；真实文件读写测试使用合法
临时目录，Linux PCI身份分支使用明确的纯路径夹具，不跳过身份检查。

MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；Stage 0 不构成优化收益或自动发布授权。

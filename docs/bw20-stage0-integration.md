# BW20 Stage 0 接入切片

2026-09-10。承接 Framework Smoke 人工签核完成后的第三步，不重复 No-op 功能运行。

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

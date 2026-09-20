# BW20 成对执行边界验收记录（2026-09-09）

## 简版

真实 Source/Artifact 已接入原 Worker/Handler、PostgreSQL 租约、BW20 单卡执行器与
原 D 评测。两个全新容器分别完成 Baseline/No-op 固定请求，HTTP200、文本一致、
输入5/输出8 tokens、结束原因 length；D 判定通过，资源恢复。

**通过的是成对执行边界，不是完整系统验收。** 本轮使用隔离数据库 schema 的诊断
Worker transport，没有经过正式 HTTP API/Profile 准入；No-op tar 仅挂载，未安装或
激活候选代码；没有 Stage0、性能结论、人工签署或自动发布。

## 详版

### 实际链路

原 GitSourceManager / NoopBuilder / LocalArtifactStore 的真实产物
→ 只读传输并校验源码包/runner/spec/8个模型文件
→ PostgreSQL 原子领取 + 原 Worker 心跳
→ 原 JobHandlers 的 Baseline执行/清理 → No-op执行/清理
→ 回读原始9文件并校验传输前后 SHA256
→ 原 SGLangSmokeEvaluator
→ EvaluationRun / EvidenceBundle / 原报告 / 数据库 Job结果。

新增 `deployment/bw20_pair_bridge.py` 只连接部署边界，未替换原 D 的等价判定。
数据库 transport 直接调用原 Repository，**不是 HTTP API 联验**。原任务仍未推进到
人工签核状态，诊断结果不能导入正式表假装已经通过准入。

### 本轮环境和身份

- 主机 github-bw20 / 10.17.1.20，物理 HCU7 / PCI0000:b1:00.0 / gfx936。
- CPU64–79、NUMA4；仅映射 `/dev/kfd` 和 `/dev/dri/renderD135`，逻辑设备0。
- 固定镜像 digest `a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`。
- 容器 Python3.10.12；本机控制器和单元测试 Python3.12，不能混称。
- 真实 Source commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`；原 manifest 保持不变。
- No-op tar SHA256 `4051b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a`。
- 断网、只读root、cap-dropALL、no-new-privileges、UID65534:65534。
- 16GiB宿主RAM/同额swap上限、PID512、exec临时缓存4GiB、shm2GiB、单文件2GiB。
- 每个容器唯一可写宿主挂载是本次 variant 输出目录，仅新增该目录的 nobody 写ACL。
- 未改宿主用户、模型、共享源码、共享依赖或时钟；未停止其他工作负载。

### 已完成的真实结果

Run `0d010e15-ec3d-40b3-8132-6ded761bf50d`。

| 项目 | Baseline | No-op |
| --- | --- | --- |
| Request ID | 2270c2a3-30b7-4912-b612-f49f438ce744 | f50780cb-e154-4a40-820a-02c0cee514a5 |
| 容器ID前缀 | 93fd8d05fbf8 | 73fa872f1e03 |
| UTC开始 | 11:40:26 | 11:41:54 |
| UTC结束 | 11:41:51 | 11:43:20 |
| 进程退出/请求 | 0 / HTTP200 | 0 / HTTP200 |
| 输入/输出tokens | 5 / 8 | 5 / 8 |
| 结束原因 | length | length |
| 清理 | 健康 | 健康 |

两边文本均为 ` Paris. It is the largest city in`；归一化输出 Hash 均为
`sha256:0facacb27f13cf4157af72ca50ab02a04589a36248100de65a692a700ddeb899`。
原始 response Hash 不同是正常的：原始响应保留请求ID、时间等非确定性字段。
原 D 明确比较文本、token数、结束原因，而不是拿整个响应字节做相等判断。

数据库实际续租27次，Job结果成功写回，资源经清理恢复 available。
两次独立实时 HostConfig 与各自 Docker die0/destroy 事件已保存。
结束再次只读核对：本次容器不存在，HCU7 busy0、VRAM2207744bytes、KFD PID目录空；
共享源码仍为固定commit且status为空。临时SSH隧道和本次host flock进程已关闭。

诊断schema的租约只能约束该schema内的Worker，不代表整机对其他用户独占；
host flock也只协调遵循同一锁的诊断进程。它们不能替代正式测量窗口授权。

### 失败和修复证据

第一轮 `fec5e90f-9ac9-4cc5-8d7c-8991a5840647` 两边均在服务就绪前失败：
`RuntimeError: No HIP GPUs are available`。原 D 判定false；Job succeeded仅表示
评测处理完成，**绝不等于模型通过**。这一轮原始证据完整保留。

受限初始化对比发现：相同设备映射和限制下，UID1002没有passwd条目，
torch.device_count为1但available=false；UID65534为镜像已有nobody，available=true，
实际设备属性为gfx936/PCI177:0。节点均0666且可读写。已证实UID相关差异，
尚未证明驱动内部具体是哪段passwd处理导致失败。

修复采用已经验证过的UID65534，并只给新建的两个输出目录授予写ACL。
`/var/log/hylog`警告在成功和失败初始化中都出现，不能作为本次失败根因。
模型日志仍有rotary launch448/bound256警告；未靠关graph/换backend掩盖，也未宣称修复。
此前48项算子正确性仅覆盖冻结用例，不证明所有shape安全。

### 独立回读与回归

`results/verify_bw20_pair.py` 对外部保留的plan/controller摘要复验，逐项检查：
原D结果与数据库结果一致、原始响应再归一化、证据清单SHA、stdout/stderrSHA、
执行请求与plan一致、实时容器的镜像/设备/用户/挂载/隔离限制、退出销毁事件、
清理与最终显存状态。回读通过，记录在 `independent-readback.json`。

另一个真实PostgreSQL CPU测试证明：有效租约可用；过期heartbeat被拒绝，不能复活。
CPU测试schema `bw20_pair_c0070af6eb7149bda9ba23f647aa8f61`，不修改既有表。

本地相关回归122passed/2skipped（仅Windows符号链接权限跳过），Ruff通过。
覆盖准备/传输校验、过期租约、外部设备占用拒绝、精确清理选择器、原执行生命周期、
原SGLang判定。该数量不代表122项HCU实测，也不等于真实超时/取消故障注入完成。

### 证据入口

- 本地：`results/bw20-paired-0d010e15-ec3d-40b3-8132-6ded761bf50d/`。
- 远程：`/home/github/hcu-auto-opt-runtime/bw20-framework-smoke/0d010e15-ec3d-40b3-8132-6ded761bf50d/`。
- 原D报告：本地 `pair/report.md`；判定、证据包、哈希清单也在 `pair/`。
- 数据库结果SHA256：`c0c74e1c31d9e2c997bc0d291f6ea2d306d1336d04ec4c3e96346c7555b43d27`。
- EvidenceBundle SHA256：`bbe056508d609b2e069c9def56154930e39b165be76cb42394fc5036e6408e04`。
- 冻结控制器输入tar SHA256：`e60341810f32211a224ef2d64c0c8d501db7f1c22ddcc0a59f541ad99d08ad53`。
- Plan SHA256：`5f77d6125838f249786ba0442c2f4be9b56a8682216edf9819da5942c348b2d9`。

这些Hash用于一致性复查，不是签名或权限证明。测试schema及证据保留；未提交或推送。

## 完整验收还差什么

1. 将BW20六类真实Adapter接成显式部署Profile，通过正常API创建任务；不能用本轮
   直接Repository诊断结果冒充准入。Source/Build的远程适配仍需连接。
2. 依据源码/运行wheel补丁映射、冻结workload及部署证据逐项处理Target blocker；
   不批量设resolved，不继承nmz36的gfx938 Stage0状态。
3. 完成正式Run的证据发布、结果页和人工签署入口；目前localhost4191的旧Run不是本轮。
4. 在获准测量策略/资源窗口内对BW20采集Stage0，再做真实候选激活与优化评估。
   原始服务日志里的latency/throughput不作为本轮性能结论。

因此当前 `full_system_acceptance=false`、`automatic_release_allowed=false`，不代签。

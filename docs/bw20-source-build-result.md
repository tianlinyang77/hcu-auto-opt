# BW20 真实源码制品链验收记录（2026-09-09）

## 简版

BW20 已实际完成 GitSourceManager → 两次 NoopBuilder → LocalArtifactStore →
候选 Worktree 清理。两次制品 SHA256 相同，基线与候选源码 Hash 相同，
共享基线保持干净。这是源码/构建/存储链通过，**不是候选代码生效或完整系统验收**。
下一步复用该真实制品做 Baseline/No-op 成对功能运行、原 D 判定和证据接线。

## 详版

目标：补齐 BW20 的真实 SourceSnapshot / ArtifactManifest，不用单测夹具代替。
入口：`src/hcuopt/deployment/bw20_source_build.py`；没有另建 Source/Build/Store 协议。

### 隔离与来源

- 固定镜像 digest `a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`。
- 只读挂载控制面输入、共享 SGLang checkout；唯一可写宿主目录为本次 UUID build目录。
- CPU64–65/NUMA4、4GiB RAM、PID128，断网、只读root、cap-drop ALL、UID1002；
  无 HCU device 或 Docker socket，未安装宿主软件。
- 控制面源码输入 tar SHA256：
  `8fe5934f21fe571d654baa36938d3acd972b6f3c2799d344485481c86f0c9b68`。
- 共享源 commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，
  tree `a883d7eb4b4667768c19f0c9b0456d41f52da91c`，前后 status 均为空。
- 先克隆到独立 baseline，再调用原 SourceManager 创建候选；不在共享 `.git` 注册 worktree。
- 共享 origin 为 HTTPS，Target 是同仓库的 SSH 地址。代码仅允许这两个固定别名，
  并只把独立 clone 的 origin 设置为 Target 声明值；不修改共享 origin、Target 或源码。

### 实际结果

UTC `10:23:45.705`—`10:23:53.977`。SSH0，Docker die0+destroy。
容器 `46cd674b7e44c3df84d7e0cdeb0818cc69c074a513f876070e1d768b9381c4d2`
已删除，独立 baseline 的 worktree list 只剩 baseline，无候选残留。

| 项目 | 值 |
| --- | --- |
| Candidate ID | `aefbb5a2-3cc7-492b-91ab-bbfc610e31f6` |
| Artifact ID | `c2779ac1-6b27-441a-8248-13d9dbe5593b` |
| Baseline/Candidate Source SHA256 | `215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b` |
| 两次构建与 Store 文件 SHA256 | `4051b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a` |
| 制品大小/模式 | 73410560 bytes / 0444 |
| source-build-result.json SHA256 | `94980a350ffe5e9624f0c2bc4923dd0c32a4746c34bbad329f3b1987f360896f` |
| artifact-manifest.json SHA256 | `70ec7f2dfde5aba6a934280cedbaadb80c6d545594f4d615f6d5c68f96928291` |

最后两个文件已取回并核对两端 Hash 一致。Manifest 保留三项真实 Adapter provenance；
其中 source_commit 字段为 null，因此控制面输入身份另由上述 tar摘要绑定，不能声称
这些自述字段本身是签名或完整构建来源证明。

**配置证据局限：**本次 live inspect 与容器创建存在竞态，返回 No such object。
上面的隔离范围来自已保存的实际提交命令，不能写成全部 HostConfig 独立实测通过。
退出码、容器ID及删除有 Docker 事件证据；不为补截图重跑已完成的源码构建。

### 失败与修复

此前 r1–r5 的独立目录、stderr 和执行记录均保留，不覆盖失败。

1. Git上传打包报 `unable to create thread`。独立诊断显示 Python线程可创建、
   pids.current=2/max=128、RLIMIT_NPROC无限；不能简单归因为系统禁止线程。
2. 源是 shallow repo，Git2.34 明确忽略 `--local`，走 upload-pack；
   只给 clone `-c pack.threads=1` 未解决。显式在 upload-pack 入口加
   `pack.threads=1 / pack.windowMemory=32m` 后克隆成功，未放宽容器权限或PID上限。
3. 原 SourceManager 严格要求 origin 相同，拒绝 HTTPS/SSH 不同拼写；
   增加固定别名核对并在隔离 clone 中规范化后成功，未放宽原 SourceManager。

### 证据位置与后续边界

远端：`/home/github/hcu-auto-opt-runtime/bw20-source-builds/9787b132-4fd4-4f43-a1e8-8ed232ca2afd/`。
制品为该目录下 `store/sha256/40/51b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a`。
本地：`results/bw20-source-build-20260909-r6/`；旧失败为 r1（无后缀）至r5。

`candidate_activation=not_installed`、`framework_pair_accepted=false`、
`automatic_release_allowed=false`。No-op archive 后续只读挂载不等于真实候选代码加载。
没有注册生产 Profile、变更 Target blocker、创建性能结论、提交或推送。

另做只读 Python源/镜像对照：详见 `bw20-python-source-parity.md`。

### 本地回归

相关八组测试日志最终为 **125 passed / 3 skipped**。这轮不能称为完全无干预：
Windows Git fixture 的 rev-parse 子进程长时间不返回，核实归属后终止该测试进程。
没有操作其他任务进程；随后受影响的
`test_reject_workload_and_lease_drift` 独立重跑 **1 passed**，
新增源码构建/rotary/库清点测试独立重跑 **24 passed / 1 skipped**，Ruff通过。
平台跳过包括Windows符号链接权限和既有CAS首次原子发布限制；Linux真实源码构建、
首次Store发布和Worktree清理已由上面的BW20容器运行另行验证。

总体验收仍缺：真实成对请求在BW20执行、当前资源租约/fencing、原D证据汇总与人工签核。
性能、Stage0、真实优化候选生效及自动发布没有本次通过结论。

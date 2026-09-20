# BW20 API 接线：真实 Build Worker 记录

## 简版

2026-09-09，新增的 CPU 远程 Worker 已实际执行原 `source_prepare → noop_build`
Handler，完成独立源码准备、构建、CAS 发布和候选清理。两项原接口返回值通过校验，
源码/制品 Hash 与此前验收一致。六类 Adapter 的显式 Profile 组合也已实现。

**这不是正常 API 创建任务后的全链路验收。** 当前只在 TestClient 验证了准入拒绝：
Target 闸门未解除时返回409，不能写入任务。默认 Catalog 没有自动注册此 Profile。

## 详细记录

- CPU入口：`src/hcuopt/deployment/bw20_build_worker.py`。
- 远程传输：`src/hcuopt/deployment/bw20_build_transport.py`。
- 组合门禁：`src/hcuopt/deployment/bw20_profile.py`。
- 原 SourceManager、NoopBuilder、ArtifactStore、JobHandlers 未另造接口。
- 原安全克隆逻辑提取为 `clone_locked_source()`，与先前成功的源码构建探针共享。
- API Build Worker 可使用原 Worker 的 claim/heartbeat/complete 循环；本轮实测直接
  调用远程 Handler，未声称测过这部分 HTTP 通信。

执行环境沿用固定 SGLang镜像/Python3.10，CPU64–65/NUMA4、4GiB内存、PID128、
UID1002，断网、只读root、无HCU设备、无Docker socket。控制器源码和共享基线只读；
仅本次 task UUID 目录可写。CPU路径不初始化HIP，所以不受GPU容器UID1002问题影响。
记录保留提交的完整命令和成功退出；**未独立采集实时 HostConfig**，不补造该证据。

真实结果：

- SOURCE_PREPARE成功，NOOP_BUILD成功，stderr均为空，两个容器在返回后均已不存在。
- Source SHA256：`215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`。
- Artifact SHA256：`4051b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a`。
- Candidate Worktree已不存在，独立baseline和共享source均干净。
- 实际Profile provenance为 `bw20-framework-smoke-v1`，不是fake adapter。
- 本地相关回归85passed/1skipped；跳过的是Windows首次CAS发布测试，远程Linux
  实际调用已覆盖CAS首次发布。另有既有Starlette TestClient弃用提示，Ruff通过。

证据目录：`results/bw20-build-jobs-992c0547-99f6-4d64-b8d6-6e90f990443e/`。
汇总 `cpu-worker-chain.json` SHA256：
`42461372442ccde8661d9b01a140558c1befbebf1159d90634cfc2992b973eb2`。
冻结控制器manifest SHA256：
`61d3ea684367aafbff7788c3a679d1244b86049dd04fb42b4957331bc99e98ec`；
202个源码文件在每次远程执行前都按该外部摘要校验，且拒绝额外文件和路径重定向。
冻结源码tar SHA256：
`2203a9c57715aa075bda6dfa5211d204b84097be325b9fdb9207ae06ff62197c`。

## 下一步，不改验收口径

1. 将完整源码/运行包补丁映射和F1冻结工作负载做成可检查的准入证据；不能把
   `all_python_files_equal=false`改写成raw source与安装代码完全相同。
2. 按证据处理Target对应闸门，显式启用组合Profile，再从真正的API创建新任务，
   通过原Worker的三段Job链路到`AWAITING_SIGNOFF`。
3. 发布该正式Run的证据并接结果页；由人完成签署，不迁移诊断结果冒充正式Run。
4. BW20 Stage0、候选生效和性能验收继续保持独立要求。

全部修改仍未提交/推送；没有自动发布、签署或新的HCU性能运行。

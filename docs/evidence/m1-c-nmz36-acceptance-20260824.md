# M1-C nmz36 启动时 Overlay 验收记录（2026-08-24）

## 结论

Issue #32 要求的人工热点接入、Candidate 源码与制品追踪、启动时只读 Overlay、真实加载
证明、Baseline 恢复和资源清理链路已经通过 nmz36/HCU 7 专项验收。

本次 Candidate 明确标记为 `fixture`，只用于证明管线工作；结果不代表业务优化，也不包含
任何性能收益结论。首个 `business` Candidate 仍必须根据真实 Profiling 和调用路径证据另行
选择并进入 M1-B/M1-D 测量、裁决和签核流程。

## 锁定输入

- 节点与设备：`nmz36` / `hcu-7`；
- SGLang Baseline Commit：`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`；
- 镜像：`10.16.1.152:5000/jenkins/model_test_env/sglang@sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`；
- Target Fingerprint：`sha256:1570580b386ee2dab432d023aa3f05289e21643f9ba1a174ce5bc738aa55bf8c`；
- Profiler 原始证据 Hash：`sha256:84a514c26c30cbca4f289e94e0cdbacc26308c03f90c04cec9575ac22e22e4de`；
- 逻辑替换点：`sglang.srt.layers.layernorm`；
- 容器内挂载目标：`/usr/local/lib/python3.10/dist-packages/sglang/srt/layers/layernorm.py`。

## Candidate 与制品

- Candidate ID：`3b8aa434-fdbe-5270-b9ae-5c3ecdf5e0b4`；
- Hotspot ID：`744a0d43-b590-55cb-b0dd-13bfc7322908`；
- Hotspot Intake Hash：`sha256:476b84dfafbf5f4cd6dfea34066e8420564dfc64831a2dc2433908e9d9fee33a`；
- Candidate Source Hash：`sha256:55a297e0977d7d3578f4a4c5c65c2ebb9fac5aca183da96c87f5300aa83c61a6`；
- Overlay Artifact Hash：`sha256:dc085097345b5c633b66b9159f524c452bf998e9ca15ae7cbd6ec8a6a34b53bb`；
- Candidate 类型：`fixture`；
- 启动方式：`startup_overlay` / `overlay_only`。

构建阶段从 Baseline SourceSnapshot 创建隔离 Worktree，只应用已审核的一个 Python/Triton
替换文件，生成确定性的 Candidate SourceSnapshot、只读内容寻址 Artifact 和最小 Build
Cache 记录。构建结束后临时 Worktree 已删除，Baseline Source Hash 前后均为
`sha256:215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`。

## 三段真实运行结果

Baseline、Candidate 和 Recovery 分别使用独立的容器执行请求和服务进程：

| 阶段 | Execution Request ID | 实际实现 Hash | 输出 Hash | 结果 |
|---|---|---|---|---|
| Baseline | `5dec5696-501e-43ae-8b74-a0aa7f6f38c5` | `sha256:74b23b7a483901e02d086c4098a47c707140d13d87b680504ee461343f68ec91` | `sha256:0776304e8f13a0b8e043f850b864361d3c3b21e0412d90e5d8afc00fd0dba18c` | 通过 |
| Candidate | `fea6329f-5516-4e75-8fe9-5e8b1f7c71be` | `sha256:dc085097345b5c633b66b9159f524c452bf998e9ca15ae7cbd6ec8a6a34b53bb` | `sha256:0776304e8f13a0b8e043f850b864361d3c3b21e0412d90e5d8afc00fd0dba18c` | 通过 |
| Recovery | `b3eb1469-24bf-496f-9285-e6bd6f4e3aff` | `sha256:74b23b7a483901e02d086c4098a47c707140d13d87b680504ee461343f68ec91` | `sha256:0776304e8f13a0b8e043f850b864361d3c3b21e0412d90e5d8afc00fd0dba18c` | 通过 |

Candidate 的 import attestation 证明 SGLang 服务进程实际加载了只读 Overlay 文件，其加载
Hash 与 Artifact Hash 一致。Recovery 未挂载 Candidate，实际实现 Hash、固定请求输出和
缓存命名空间均恢复为 Baseline 状态。

容器内 PID 命名空间可以在不同容器中重复使用同一个 PID，因此独立进程身份按“不同
Execution Request/容器身份 + 各自进程证明”判定，不能仅比较容器内 PID 数字。

## 清理和证据

- 三段执行均为 `synthetic=false`，退出码均为 0；
- Candidate 使用独立缓存，Baseline 与 Recovery 使用相同的 Baseline 缓存命名空间；
- 验收结束后不存在 M1-C Adapter 管理的残留容器；
- 最终复查 HCU 7 使用率为 0%，显存使用率为 0%；
- SGLang Baseline 工作区保持干净；
- 最终结果：`passed=true`、`activation_proved=true`、`recovery_passed=true`、
  `resource_healthy=true`。

原始证据由 verifier-owned 内容寻址发布器写入：

```text
file:///home/github/lyt/Asari/hcuopt-m1c-acceptance-20260824-v3/trusted-evidence/m1/3b8aa434-fdbe-5270-b9ae-5c3ecdf5e0b4/overlay-runtime/sha256-f1b58db0d893a04b8c72b974657f650a0a72151a39f6cbafcab3a34e363752cc.json
```

证据文件 SHA256：
`f1b58db0d893a04b8c72b974657f650a0a72151a39f6cbafcab3a34e363752cc`。

## 测试结果

- Ruff：通过；
- 本地完整 Pytest：`330 passed, 33 skipped`；
- PostgreSQL M1 契约测试：`7 passed`；
- nmz36 真实启动时 Overlay：通过。

前两次实机运行分别暴露并修复了容器 PID 命名空间误判和激活标记未由部署配置冻结的
问题。失败运行的证据被保留，不计入最终通过结论。

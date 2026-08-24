# M1-C 人工热点与启动时 Overlay 制品链

## 解决什么问题

M1-C 把“人工从 Profiler 中选出的一个热点”和“人工编写的一份 Python/Triton 候选修改”
变成可核对、可重放的输入，再生成只读 Overlay 制品。C 负责证明候选从哪里来、改了什么、
制品是否一致，以及运行后能否恢复 Baseline；C 不负责宣布候选正确或更快。

## 热点登记

先调用：

```http
POST /v1/manual-candidate/tasks/<task-id>/hotspots
```

必须提供：

- 真实 Profiler 原始输出 URI、SHA256 和 Real Adapter provenance；
- Kernel/算子名称、shape、dtype、meta、Python 实现位置和调用路径；
- Amdahl 时间占比、机会评分、上游查重结果、可补丁性和选择理由；
- 操作人，以及 `fixture`（管线夹具）或 `business`（业务候选）分类。

服务端生成 `intake_hash` 并不可变保存。同一幂等键只能重放完全相同的记录；夹具记录不能
被改写成业务成果。

## 可信候选源码输入

公开 API 不能提交 shell 命令、宿主路径或挂载。候选替换文件由部署人员审核后放入 Worker
专属根目录，并以期望的 Candidate Source Hash 寻址：

```text
<trusted-root>/sha256/<前两位>/<其余 62 位>/
├── manifest.json
└── files/
    └── python/sglang/.../replacement.py
```

`manifest.json` 使用 `m1-candidate-source-v1`，绑定 Candidate ID、Hotspot ID、Baseline 与
Candidate Source Hash、替换点、候选分类、Profiler 证据和审核人。每个替换文件还必须有
独立 SHA256。当前启动时 Overlay 只接受一个最小替换文件；Worker 只接受部署配置中允许的
SGLang Python/Triton 路径和容器内挂载目标，且只替换 Baseline 中已经存在的普通
`.py`/`.pyi` 文件。

## `manual_build` 执行过程

```text
读取 durable Job 中的不可变 ID
  → 从部署侧可信目录读取并校验源码包
  → 从 Baseline SourceSnapshot 建立隔离 Worktree
  → 只写入清单列出的替换文件
  → 校验修改路径和 Candidate Source Hash
  → 生成确定性的干净 Candidate Commit/SourceSnapshot
  → 将最小替换文件生成 python_overlay Artifact（不是整仓源码包）
  → 按 Artifact SHA256 原子发布为只读文件
  → 写入不可变 Build Cache 索引
  → 删除 Candidate Worktree，并复查 Baseline 未改变
```

相同 Candidate 重试时，SourceSnapshot ID、Artifact ID、Artifact Hash 和 Build Cache Key
保持一致。任何路径越界、软链接、Hash 不匹配、额外源码修改或 Baseline 污染都会失败关闭。

## 运行与恢复边界

构建结果交给后续正确性 Worker。实机验收必须复用 S0-C 已证明的三段生命周期：

```text
独立 Baseline 服务进程
  → 独立 Candidate 服务进程（只读 Overlay + 独立缓存 + import marker）
  → 清理候选进程和缓存
  → 新的独立 Baseline Recovery 服务进程
```

“Recovery”不是把 Candidate 改回去，而是在候选退出和清理后重新启动原始 Baseline，证明
源码 Hash、实际导入实现、固定请求输出和缓存命名空间都恢复一致。正确性判定属于 D，性能
原始采样属于 B；C 只提交加载、清理和恢复证据。

生产部署通过 `build_m1_source_artifact_registry()` 注入可信源码根目录、允许的替换源码路径、
逻辑替换点到容器挂载目标的固定映射，以及 Baseline/Candidate/Recovery 三段固定命令。
这些值属于部署配置，不能由 Candidate API 或 Job Budget 覆盖。

## 当前能力边界

- 只支持启动时 Python/Triton Overlay，不是进程内 Hot Patch；
- 不修改 `_C.so`、Driver、DTK、系统 BLAS/RCCL 或节点环境；
- 不接受自动 Candidate 生成、Agent、Beam Search 或自动发布；
- RMSNorm/LayerNorm 等示例若标为 `fixture`，只能证明管线工作，不能作为业务优化成果。

## 本地验证

```bash
ruff check .
pytest tests/unit/test_m1_candidate_builder.py tests/unit/test_m1_control_plane.py -q
```

PostgreSQL 热点幂等与 Candidate 绑定：

```bash
pytest tests/integration/test_m1_control_plane_postgres.py -m postgres -q
```

本地测试不能替代 nmz36 验收；最终还要在锁定实验环境中验证只读 Overlay、真实 import
attestation、独立服务进程、缓存隔离、Baseline Recovery 和资源清理。

## nmz36 专项验收

2026-08-24 已在锁定的 nmz36/HCU 7 环境中，使用明确标记为 `fixture` 的 SGLang
LayerNorm 替换文件完成启动时 Overlay 专项验收。Candidate 的实际加载 Hash 与 Artifact
Hash 一致，Baseline/Candidate 固定请求输出一致，Recovery 的源码、实际实现、输出和缓存
命名空间恢复一致，受管容器和 HCU 资源清理完成。该结果只证明 M1-C 管线可用，不是业务
优化或性能收益结论。完整证据见
[M1-C nmz36 启动时 Overlay 验收记录](evidence/m1-c-nmz36-acceptance-20260824.md)。

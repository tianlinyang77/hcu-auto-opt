# 公共接口层（platform-v1.2）

## 目的

公共接口层是四条开发线之间唯一允许共享的数据和调用边界。它解决“控制面怎样调用
执行、源码、构建和评测模块”；真实 F1 Profile 已接入，性能测量仍不在本阶段范围内。

当前版本为 `platform-v1.2`。它在 v1.1 上增加了 Target blocker 作用域，以及
Framework Smoke 的 baseline/noop 双执行身份；变更依据见
[ADR-0004](adr/0004-framework-smoke-dual-execution.md)。代码位于：

- `src/hcuopt/contracts/platform_v1.py`：跨模块数据契约；
- `src/hcuopt/targets/`：Target Lock 加载与校验；
- `src/hcuopt/adapters/interfaces.py`：外部能力 Protocol；
- `src/hcuopt/adapters/registry.py`：Adapter 显式注册；
- `src/hcuopt/adapters/profiles.py`：Task/Worker 共用的 Profile 能力目录；
- `src/hcuopt/workflows/interfaces.py`：Workflow 注入边界；
- `src/hcuopt/workers/handlers.py`：Job 到 Adapter 的薄路由层。

## 核心契约

| 契约 | 生产者 | 消费者 | 不变量 |
|---|---|---|---|
| `TargetSpec` | A/B | 所有模块 | 镜像使用 digest，源码使用完整 Commit，禁止自动发布 |
| `ExecutionRequest` | A/D | B | `argv` 是数组而不是 shell 字符串；租约执行必须带资源和 fencing token |
| `ExecutionResult` | B | A/D | 状态、退出码、起止时间和日志 URI 完整；Fake 必须标记 synthetic |
| `AdapterProvenance` | Adapter Owner | A/D/Registry | Profile、能力、实现名、版本和真假类型可审计 |
| `MeasurementSeries` | B | D/A | 原始样本 URI/Hash、协议和环境指纹完整；Fake 只能是 not_measured |
| `EvaluationRun` | D/A | Registry | 有意复测是新 Run，消息重放由 idempotency key 去重 |
| `ExecutionAttempt` | B | A/Registry | 物理重试属于同一个 Run；Framework Smoke 显式标识 baseline/noop |
| `SourceSnapshot` | C | A/B/D | Commit、Tree Hash、Source Hash 和 Worktree 可追溯 |
| `ArtifactManifest` | C | A/D | 内容 Hash、构建配方、SBOM/签名位置和 synthetic 标记完整 |
| `EvidenceBundle` | D | A/Registry | 绑定 Task、Target、Baseline、Candidate、协议版本和原始证据 |
| `Stage0Run` | A | B/C/D | 绑定不可变 Target Snapshot，区分 Dry Run 与 Formal |
| `Stage0ProbeResult` | B/C | A/D | 必须来自 completed Job；Formal 需要真实来源、独占租约和原始证据 Hash |

所有输入模型默认 `extra=forbid`。新增或改名字段必须走 ADR、上下游 Reviewer 和兼容测试，不能在各自模块重新声明同名结构。

F0.5 的证据边界和数据库升级说明见 [可信证据契约](f0-5-trust-contracts.md)。

## Target Lock

Target Lock 是可执行配置，不是说明文档：

```bash
hcuopt target-validate config/targets/nmz36-sglang-0.5.12.yaml
```

Target Loader 会拒绝 Tag-only 镜像、digest 不一致、非完整 Git Commit、路径穿越和自动发布；`ExecutionRequest` 会拒绝不完整租约和非 digest 容器镜像。移动分支只用于说明来源，执行时始终使用锁定 Commit。

## Adapter Registry

Worker 不再在构造函数中写死 Fake Adapter，而是接收一个显式 `AdapterRegistry`：

```python
registry = AdapterRegistry(
    profile="nmz36-framework-smoke-v1",
    executor=real_executor,
    resource_cleaner=real_cleaner,
)
worker = Worker("gpu-1", WorkerType.GPU, api_url, adapters=registry)
```

未注册的 Adapter 必须以 `AdapterUnavailable` 失败，禁止静默退回 Fake。现有 Demo 显式使用 `fake-v1-control-flow-only`，并继续只验证控制流。

## 四条实现线

| 目录/接口 | Owner | 第一实现 |
|---|---|---|
| `targets/`、`workflows/`、Registry | A | Target Catalog、Workflow 选择和 API/DB 接线 |
| `ExecutionAdapter`、`ResourceCleaner` | B | SSH/Container Executor 与真实 Fencing |
| `SourceManagerAdapter`、`BuilderAdapter`、`ArtifactStoreAdapter` | C | 固定 Commit、Worktree、No-op Artifact |
| `EvaluatorAdapter`、`EvidenceBundle` | D | SGLang Smoke、输出一致性和证据归档 |

## Framework Gate

公共接口层完成后，下一条集成链是：

```text
TargetSpec → SourceSnapshot → No-op Artifact
→ Baseline/No-op 双 ExecutionRequest → 双 ExecutionResult → EvidenceBundle
```

该链在 nmz36 的锁定镜像中跑通、失败可恢复、证据可归档后，才进入 Stage 0。Stage 0 拦截性能搜索和优化工程，不拦截搭建这条必要的执行框架。

`SourceManagerAdapter` 还负责 Candidate Worktree 的正常回收与异常恢复；清理只能作用于
受管 Candidate 目录，完成后必须重新校验 Baseline 未发生变化。具体语义见
[F1-C 源码与制品证据链](f1-c-source-artifact.md)。

A 线的持久化、状态、API、取消、复测与 Reconcile 已进入 F1 实现，详见
[Framework Smoke 控制面](framework-smoke-control-plane.md)。默认目录同时声明 Fake 和
`nmz36-framework-smoke-v1`；真实任务仍须通过相应 Target blocker 作用域。

F1 之后的 Stage 0 已建立 Target-bound 控制面和七探针 Barrier，接口与真假证据边界见
[S0-A Stage 0 控制面](s0-control-plane.md)。

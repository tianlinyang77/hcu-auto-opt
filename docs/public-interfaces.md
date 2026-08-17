# 公共接口层（platform-v1）

## 目的

公共接口层是四条开发线之间唯一允许共享的数据和调用边界。它先解决“控制面怎样调用执行、源码、构建和评测模块”，不在本阶段实现真实 SSH、SGLang、Build 或性能测量。

当前版本为 `platform-v1`，代码位于：

- `src/dcuopt/contracts/platform_v1.py`：跨模块数据契约；
- `src/dcuopt/targets/`：Target Lock 加载与校验；
- `src/dcuopt/adapters/interfaces.py`：外部能力 Protocol；
- `src/dcuopt/adapters/registry.py`：Adapter 显式注册；
- `src/dcuopt/workflows/interfaces.py`：Workflow 注入边界；
- `src/dcuopt/workers/handlers.py`：Job 到 Adapter 的薄路由层。

## 核心契约

| 契约 | 生产者 | 消费者 | 不变量 |
|---|---|---|---|
| `TargetSpec` | A/B | 所有模块 | 镜像使用 digest，源码使用完整 Commit，禁止自动发布 |
| `ExecutionRequest` | A/D | B | `argv` 是数组而不是 shell 字符串；租约执行必须带资源和 fencing token |
| `ExecutionResult` | B | A/D | 状态、退出码、起止时间和日志 URI 完整；Fake 必须标记 synthetic |
| `SourceSnapshot` | C | A/B/D | Commit、Tree Hash、Source Hash 和 Worktree 可追溯 |
| `ArtifactManifest` | C | A/D | 内容 Hash、构建配方、SBOM/签名位置和 synthetic 标记完整 |
| `EvidenceBundle` | D | A/Registry | 绑定 Task、Target、Baseline、Candidate、协议版本和原始证据 |

所有输入模型默认 `extra=forbid`。新增或改名字段必须走 ADR、上下游 Reviewer 和兼容测试，不能在各自模块重新声明同名结构。

## Target Lock

Target Lock 是可执行配置，不是说明文档：

```bash
dcuopt target-validate config/targets/nmz36-sglang-0.5.12.yaml
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
→ ExecutionRequest → ExecutionResult → EvidenceBundle
```

该链在 nmz36 的锁定镜像中跑通、失败可恢复、证据可归档后，才进入 Stage 0。Stage 0 拦截性能搜索和优化工程，不拦截搭建这条必要的执行框架。

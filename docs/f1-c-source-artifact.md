# F1-C 源码与制品证据链

## 定位

F1-C 回答“源码从哪里来、是否干净、产物是否一致”。它实现真实的
`SourceManagerAdapter`、`BuilderAdapter` 和 `ArtifactStoreAdapter`，但不负责 SSH、
HCU 执行、SGLang 输出判定或性能结论。

第一版链路为：

```text
TargetSpec.source_baseline
  -> clean Baseline SourceSnapshot
  -> isolated No-op Candidate Worktree
  -> deterministic source archive
  -> content-addressed local Artifact Store
  -> ArtifactManifest + logs + AdapterProvenance
  -> candidate cleanup + baseline revalidation
```

## 真实 Adapter

- `GitSourceManager`：只接受 Target Lock 的完整 Commit；已有 Baseline 必须同时满足
  origin、HEAD 和 clean 状态校验；Candidate 使用 detached Git Worktree。
- `NoopBuilder`：不修改 Candidate，使用 `noop-source-tar-v1` 配方生成确定性 Tar；
  路径顺序、mtime、uid/gid 和归档权限均固定。
- `LocalArtifactStore`：重新计算 SHA256，并按内容 Hash 原子发布只读文件；相同内容
  幂等复用，不同内容不能覆盖同一地址。
- `BuildCache`：F1 只冻结 `lookup` / `record` 接口和稳定 Cache Key，不实现搜索阶段
  缓存策略。

`SourceSnapshot.source_hash` 使用规范化目录 Hash。绝对路径和时间戳不参与计算，
文件路径、类型、可执行位、符号链接目标、空目录和文件内容参与计算，`.git` 管理
数据不参与计算。

## No-op 语义

No-op Candidate 是固定 Baseline Commit 的独立 Worktree，不增加标识文件，也不修改
源码。因此 Baseline 和 No-op Candidate 的 Commit、Tree Hash、Source Hash 一致；
“候选身份”只存在于 Candidate ID、Manifest 和日志中。

`ArtifactManifest.content_hash` 证明制品内容一致。Manifest 自身含 UUID 和创建时间，
所以重复构建只要求 `content_hash` 一致，不要求两个 Manifest 的 JSON 完全相同。

## 清理和失败恢复

Framework Smoke Handler 在 `finally` 中回收 Candidate，因此 Builder 或 Artifact Store
失败时也不会遗留正常 Worktree。恢复扫描只处理受管 `worktrees/` 目录下以 UUID 命名
的直接子目录；未知目录、Baseline 和受管目录以外的路径不会被删除。每次清理后都会
重新校验 Baseline Commit、Tree Hash、Source Hash 和 clean 状态。

## Target Lock 验收

最终验收必须使用 `config/targets/nmz36-sglang-0.5.12.yaml`：

- 主机 `nmz36`（`10.17.1.2`）；
- CPU `112-127`、NUMA 7、HCU 7；
- 精确镜像 Registry Digest，禁止用 Tag 或 `latest` 替换；
- `HYGON-AI/sglang-das` Commit
  `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`；
- Baseline 路径 `/home/github/sglang-das` 保持干净。

单元测试使用临时 Git 仓库且不访问 HCU；它们证明算法和失败语义，不能替代上述
Target Lock 验收。

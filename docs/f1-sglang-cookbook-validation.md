# F1 SGLang Cookbook 对齐验证

## 结论

F1 Framework Smoke 不再以“容器或服务能启动”作为成功标准。锁定的 SGLang
Commit 现编 wheel 后，必须在 nmz36 的物理 HCU 7 上完成模型加载、Ready、一次外部
`/generate`、证据保存和资源清理。

2026-08-19 使用最小修复 wheel 完成了上述验证：

- SGLang Commit：`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`
- wheel SHA256：
  `20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa`
- 派生镜像 ID：
  `sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`
- Registry digest：
  `sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`
- 模型：`Qwen2.5-0.5B-Instruct`
- 服务配置：`sglang serve + fa3 + page-size 64 + mem-fraction-static 0.85`
- `/health_generate`：HTTP 200
- `/generate`：HTTP 200，确定性请求生成 ` Paris. It is the largest city in`
- HCU 7：运行时显存 86%，容器删除约 8 秒后显存回到 0

实机原始证据保存在 nmz36：

```text
/home/github/lyt/Asari/hcuopt-sglang-wheel-evidence-Vd4TSN/fa3-hcuopt1-final
```

目录包含镜像和容器 inspect、wheel Hash、服务日志、Ready 轮询、外部生成响应、
清理前后 HCU 状态及文件 SHA256 清单。

## Cookbook 使用边界

配置参考固定到 `HYGON-AI/inference-cookbook-das` Commit
`2a7f431301e41e6ea1f377129bf5ba3e43ae299f`：

- `CONTRIBUTING.md`：使用 `sglang serve`，默认服务端口为 30000，并提供真实请求验证。
- `docs/model-deployment/sglang/qwen3.5.md`：Qwen/HCU 部署采用 `fa3` 和
  `page-size 64`。
- `docs/model-deployment/docker_images.md`：公开镜像组合以 SGLang 0.5.10rc0、
  DTK 26.04、Python 3.10 为主。

Cookbook 没有直接覆盖本项目的精确模型和 SGLang 版本，所以不能写成“官方组合已经
证明兼容”。本项目只复用公开部署经验，并用 Target Lock 实机证据独立确认。

## 控制面成对验收

同日将唯一测试标签推送 Registry 并按返回 digest 建立临时 Target Lock，随后使用
`nmz36-framework-smoke-v1` 真实 Profile 完成控制面成对验收：

- Task：`02f4ec13-5d29-4eff-8d97-7b7c62ba4344`
- 最终状态：`AWAITING_SIGNOFF`
- Candidate：`FRAMEWORK_SMOKE_PASSED`
- 物理执行：Baseline 和 No-op 各 1 次，共 2 个 `ExecutionRequest` 和 2 个
  `ExecutionAttempt`
- 结果：`synthetic=false`、`output_equivalent=true`、`difference_count=0`
- 两边规范化输出 Hash：
  `sha256:0facacb27f13cf4157af72ca50ab02a04589a36248100de65a692a700ddeb899`
- No-op Artifact Hash：
  `sha256:4051b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a`
- `cleanup_healthy=true`，结束后 HCU 7 使用率和显存均为 0
- `performance_conclusion=not_measured`

完整控制面证据位于 nmz36：

```text
/home/github/lyt/Asari/hcuopt-f1-run-PaQaFE
```

其中保留最终 Summary、PostgreSQL dump、Baseline/No-op 原始响应、比较结果、
EvidenceBundle、SHA256 manifest、Worker/API 日志和最终 HCU 状态。正式 Target Lock
因此可以切换到上述 Registry digest，并关闭 `locked_image_dependency_conflict`；
Stage 0 与设备独占窗口仍保持 open，不得开始性能优化或发布。

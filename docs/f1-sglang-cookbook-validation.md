# F1 SGLang Cookbook 对齐验证

## 结论

F1 Framework Smoke 不再以“容器或服务能启动”作为成功标准。锁定的 SGLang
Commit 现编 wheel 后，必须在 nmz36 的物理 HCU 7 上完成模型加载、Ready、一次外部
`/generate`、证据保存和资源清理。

2026-08-19 使用最小修复 wheel 完成了上述验证：

- SGLang Commit：`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`
- wheel SHA256：
  `20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa`
- 本地派生镜像 ID：
  `sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`
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

## 正式发布前剩余事项

当前镜像还是 nmz36 本地唯一标签，尚未推送 Registry，也没有 Registry digest。
必须先用控制面完成 Baseline/No-op 成对 Framework Smoke；通过后才允许推送镜像、
锁定 Registry digest、更新 Target Lock 并关闭 `locked_image_dependency_conflict`。

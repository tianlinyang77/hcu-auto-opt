# DeepSeek Anthropic Agent Provider

## 定位

该组件把真实模型调用接到现有 M2b Agent/Apex Proposal 层，默认服务为：

```text
https://api.deepseek.com/anthropic/v1/messages
```

它只负责读取冻结的 Generation Request、热点源码、Profiler Evidence 和 Knowledge Snapshot，
然后生成待人工审核的 Python/Triton startup-overlay Patch Proposal。它不拥有 HCU、Build、正确性、
性能测量、Holdout、Barrier/FWER、Signoff 或 Release 权限。

模型结果始终保持：

- `review_required=true`；
- `formal_intake_allowed=false`；
- `performance_conclusion=not_measured`；
- `automatic_release_allowed=false`。

## 凭据边界

API Key 不进入 `AgentRunRequest`、Apex Plan、环境白名单、argv、stdout/stderr、Receipt、Evidence、
配置文件或 Git。部署进程从自己的 Secret Store 或只读 Secret 文件取得凭据，创建
`AgentDeploymentCredential`，Runner 再为单次 Attempt 写入权限受限的临时文件，并只向固定
Provider Artifact 注入 `HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE`。Attempt 结束后整个临时目录被
验证清理。

调用方即使在 `AgentRunRequest.environment` 里提交同名变量，也会被 Runner 拒绝。Receipt 只记录
普通调用方环境变量名称，不记录部署凭据名称、路径或内容；摘要层还会按实际 Secret 值再次
脱敏。

不要把 Key 写入 `.env` 或命令行。示意部署代码中的 `/run/secrets/deepseek_api_key` 必须由部署
系统创建并限制读取权限：

```python
credential = AgentDeploymentCredential(
    environment_name="HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE",
    content=Path("/run/secrets/deepseek_api_key").read_bytes().strip(),
)
```

## 执行链

```text
A 原子领取 GenerationAttemptClaim
  → 部署方重读并校验 Profiler Evidence / Knowledge / Source
  → B LocalCommandAgentRunner 启动固定 Provider Artifact
  → HTTPS Anthropic Messages 请求
  → 严格 JSON Proposal 输出 + usage sidecar
  → B 发布不可变 RunnerExecutionReceipt
  → C 重读回执并发布内容寻址 Patch / CandidateProposalBatch
  → A settle + barrier/dedupe
  → D 独立复算
  → 人工 Review
```

Provider 禁止 HTTP 外发、禁止重定向携带凭据；仅测试时可显式允许 loopback HTTP。网络错误、401、
超时、非 JSON、重复 JSON key、NaN、缺失 token usage、响应超限、越界源码路径或 Patch Contract
错误都会失败关闭，不产生可审 Batch。

## 当前验收范围

单元测试使用本机 loopback 模拟 Anthropic 服务，覆盖单/多 Proposal、usage、HTTP 失败、超时、
超大响应、越界路径、Artifact/Profiler Hash 漂移和凭据不落 Receipt。该测试不访问 HCU，也不证明
生成 Patch 正确或更快。

真实服务连通后，还需要冻结一个 BW20 热点，把 Proposal 依次走完人工 Review、Source Package、
Build、正确性、Search/Holdout 性能、恢复、Evidence 和 Signoff，才能称为“真实 Agent 候选跑到底”。

2026-09-15 已对官方 HTTPS 地址做最小实时协议探测，未发送仓库源码：服务返回 HTTP 200，模型
身份为 `deepseek-flash` 并提供 token usage。使用 Provider 默认推理行为时，512 和 2048 token 都
可能全部消耗在 `thinking`，Provider 会按 `stop_reason=max_tokens` 失败关闭；显式
`thinking={"type":"disabled"}` 时，512 token 内返回 `end_turn + text`。因此首个结构化 MVP
Candidate 默认使用 `thinking_mode=disabled`；后续若使用 `provider_default` 提升复杂优化推理质量，
应在 Apex Plan 中预留更高 token，并继续让截断结果失败关闭。该探测只是服务兼容性证据，不是
候选正确性或性能证据。

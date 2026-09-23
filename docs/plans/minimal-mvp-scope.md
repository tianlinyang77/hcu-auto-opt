# HCU 自动优化系统：最小 MVP 范围

## 决定

先交付一条**单热点、单候选、单目标环境**的受控优化链路，不把多候选 Formal Round 平台作为 MVP 前置条件。

MVP 链路：

```text
BW20 固定热点
  → Agent/Apex 生成一个 Proposal
  → 人工审核并冻结源码包
  → 既有 M1 Build / 正确性 / 微基准测量
  → D 独立重读原始证据并裁决
  → 页面查看证据
  → 人工签核
```

## 固定验收边界

- 目标：BW20 / HCU 7；只使用已登记的 allocator 热点和对应 SGLang 源码环境。
- 候选：每轮只接入一个经过人工审核的 Agent Proposal；生成预算与尝试次数沿用已有冻结配置。
- 证据：保留 Baseline、源码与制品身份、正确性、测量、清理、D 裁决及人工签核的现有 M1 约束。
- 结果口径：只报告冻结 allocator 微基准结论；没有 BW20 SGLang 端到端证据就不声称服务级加速。
- 发布边界：人工签核不等于自动发布或生产发布，`automatic_release_allowed=false`。

单候选必须在观察性能结果前冻结，不能根据测量结果挑选赢家。今后若增加候选并根据 Search 性能筛选，才启用独立 Holdout 和批级 FWER；不能把多个候选塞进单候选链路规避多重比较。

## 暂不纳入 MVP

- 多候选 Search/Holdout/FWER 与 Formal Round Web 派发。
- 多 Agent 并行、通用热点自动发现、多框架、多机调度。
- 自动签核、自动发布、生产基线替换。
- 为未来适配预先搭建通用插件平台或重复的 Formal/Scripted 执行栈。

这些项目不删除其已有代码或历史证据，但在单候选 MVP 可交付前不继续扩展，也不能把其 Scripted 演练表述为真实优化验收。

## 当前已具备的实现与证据

[`bw20_agent_m1_intake.py`](../../src/hcuopt/deployment/bw20_agent_m1_intake.py) 和 [`bw20_agent_m1_promotion.py`](../../src/hcuopt/deployment/bw20_agent_m1_promotion.py) 提供固定 BW20 Agent 候选的受控接入路径；现有 M1 pipeline 提供 Build、正确性、微基准、D 裁决和人工签核；[`ManualCandidateInspection`](../../web/src/ManualCandidateInspection.jsx) 可读取 M1 证据并展示签核。

`docs/evidence/m1-formal-bw20-20260916.md` 记录了一个 Agent 候选在 BW20/HCU 7 的完整 M1 微基准链路和人工接受。该记录证明的是这个冻结案例，不自动证明部署可重建、任意新候选可重复执行，也不构成服务级加速结论。

已有入口各司其职：Agent 终态证据页使用 `/?agentEvidence=<generation_run_id>`；M1 候选结果与签核页使用 `/?manualCandidate=<task_id>`。BW20 M1 晋级现在将 Run、Proposal、Review、Candidate、源码包和审核证据哈希作为不可变 Task Event 写入控制面；Summary 返回该事件，结果页展示并校验来源链。页面和服务端签核都要求恰好一条来源事件与当前 Candidate Source Hash 匹配，不能通过拼 URL 假造关联。页面仍不展示源码 diff。Agent 终态页用于查看生成尝试、去重、审核和预算细节。详细步骤见 [`mvp-single-candidate-operations.md`](../mvp-single-candidate-operations.md)、签核核对见 [`m1-signoff-runbook.md`](../m1-signoff-runbook.md)，Agent 只读入口部署见 [`agent-inspection-deployment.md`](../agent-inspection-deployment.md)。

## MVP 剩余收口

1. **代码层操作手册与来源绑定已完成**：步骤可照做；来源事件会绑定 Agent Run/Proposal/Review 到 M1 Candidate Hash，服务端签核也会复核。
2. **待部署联验**：在目标部署上连接真实 API，确认结果页读取同一条控制面事件并复核批准/拒绝路径。
3. **待部署运维收口**：完成数据库与证据目录持久化、备份恢复演练及回滚说明。
4. **待当前版本实机验收**：部署最新代码后，在当期 BW20 授权和资源窗口下跑一条新的 Agent→M1 链路；不通过 CPU/Synthetic 测试代替。

上述收口完成后，可以交付一个边界明确的**单候选微基准优化 MVP**。SGLang 服务级验证和多候选自动筛选是后续阶段，不作为这个最小版的隐含承诺。

## 2026-09-23 实现进度

- **页面证据口径已修正**：正确性摘要、效果比、置信区间、MDE、测量协议、样本数、预热数、进程重启数、环境指纹和 raw Hash 均来自当前 M1 API 返回；缺失值显示“未提供”。页面不再硬编码某个候选的代码 diff、ABBA 组数或设备样本数。
- **操作手册已补齐**：[`mvp-single-candidate-operations.md`](../mvp-single-candidate-operations.md) 串起固定 BW20 Intake、一次 Agent 执行、人工审核、受控晋级、M1 Worker、结果页和签核，标出历史幂等键、密钥注入和实机停止条件。
- **身份边界已明确**：M1 Summary 不返回源码 diff 或 Agent Generation Run ID。晋级回执绑定 Run/Proposal/Review/Candidate/Source Hash；操作手册要求把晋级回执与 M1 Summary 的 Candidate ID 和 Source Hash 精确比对。两页面仍是独立授权入口，不可拼接 ID 冒充关联。
- **Agent→M1 来源已持久化并接入签核**：BW20 Promotion 将 Agent Run/Proposal/Review、审核记录/证据、Patch、源码包、Manifest 与 Candidate Source Hash 作为幂等 Task Event 保存；M1 Summary 返回该记录。页面展示来源链并拒绝缺失、重复或 Hash 漂移的批准；PostgreSQL 签核事务执行同等服务端检查，拒绝不匹配的批准。非 BW20 M1 行为保持兼容。
- **命令入口检查**：在仓库 `PYTHONPATH=src` 下分别调用 Intake、Proposal Review、Promotion 三个命令的 `--help`，确认手册中标出的参数与当前 CLI 一致。验证使用本机 Python 3.12，只验证参数解析，不代表 BW20 Python 3.10 部署或运行验收。
- **代码检查**：前端 61 项单测、ESLint、生产构建通过；Agent 晋级与来源签核后端定向单测 5 项及 Ruff 通过。全量 Python 单测在约 25% 后长时间无进展，本轮中断，不能据此宣称全量通过。
- **本机联页状态**：本机只启动了 Vite 前端，读取 M1 Summary 得到 HTTP 404；当前没有连接控制面 API，因此没有声称已完成页面到真实 Task 的联验，也没有使用演示数据替代。
- **外部收口仍在**：数据库持久化/备份恢复交接需在部署环境完成；新一轮 BW20/HCU 验收需另有当期授权。本次没有连接或启动 HCU。

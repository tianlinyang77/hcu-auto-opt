# 单候选 M1 MVP 操作手册

本文把已有 BW20 Agent→M1 的固定路径串成可执行步骤。目标是一个 Agent Proposal、一个经人工批准的 Candidate 和一个 M1 Task；不是通用多候选入口。脚本使用正式控制面和持久证据目录，不能在普通开发机上随意指向 HCU。

## 运行前检查

1. 在获准的 BW20 部署环境确认本次使用的 Stage 0、Target Snapshot、Baseline 源码、SGLang 镜像和 adapter profile；历史 nmz36 trace 仅是热点假设，不能代表 BW20 的热点占比。
2. 确认 M1 Worker、PostgreSQL、证据目录和模型 API 凭据文件都已由部署管理员配置。`HCUOPT_DATABASE_URL`、模型 Key 只经秘密管理方式注入进程，不写入命令行、配置提交或聊天。
3. 本路径固定使用 HCU 7、`auto` 频率和一个候选。保持现有频率策略，不停其他任务；没有当前窗口授权就不启动 HCU Worker。
4. 为新一轮指定全新的 `task-key`、`hotspot-key`、`generation-key`、`candidate-key` 与固定 UTC 时间。脚本默认 key 是历史验收的幂等身份，直接复用会重放旧 Task/Run，不会创建新一轮。

## 一轮执行

### 1. 创建冻结输入和 Agent Run

在已安装项目依赖、可以访问目标控制面的部署环境执行：

```bash
python -m hcuopt.deployment.bw20_agent_m1_intake \
  --source-root <BW20锁定的SGLang源码目录> \
  --baseline-snapshot <干净Baseline快照.json> \
  --evidence-root <持久化证据根目录> \
  --generation-root <持久化Agent Store目录> \
  --base-url <批准的模型服务地址> \
  --model <已批准的模型名> \
  --task-key <本轮唯一Task幂等键> \
  --hotspot-key <本轮唯一Hotspot幂等键> \
  --generation-key <本轮唯一Agent Run幂等键> \
  --created-at <固定UTC时间，例如2026-09-23T02:00:00Z>
```

保留脚本 JSON 输出中的 `task_id`、`baseline_epoch_id`、`hotspot_id`、`generation_run_id`、`input_uri/hash`。这个步骤仅建立受控输入，不创建 Candidate、不触碰 HCU，也不产生性能结论。

BW20 Intake 会把冻结参考 case 的页号重复事实追加到生成输入：4,090/4,091 个 token 分别覆盖 64 页，页号并非严格递增；其他正确性 case 还含非单调页序。自定义 `--hotspot-summary-file` 只能增加操作员说明，不能移除这些事实。Agent 仍可能提出错误假设，审核时必须对照实际 Patch 和冻结输入核对快路径是否可达、提案文字是否与代码一致。修改生成输入后必须使用**新的** `generation-key` 创建新 Run；不得改写或重放已结算的 Run/Attempt。

Intake 还将 2026-09-16 已签的单候选微基准记录作为内容寻址、`advisory_only` 知识快照输入。它提供一个可复用的 `torch.unique_consecutive` 思路及原有验收边界，不把旧 Candidate、测量、D 结论或签署移植到新 Task。新提案仍须核对补丁、冻结用例、独立测量和人工签核；特别不能从排序后比较的正确性 Oracle 推断一般 SGLang 运行时的释放页顺序等价。

### 2. 执行一次 Agent 尝试并检查提案

按上一步返回的 `generation_run_id` 和 `input_uri` 执行一次受限 Worker：

```bash
hcuopt agent-messages-run-once <generation_run_id> \
  --store-root <generation-root> \
  --input <input_uri对应的本地文件> \
  --worker-id <本次Worker标识> \
  --lease-seconds <已批准的短租约秒数>

hcuopt agent-generation-status <generation_run_id>
```

只有 Run 到达 `awaiting_review`，且保留 Proposal 的 Patch、Hash、目标文件和替换点都在冻结范围内，才进入审核。没有 Proposal、Patch 无法重读、触及范围外文件、Baseline/Target 漂移或 Worker 清理不健康时停止；不要补造候选或重复调用模型。

如需只读查看 Agent 终态，使用 `/?agentEvidence=<generation_run_id>`，并通过对应 Run 的授权入口读取。它与 M1 Task 页面是不同权限范围。

### 3. 人工审核并记录决定

人工检查原始 Patch、Proposal 意图、风险说明和目标文件；`approved` 只表示允许将该 Proposal 冻结为一个 M1 Candidate，不是性能通过或发布授权。将审核理由写入受控文件，将审核材料写入只读审计文件，然后记录不可变决定：

```bash
hcuopt agent-proposal-review <generation_run_id> <proposal_id> \
  --store-root <generation-root> \
  --decision approved \
  --reviewer <审核人> \
  --reason-file <审核理由文件> \
  --evidence-file <审核材料文件> \
  --idempotency-key <本次决定稳定幂等键> \
  --reviewed-at <固定UTC时间>
```

保留返回的 `review_id`。若拒绝，记录拒绝决定后结束本轮，不执行晋级。

### 4. 晋级一个 Candidate 到 M1

在保持同一个 Task、Baseline、Agent Store 和源码包目录的前提下运行：

```bash
python -m hcuopt.deployment.bw20_agent_m1_promotion \
  <generation_run_id> <proposal_id> <review_id> \
  --task-id <intake返回的task_id> \
  --baseline-snapshot <同一份干净Baseline快照.json> \
  --store-root <generation-root> \
  --source-package-root <受控Candidate源码包根目录> \
  --candidate-output-dir <隔离Candidate工作目录> \
  --candidate-key <本轮唯一Candidate幂等键> \
  --completed-at <固定UTC时间>
```

保存晋级结果 JSON，重点记录 `generation_run_id`、`proposal_id`、`review_id`、`candidate_id`、`candidate_source_hash`、`source_package_hash`、`manifest_hash`、`review_evidence_hash`、`agent_origin_event_id` 和清理状态。晋级程序必须复读审核、Baseline 和 Patch；任一绑定不一致就停止。晋级同时把 Agent 来源以不可变 Task Event 写入控制面；重复晋级只能重放同一份绑定，不得覆盖来源。

### 5. 运行既有 M1 链路并签核

只有已获准的 BW20 M1 Worker profile 才能消费队列中的 Candidate Build、正确性、微基准和 D 裁决 Job。浏览器不直接调用 HCU Worker。通过正式控制面读取 M1 Task Summary，页面入口为 `/?manualCandidate=<task_id>`。

签核前在 M1 Summary 和页面核对 Agent `generation_run_id`、`proposal_id`、`review_id`、`candidate_id` 与来源 Hash；控制面会要求恰好一条来源事件绑定同一 Candidate Source Hash，页面检查之外后端也会在批准事务内复核。来源缺失、重复或 Hash 不匹配时不能批准（可记录拒绝）。再按 [M1 人工签核 Runbook](m1-signoff-runbook.md) 重读 EvidenceBundle、D 结论、清理和发布边界。只有同一 Task/Candidate/EvidenceBundle 处于 `awaiting_signoff` 且 `synthetic=false` 时才签署。结果只适用于冻结 allocator 微基准；没有 SGLang 服务级证据就不宣称端到端加速，`automatic_release_allowed` 必须保持 `false`。

## 部署、恢复与停止

- 将 PostgreSQL 和 Baseline、Candidate、Artifact、Agent Review、测量及 D Evidence Store 放在部署方确认的持久化存储；部署前按其运维规程做备份、恢复演练和 Hash/行数核对。当前代码本身不替部署方保证 BW20 数据库持久化。
- 对未知的 Worker 结果先按 M1/Agent recovery 规程对账。未确认失败和资源清理前不要重跑；不要按 PID/端口名批量终止进程。
- Worker 结束后核对 Lease/Fencing、HCU 使用和 Worktree 清理证据；本手册不执行自动频率调整或生产发布。
- 本机演示已有冻结案例入口和启动说明见[单候选交付记录](mvp-single-candidate-delivery.md)。历史验收 ID 仅用于回看，不得拿来冒充新一轮运行。

## 不属于最小 MVP

多候选 Search/Holdout/FWER、通用热点自动发现、其他框架/机器、自动签核/发布和 SGLang 服务级自动优化均不由本手册启用。需要其中任何一项时，应作为后续独立工作，不通过复用单候选 ID 或放宽审核来实现。

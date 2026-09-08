# M2b：真实 Messages 生成器接入（开发阶段）

本切片把现有无 HCU 生成链连接到一个真实 Anthropic Messages 服务。它不是 Asari Apex 的
原版实现，也不开放 Formal Intake、HCU、签核或自动发布。现有 A 的 PostgreSQL 调度、
claim/reconcile/settle、B Runner、C 源码包、D 独立验证仍是各自唯一入口。

接口与失败语义见 [ADR-0018](adr/0018-m2b-messages-dispatch-inspection.md)；跨模块接受
仍待 PR 审核，不以 CPU 测试代替人工接受。

## 当前交付与未完成项

- `generators/anthropic_messages.py`：一个标准库实现的 allowlisted 程序，一次请求、无工具、
  无 shell、无生成代码执行。拒绝跳转、继承代理、非文本工具回复和截断回复，不自动重试。
- `prepare_messages_input()`：复用 C 的完整基线 Hash 复核，限定一个 Python 文件，重读知识
  Store，只提供知识文本，不 import Skills；热点摘要明确是人工参考，不是新测量结论。
- `messages_profile_for_input()`：把模型、endpoint、源码、知识和摘要组成的精确输入 Hash
  编入现有 Plan 的 `adapter_profile`。重试换输入就必须新建 Plan，不能偷偷切模型或上下文。
- `MessagesGenerationWorker.execute_claim()`：消费 A 的已有 Claim，校验代码 Hash、输入、预算、
  剩余 lease，调用 B Runner，保留不可变 Receipt，再交 C 解析。同一 Attempt 已启动就拒绝
  再次调用模型；进程崩溃后恢复回执或由 A 超时结算，不允许原 Attempt 重复付费。
- `MessagesProposalIngestor.ingest()`：从 B Store 独立重读回复和用量，校验输入 manifest、
  Request/Plan/Attempt/Artifact 绑定；模型只能提供意图、理由、风险和 diff，不能提供权威 ID、
  Hash、URI、签核或性能结论。复用现有 C 的补丁适用性检查，不执行补丁。
- 失败回复保留原始 Receipt；成功 HTTP/Runner 但无有效补丁时，产生零 Proposal 的 failed Batch，
  供 A 按实际 Runner 用量结算。任何一个候选非法，整批拒绝，不发布部分补丁。
- `examples/messages_hotspot_probe.py`：源文件级真实调用检查，支持 `--recover` 无调用恢复报告。
  **这个探针没有 DB Claim，也不创建业务 Candidate，不能替代上面的正式 Worker 接线。**

- `MessagesDispatchService`：通过现有 PostgreSQL API 领取指定 Run，执行一次并结算；保存
  Claim 和输入，重启后只重读 Receipt/Batch，不重复调用模型。CLI 已提供 run-once/recover。

- `AgentGenerationInspectionService`：单独的审核前只读检查，复用 D 的安全文件读取器、
  预算核对、回执/Batch/提案验证。不会调用 complete_generation_review 或发布终态索引。
- 前端新增 `?agentInspection=<run-id>` 直达入口，只调用 inspection API，不回退演示数据；
  零有效提案仍展示 Attempt、用量、失败原因和权限边界。

仍待完成：持久部署注册与受控密钥注入、真实模型结果的人工 Review/Promotion，以及独立
授权下的 HCU 正确性和性能验收。Linux PostgreSQL → D/API 模拟模型整链已通过；这不能
称为真实 Agent 多轮自动优化已完成。

## 服务配置与预算

非秘密示例见 `config/m2/messages-internal.example.json`，对应项目所有者指定的内部入口。
它显式允许 HTTP，仅限本次指定的受信网络服务；这不等于链路加密或来源认证，后续应迁移
HTTPS。API Key 由部署进程提供给 `execute_claim(..., api_key=...)`，只传给固定的可信 Wrapper，
不放入输入文件、Plan、数据库、日志、命令参数或模型上下文。不要提交 `.env` 或截图中的密钥。

初始建议为单 generator、单 attempt、单 Proposal、120 秒 HTTP timeout、150 秒冻结总预算，
145 秒 Runner timeout、150 秒 Claim lease、45,000 总 token 上限、4,096 输出 token 上限。
数据库要求 lease 不大于冻结 timeout；Worker 在预算内留出 5 秒清理/结算余量，不增加预算。
调度或落盘延迟仍可能使租约到期，此时结算必须拒绝，不能延长旧 Claim 强行通过。费用没有货币计价，
不能把 token 上限称为人民币/美元限额。token 实际用量包括：

```text
input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

`iterations` 和缓存分桶是重复拆分数据，不再累加。未知/畸形用量拒绝成功结算。HTTP 上游缓存
和系统提示词可能产生额外输入 token，因此总 token 预算是事后拒绝接纳的上限，不能保证上游
计费绝不超过它；输出数受 `max_tokens` 限制，墙钟由 Runner 限制，SDK 不自行补调用。

## 与 A 的接线顺序

1. 部署方从批准的 Baseline、Hotspot、Knowledge Store 准备输入；先确认源码披露范围。
2. 将 `messages_profile_for_input(input)` 和生成器文件 SHA256 写入现有 `GeneratorPlanEntry`，
   再通过现有 Coordinator/Repository 建 Run。保存原始输入，重试不得重新拼接不同内容。
3. `MessagesDispatchService.run_once()` 校验输入与 Plan；A 按冻结 timeout 领取 Claim。
4. 将 Claim 和精确输入交给 Worker，key 由部署侧注入。Worker 必须在无 HCU、无 Holdout、
无生产凭据的专用执行域；LocalCommand Runner 不是任意不可信代码的通用安全沙箱。
5. A 用返回的 Receipt/Batch 调用原有结算接口，不增加第二套调度状态机：

```python
result = worker.execute_claim(claim, prepared_input, api_key=deployment_key)
repository.settle_generation_attempt(
    claim.attempt.attempt_id,
    claim.attempt.claim_token,
    result.batch.batch if result.batch else None,
    result.receipt_ref,
    runner_receipt_reader=worker.receipts,
    batch_uri=result.batch.uri if result.batch else None,
)
```

6. A 汇总、去重，D 独立重读验证，之后才是人工 Review。真实 Proposal 的业务晋级和 Formal
   激活仍受 ADR-0011/#102 等既有门禁约束；本次源码级试跑不批准这些步骤。

Worker 不直接开放公共 Web 写入口，不读取任意 URI，不给 Agent 提供受保护 Holdout 明文。
Receipt 落库之前宕机时，先重读 `attempts/<id>/receipt-ref.json`；没有 Receipt 的已启动
Attempt 交 A 过期处理，不能通过删除 started marker 绕开付费调用去重。

## 单次调度和无调用恢复

先通过现有 `agent-generation-start` 注册输入 Hash 已绑定的单 generator Plan，再执行：

```text
hcuopt agent-messages-run-once <generation-run-id> \
  --input <保存的generation-input.json> --store-root <部署专用持久目录> \
  --worker-id messages-worker-1

hcuopt agent-messages-recover <generation-run-id> \
  --attempt-id <attempt-id> --store-root <同一个持久目录>
```

数据库地址使用 `HCUOPT_DATABASE_URL`；执行密钥只从 `HCUOPT_MODEL_API_KEY` 读取。
默认 lease 等于冻结 timeout，可显式设小但必须保留完整调用和结算余量；不可设大。
本版本有意限定一个 generator、一次领取，不自动循环或新建重试，不支持任意命令。
返回 JSON 是现有 Generation Run Status：必须检查 Attempt 和 Run 状态，命令退出 0
只表示调度/结算成功返回，**不代表候选合格**。HTTP/Runner 成功而 patch 非法时，Attempt
仍为 failed，实际 token 保留；无候选不进入 Review。

持久目录是部署方独占的可信 Store，包含源码、知识、原始回复和 Claim token；不得公开或
交给 Agent 写入。恢复不读取 API Key、不连接模型，不改变租约，也不删除 started marker。
已终结 Attempt 仅允许精确相同的 Receipt/Batch 重放。只有 started 而没有 Receipt 时，
报告无法恢复，交现有 reconcile 过期处理；不能假装“肯定未调用”后重新收费。

## 审核前 D 检查与页面入口

已有终态 `/evidence` 保持原规则，不允许为了页面可见而将 Run 提前标成 completed。
新增部署侧命令，为已结算、状态为 awaiting_review 或 failed 的 Run 保存不可变审核前快照：

```text
hcuopt agent-messages-prepare-inspection <generation-run-id> \
  --store-root <Worker持久Store> --knowledge-root <知识Store> \
  --task-id <已有task-id> --target-id <部署目标标识>
```

这条命令必须在 Linux 上运行。`HashedEvidenceReader` 要求 POSIX openat/O_NOFOLLOW，
Windows 不做兼容性降级。API 部署设置 `HCUOPT_AGENT_INSPECTION_ROOT=<同一Worker Store>`；
只读部署同时设置 `HCUOPT_AUTO_MIGRATE=false`，不让 API 启动隐式迁移数据库。
接口为 `GET /v1/operator/agent-generations/<run-id>/inspection`。调用者只提供 Run ID，
不接收任意文件路径、知识上下文或模型配置；根目录只由服务端指定。

部署创建 API 时还须注入 `agent_inspection_read_authorizer(Request, generation_run_id)`，
从既有、经过验证的会话身份检查对这个 Run 的读取权限。只有返回 Python `True` 才放行；
未注入或鉴权器异常为 503，拒绝/非布尔真值为 403。认证发生在读取 Store/A 状态之前，
每次 GET 重新调用，不因先前读成功而缓存权限。响应设 `Cache-Control: no-store`。

这不是现成 SSO：完整控制面仍需部署侧提供真正的身份校验与 Run ACL。不要使用恒真回调、直接信任
客户端身份请求头，或把模型 API Key 放到页面/URL/localStorage 充当登录凭据。普通
`hcuopt api` 没有注入器，配置 root 后仍会拒绝 inspection；需用部署 app factory 接线。
此变更只保护 inspection，旧 API 不自动获得同样的权限保证。内部反向代理只应开放明确
获准的只读路径，不能把整个开发 API 暴露公网。不要公开源码 Store 或原始回复。

第一版现已选定独立凭据方案，可使用不挂载控制面的
[专用只读部署入口](agent-inspection-deployment.md)。它提供真实凭据摘要/Run/到期验证，
不需要先建设企业 SSO；实际服务启动、端口开放和浏览器联验不由代码完成推定。

### 接线前检查顺序

1. 确认 PR/ADR 跨模块审核、固定代码 Commit 和独立 Worker Store；不改动原基线和账本。
2. 部署方提供已验身份 + Run ACL 的回调；关闭自动迁移，不向 API 进程注入模型 Key。
3. 先验证无鉴权 503、无权限/跨 Run 403、鉴权异常 503，且没有 A 状态/文件读取。
4. 用已授权测试身份读取审核前快照，再验证撤权后立即 403、证据漂移 422；不创建审核记录。
5. 浏览器通过同源受控入口显示结果，密钥仍只归 Worker；模型调用和 HCU 验收独立授权。

单元测试覆盖上述拒绝与 Run 绑定；真实 PostgreSQL + D/API 集成继续使用测试专用回调，
不将它冒充真实身份服务接入验收。原 `c2ec651` CI 六项通过，后续鉴权补丁结果独立记录。

每次 GET 均重读快照、Runner Receipt、Batch 和补丁，并重新运行 D Verifier；读取前后
两次比较 A 的当前状态。身份、Hash、用量、去重或状态漂移即拒绝返回，不读取陈旧缓存。
它不执行模型、补丁、HCU、Review、Promotion 或终态 publication。快照不可覆盖；
发生后续人工 Review 后，应转入已有终态证据流程，不复用审核前快照冒充新状态。

前端访问 `/?agentInspection=<run-id>`，显式登录后只向当前源的 inspection 路径发送凭据，
不使用 `VITE_HCUOPT_API_BASE`，避免凭据跨源。已构建前端可用 `npm run inspection:serve`
启动本机专用代理；后端和获准 SSH 隧道仍须事先配置，代理不创建数据库账号或延长授权。
这个入口不依赖 Round 列表，不回退 `?demo=1`。读取失败、零有效提案、生成失败都会
保留可解释的状态；HTTP/进程成功但 Batch 被拒绝时，不显示成“生成成功”。

沿用 D v1 的 `synthetic=true / scripted_dev_only` **开发证据分类**，不把它升级为 Formal。
真实 Runner 的 provenance 独立展示；它不证明真实模型服务质量，也不构成正确性/性能证据。
仍需人工审核后走已有 Promotion、Build、可信评测链路。

### 第三切片验证边界

- Windows Python 3.12：134 passed、4 skipped；4 项是刻意保留的原生 POSIX 安全读取测试。
- nmz36 Python 3.10、无网络/无 HCU CPU 容器：138 passed；覆盖原生 D 检查、API 和篡改拒绝。
- Windows Worker + 真实 PostgreSQL：9 passed、3 skipped；3 项同机 D 联验要求 Linux。
- 前端：15 项单测、4 项 Sites 包装测试、lint 和生产构建通过；没有新增云端部署。
- 代码包 `code-inspection-final2.tar.gz` SHA256：`8bc793525f503cbe3a8cf78b9eb8df3a47c8c1410892b22187a6515c02a5b523`。
- Linux CPU 日志 `cpu-inspection-acceptance.log` SHA256：`37092c904fb8d975b6ccd0d8f93c56ec0a332174cc3e6b11904dc05fd0742df8`。

随后经项目所有者单独批准，把同一临时 CPU 容器的网络改为
`--network container:hcuopt-m1-postgres-test`，完成 **12 passed，16.02 秒** 的 Linux 整链联验，
包含 3 个此前在 Windows 跳过的 PostgreSQL → 原生 D → API 场景（成功、非法补丁、上游失败）。
该测试使用本地模拟模型，不需要真实 API Key；其余源码只读、无 HCU、无 privileged 等约束不变。
每例只使用随机隔离 schema，结束确认无 `messages_test_*` 遗留，临时 CPU 容器已自动删除。

原始日志 `results/messages-live-20260907/cpu-full-integration-acceptance.log` SHA256：
`8e24472226beff61b8f63b080a7ba02f1b51e790434a19abc929746c7000a38c`。
这验证了 API 返回正确，不等于已部署常驻服务或完成真实数据的浏览器交互验收。
本地 `http://127.0.0.1:4183/?demo=1` 是 UI 演示预览，不冒充上述数据库的实时结果。

提交前补跑 Windows 全仓 `tests/unit` 未完成：运行停留于既有 F1-C 夹具的
`git config user.email` 子进程，随后中止该轮测试。原因尚未确认，不能计为全仓通过；
以上定向结果仍有效，全仓结果以当前 PR 的 Linux CI 为准。Windows CI 也已补入新的
Messages/inspection 定向测试，POSIX 专属项继续明确跳过。

无 `.git` 的源码归档通过 `HCUOPT_SOURCE_COMMIT` 记录基线 Commit，**该值不代表未提交修改
已包含在 Commit 中**。实际验收内容以代码包 SHA256 为准。

## 数据库调度接线验收（第二切片）

`tests/integration/test_messages_dispatch_postgres.py` 使用真实 PostgreSQL、真实子进程 Runner、
本地 HTTP 模拟服务。每个测试创建随机 `messages_test_<uuid>` schema，所有迁移和状态写入
都限制在该 schema；结束只删除自己创建的测试 schema，不运行旧测试里的共享表 TRUNCATE。

覆盖成功、非法补丁实际用量、上游失败保守计费、结算前中断后的恢复、终态精确重放、
过期租约拒绝、跨 Run/输入篡改拒绝、无 Receipt 的已启动 Attempt，以及超预算租约拒绝。
这些是软件调度证据，不是模型质量、HCU 正确性或性能证据。D 发布和 Web 接线不由它们替代。

2026-09-07 第二切片结果（未提交工作树）：

| 验证 | 结果与边界 |
| --- | --- |
| Windows Worker → SSH 本地转发 → nmz36 已有测试 PostgreSQL | 9 passed，152.43 秒；每例独立 schema，清理后无遗留 |
| Windows Python 3.12，8 组相关回归 | 128 passed，25.18 秒 |
| nmz36 Python 3.10.12，相同 CPU 回归 | 128 passed，6.58 秒；无网络、无 HCU 的临时容器已删除 |
| Ruff / diff whitespace | 通过 |
| 新真实模型调用 / HCU / D 发布 / Web | 本切片没有执行或验收 |

原始日志在 `results/messages-live-20260907/`（不进 Git）：

- `postgres-dispatch.log` SHA256：`5708b0dd491f314ca9db73549d2022404de02bc831b75a7e68e446d842486ff2`。
- `cpu-dispatch-acceptance.log` SHA256：`e898d145d9f72b716062fd031a35831738f693a2f1efe517634cf01933b579f1`。
- `code-dispatch.tar.gz` SHA256：`f56df7a3b51e547e919e942dd95d07500435439753208e4d641e85cfeec112bc`。

远端同一批源码、CPU 日志和执行脚本位于 `/tmp/hcuopt-agent-accept-20260907.byipW5/`。
数据库验收使用 Windows Worker；不能把它描述为在无网络 Linux 容器内跑通 PostgreSQL。

## Messages 到真实 Git 源码制品的组合验收（2026-09-08）

补充三项 PostgreSQL 集成用例，直接消费实际 Messages Dispatch 生成的 Batch/Patch，
不再为 C 接入另造一份状态或 Proposal。只使用本地 HTTP 模拟模型和测试专用审核证据，
不审核部署中的真实 Run，也不把测试审核当成人工批准。

- 成功路径：一次生成两个不同补丁 → PostgreSQL 状态独立重读 → 测试审核 →
  `GitSourceManager` 创建独立 Worktree → 两个源码制品 → 双成员 Family 独立验证 →
  Promotion Receipt 落盘与独立重读。
- 每个制品都重新应用到新的 Git Worktree，校验完整源码 Hash 与未改文件；结束确认
  Baseline 干净且只剩 Baseline Worktree。C 接入不重复调用模型。
- 缺失审核、明确拒绝和 Baseline 漂移均不能发布源码制品。
- Receipt 保持 `formal_intake_allowed=false`、`automatic_release_allowed=false`、
  `performance_conclusion=not_measured`。A 状态仍为 `awaiting_review`；这组用例不覆盖
  最终人工签核或 D 终态发布，不能把审核前 inspection 测试当作终态报告验收。

| 验证 | 结果 |
| --- | --- |
| Linux Python 3.10.12，真实 PostgreSQL + 子进程 Runner + 本地 HTTP 模拟模型 | 18 passed，零跳过，22.89 秒；含上述新增 3 项用例 |
| Windows Messages Generator 与 Proposal Promotion 定向单测 | 33 passed，5.16 秒 |
| 修改文件 Ruff 检查 | 通过 |

沿用已批准的 CPU 隔离范围：一次性容器与既有测试 PostgreSQL 共享网络命名空间，
源码和离线 wheels 只读挂载、只读根文件系统、临时数据写 `/tmp`，无 HCU 映射、
无模型密钥、无 privileged。数据库各例使用独立随机 schema；容器结束自动删除。

运行源码为基线 `051e2ab7ed9c9b9bfaeb55e07c0a0efca9876c44` 加当时未提交的
`tests/integration/test_messages_dispatch_postgres.py`，不是该 Commit 单独通过新增测试。
该测试文件 SHA256：`560ec806d4ecdd049a6a8aa37ce1007749b0edbdc5e2af83389fb4522da2b59b`。
原始日志保存为 `results/messages-promotion-acceptance.log`（不进 Git），SHA256：
`8d62ef54ec68b13ce62f2e57d76f2c13b21b621bd75907244f2e0f02deb8984e`。
两项依赖弃用警告不影响测试通过；本轮没有重新调用真实模型或进行 HCU 优化验收。

## 同批 Messages 输出的 D 终态报告联验（2026-09-08）

在前述双候选源码制品用例上增加四种终态参数：正常读取、Promotion Receipt 篡改、
报告正文篡改、状态快照篡改。它们使用同一用例实际产生的 Review/Promotion 记录，
通过已有 `complete_generation_review`、D Verifier、报告器和数据库 Publication 接口，
没有新建报告格式或修改产品权限。

验证链路：

```text
Messages Dispatch → 两个源码制品与 Family → 测试审核完成
  → 独立读取终态 A 状态 → D 验证 → 报告及清单 → Publication 落库
  → 新 Repository/Reader 独立重读 → 既有 control-plane evidence API
```

- 终态后旧 inspection 快照被拒绝；另建绑定终态状态、Review 和 Promotion 的验证上下文。
- Publication 重复登记保持幂等，API 返回与独立 D 重算一致的两候选结果。
- 三种证据损坏均导致读取失败、API 返回 422，不能返回旧的候选结果。
- 全程只调用一次本地模拟模型；终态读取不修改 A 状态或预算。
- `approved/promoted` 只描述测试记录；仍为 `formal_readiness=hold`、
  `performance_conclusion=not_measured`，Formal Intake 与自动发布均为 false。

Linux Python 3.10.12 + 真实 PostgreSQL：**22 passed，零跳过，30.74 秒**。
Windows 相关单测：**22 passed、4 POSIX-only skipped，5.41 秒**。
仓库 Ruff 与 diff whitespace 检查通过。沿用前节已批准的 CPU 隔离范围，
测试结束临时容器已自动删除，未调用真实模型或访问 HCU。

运行源码为 `4a4971768a891aa7ca5fb99f1e82f8d16c08ba86` 加当时未提交的集成测试文件；
测试文件 SHA256：`473032fd6466222047383bb71e5aba12db8a3edce67df2d5747e6df9ca656abc`。
日志 `results/messages-terminal-acceptance.log`（不进 Git）SHA256：
`e6efef221d456107dd77b4b097bbd242a7b6804e3be7f4d723c459c0b07530ae`。
早期两轮失败来自测试准备：未创建报告根目录、尝试直接写只读报告；已修正测试，
没有放宽产品报告器的目录检查或只读权限。篡改注入仅作用于 pytest 私有临时目录。

**部署边界：**上述 API 通过进程内 TestClient 调用，未开放网络监听。
当前专用只读服务及本机代理仍只开放 `/inspection`，没有新增 `/evidence` 路由、
授权或数据库权限。因此这不是终态报告的浏览器部署验收；下一个部署切片需把终态
只读接口接入同样的精确 Run 鉴权和 no-store 保护，不能直接暴露完整控制面。

## 手动源文件级试跑

在独立的 Python 3.10 环境安装项目依赖，从仓库根目录运行。事先在进程环境设置
`HCUOPT_MODEL_API_KEY`，不要把真实值写到脚本或 shell 历史；下面没有任何真实密钥：

```text
python examples/messages_hotspot_probe.py \
  --source-file <已导出的基线文件> \
  --source-path python/sglang/srt/mem_cache/allocator.py \
  --source-commit <已验证的固定Commit> \
  --hotspot <人工选定的热点说明> \
  --base-url http://itokens.sourcefind.cn:10087 \
  --model mmm1-yx-claude-opus-4-7 --allow-http \
  --output <不存在的新输出目录>
```

Unix 用 `PYTHONPATH=src`，PowerShell 用 `$env:PYTHONPATH=(Resolve-Path src).Path`，避免导入其他
checkout。命令不在 nmz36 主机 Python 3.6 下运行，不为它替换系统 Python。

返回码 0 只表示提出了可应用但未审核的补丁；2 表示无候选、输出拒绝或执行失败，不表示
性能回归。原始输出、输入 Hash、执行清理和 token 均保留。若报告写出中断：

```text
python examples/messages_hotspot_probe.py --recover --output <已有输出目录>
```

恢复不需要 key，不连接模型。它独立重读 Receipt、输入与原始回复，不能拿新文本替换原回复。

## 测试

`tests/unit/test_messages_generator.py` 覆盖输入/Plan 绑定、缓存 token、跨 Attempt 拒绝、
补丁越界/上下文错误、伪造权威字段、零候选、重复 JSON、截断/工具输出、凭据回显、跳转拒绝、
同一 Claim 防重调用、报告无调用恢复，以及本地 HTTP stub + 真实子进程 + Receipt/Batch。
HTTP stub 测试是软件接线测试，不是大模型生成质量或 HCU 性能证据。

### 本切片验收记录

代码基于 main `71bd572d6db4b62c2865f9d5a521159135b78de7`，开发分支
`feat/m2b-live-proposal-intake`。本记录对应未提交工作树，测试源码包作为内容固定凭据。

| 检查 | 结果 |
| --- | --- |
| 新生成器定向测试 | 23 passed |
| Windows Python 3.12，8 组 Agent/Runner/Knowledge/Promotion/Verifier/CLI 回归 | 126 passed，25.02 秒 |
| nmz36 Python 3.10.12，同组 CPU 回归 | 126 passed，6.58 秒 |
| 仓库 Ruff、diff whitespace 检查 | 通过 |
| 新增真实 PostgreSQL Worker 集成 / Web / HCU 测量 | 未验收，不由上述测试推定通过 |

nmz36 使用预先批准的一次性 CPU 容器，镜像 ID：
`sha256:71c8604c255a9a20631eef68e487ed08c12100d722c6e09fe4dd153b722240be`。
启用 `--init --network none --read-only --cap-drop ALL --security-opt no-new-privileges`，
限制 2 CPU/4 GiB/256 PID，源码与离线 wheels 只读挂载，依赖和测试目录位于容器 `/tmp`，
没有 HCU 映射且验证 `/dev/kfd` 不存在。测试后 `--rm` 删除临时容器；没有停止其他容器或进程。

可在 nmz36 查看 `/tmp/hcuopt-agent-accept-20260907.byipW5/cpu-acceptance-final.log`。
原始日志和模型证据归档也保存在当前 checkout 的 `results/messages-live-20260907/`，不进 Git。

- CPU 日志 SHA256：`00aad98ca64cbddf5fa65efbf8de1a92919c6f1c4822cde3e56d9196ac0425c2`。
- 最终测试源码包 SHA256：`68019717bd17662f6bda4a48f39a27622585cc23d35a80a6a4ccb6275558a41b`。
- 模型试跑证据归档 SHA256：`5c6f877c015df7f06b0cd32f9869af1fc765aed8eb71bd66f1c2ffa41497efd5`。

模型证据归档保留了原始 Windows URI，不为迁移目录重写 Receipt Hash。跨机器查看归档内容
不等同于完成原路径 Store 重读；恢复需保持原路径或另建经过验证的迁移流程。

## 已观察到的真实服务结果（2026-09-07）

连通性检查通过。随后对 nmz36 固定 Commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1` 的
`python/sglang/srt/mem_cache/allocator.py` 做了一次真实源文件级调用，源文件 SHA256 为
`ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27`，本地与远端一致。

- 返回模型名 `claude-opus-4-7`；Runner 成功，约 113.11 秒，总用量 10,199 token，清理 verified。
- 模型提出 `page_size == 1` 的特殊路径，但 diff 上下文与基线不符，C 解析器拒绝。
- 建议还依赖未验证的输入唯一性假设；没有人工批准，没有可接受的优化效果结论。
- 调用发生在 Windows CPU Worker；nmz36 只提供只读基线，Linux CPU 回归另行记录。
- 探针报告序列化曾失败，已通过保存的 Receipt 恢复；没有为补报告重复调用模型。
- 没有修改基线、占用 HCU、创建 Formal Round 或自动发布。

这份负面结果说明真实生成器已经能被调用，且非法补丁不会因为“模型调用成功”而被当作合格
候选。它不证明热点优化成功，也不替代后续真实调度/独立评审/实机验收。

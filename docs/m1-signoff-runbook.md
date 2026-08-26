# M1 人工签核 Runbook

## 目的与边界

本 Runbook 用于人工接受或拒绝一份已经由 D 独立裁决的 M1 EvidenceBundle。签核只回答：
“项目是否接受这份冻结证据作为本次 Task 的权威记录”。它不执行 HCU 测量，不安装 Overlay，
不提升 Baseline，不声明端到端收益，也不授权生产灰度或自动发布。

M1 Signoff 必须始终保持：

```text
Evidence verification
→ Human decision
→ Idempotent API write
→ Task/Candidate terminal state
→ automatic_release_allowed=false
```

首个真实签核实例见
[M1 nmz36 Formal 与签核记录](evidence/m1-formal-nmz36-20260825.md)。

## 角色

| 角色 | 签核前责任 | 不得代替的责任 |
| --- | --- | --- |
| A | 核对控制面身份、状态、不可变 ID 和审计写入 | 不重新解释 B 的原始样本或 D 的统计结论 |
| B | 确认 Measurement、Lease/Fencing 和清理证据可重读 | 不写 `faster/slower/inconclusive` |
| C | 确认 SourceSnapshot、Artifact Hash、只读属性和 Worktree 清理 | 不把实验分支替换成已评测 Artifact |
| D | 独立重读正确性与性能证据，给出最终 verdict | 不替项目所有者做接受决定 |
| 项目所有者 | 阅读完整 EvidenceBundle 后批准或拒绝 | 不把签核解释为发布授权 |

## 0. 前置停止条件

出现任一情况时停止，不调用 Signoff API：

- Task 或 Candidate 不在 `awaiting_signoff`；
- EvidenceBundle 不是该 Candidate 最新且唯一的裁决输出；
- `synthetic=true`、Fake provenance 或 `automatic_release_allowed` 不是 `false`；
- Target、Stage0Run、Baseline、Workload、Source、Artifact、Measurement 或协议绑定不一致；
- 原始 URI 不可读、Hash 不一致、递归证据缺失或保留策略不明确；
- HCU Lease 未释放、Fencing/Health 失败、受管进程或 Worktree 未清理；
- D 的 verdict 为 `invalid`，但请求却准备批准为有效性能结论；
- 当前服务实例的源码身份、Contract 版本或 Adapter Profile 无法确认。

当前 M1 API 的 OpenAPI 版本为 `0.5.0-m1-control-plane`，但尚未原生公开 Git
`source_commit`。在该 P0 接口债务修复前，部署方必须从精确提交启动服务并把提交 ID 写入
签核记录；端口存活或页面可打开不能证明服务身份。

## 1. 固定签核输入

在只读工作表中记录：

| 字段 | 来源 |
| --- | --- |
| API base URL、服务源码 Commit、Contract/OpenAPI 版本 | 部署记录与 `/openapi.json` |
| Task ID、Candidate ID、Baseline Epoch ID | Task Summary |
| Target Snapshot、Stage0Run、Project Mode、Adapter Profile | Task/Baseline 与 Formal 报告 |
| Workload ID/Hash、Configuration Hash、Image Digest | Baseline Epoch |
| Candidate SourceSnapshot、Artifact ID/Hash | Build 结果与 Artifact Manifest |
| Correctness、Measurement、EvaluationRun、EvidenceBundle ID | D 输出与 Task Summary |
| 原始证据 URI/Hash、协议版本/Hash | EvidenceBundle 递归引用 |
| 最终资源状态、Fencing/Health 证据 | B 清理输出与资源表 |

这些 ID 一旦开始签核复核不得更换。发现新执行或新 Measurement 时，应中止本次签核并重新
生成整份复核记录，而不是只替换一个数字。

## 2. 读取控制面状态

```bash
curl --fail --silent --show-error \
  "${HCUOPT_API_URL}/v1/manual-candidate/tasks/${TASK_ID}/summary" \
  > m1-signoff-summary.json

curl --fail --silent --show-error \
  "${HCUOPT_API_URL}/openapi.json" \
  > m1-signoff-openapi.json
```

人工确认：

- `task.state=awaiting_signoff`；
- `candidate.state=awaiting_signoff`；
- `task.automatic_release_allowed=false`；
- `signoff=null`；
- 只有一个逻辑 Candidate；
- 最终 Job 分别为 Build、Correctness、Performance、Adjudication 的成功终态；
- EvidenceBundle ID 与 Candidate 上保存的 ID 一致；
- 不存在晚到 Worker、活动 Claim 或等待写回的重试。

若 Task 已有 Signoff，不创建第二个决定；使用原请求做幂等重放或只读核对。

## 3. 递归验证证据

复核必须从 EvidenceBundle 向外重读，而不是只看 `signoff.md`。至少验证：

1. EvidenceBundle 自身 Hash、`synthetic=false` 和 `automatic_release_allowed=false`；
2. Task、Candidate、Baseline、Target、Workload、Artifact、协议和 Measurement 的不可变 ID；
3. Candidate SourceSnapshot 是干净 Baseline SourceSnapshot 的子快照，Source Hash 匹配 Intake；
4. Artifact 内容 Hash 与 Manifest 一致，内容只读，Overlay import attestation 指向该 Artifact；
5. Correctness 原始文件和 Verification Artifact 可重读且 Hash 一致；
6. Performance 主文件、全部 ABBA acquisition、设备 Event、进程生命周期、缓存证明和原始样本
   可重读且 Hash 一致；
7. D 使用仓库冻结算法重新得到相同 effect、置信区间、MDE 和 verdict；
8. Lease ID、Resource ID、Fencing Token、Fence/Health 和最终空闲状态互相一致；
9. 外部 Profiler Trace、Artifact 等跨根引用也按 URI/Hash 重算，不能因不在裁决目录而跳过；
10. 每个缺失或不一致项都产生明确失败记录，不能把“未检查”写成“通过”。

`signoff.md` 仅是面向人的待签摘要。数据库 Signoff 行和签核事件才是 M1 人工决定的权威；
摘要文件不能作为“已经批准”的证据。

## 4. 形成人工决定

决定只能是：

- `approved`：接受该 EvidenceBundle 作为冻结 Task 的项目证据；
- `rejected`：不接受该 EvidenceBundle，并在 `reason` 中写明证据、适用范围或项目判断问题。

`reason` 应说明接受/拒绝的对象和边界，不写密码、Token、内网地址、临时进程号或无关个人
信息。幂等键对同一逻辑决定保持稳定，例如：

```text
m1-<task-short-id>-signoff-<decision>-<date>-<actor>
```

同一幂等键不得用于不同 EvidenceBundle、decision、actor 或 reason。

## 5. 写入 Signoff

先把请求保存为待审文件：

```json
{
  "decision": "approved",
  "actor": "<reviewer>",
  "reason": "Accepted the independently verified M1 evidence; no release authorization.",
  "evidence_bundle_id": "<evidence-bundle-uuid>",
  "idempotency_key": "<stable-idempotency-key>"
}
```

由有权操作控制面的人员执行：

```bash
curl --fail --silent --show-error \
  -X POST \
  -H 'Content-Type: application/json' \
  --data-binary @m1-signoff-request.json \
  "${HCUOPT_API_URL}/v1/manual-candidate/tasks/${TASK_ID}/signoff" \
  > m1-signoff-response.json
```

服务端必须原子完成：

- 校验 Task/Candidate 均等待签核；
- 校验 EvidenceBundle 是该 Candidate 的裁决输出且不可变绑定一致；
- 写入唯一 `manual_candidate_signoffs` 行；
- 写入 `manual_candidate_signoff_recorded` 审计事件；
- 批准时转为 `Task=completed`、`Candidate=accepted`；拒绝时转为对应拒绝终态；
- 保持 `automatic_release_allowed=false`。

## 6. 幂等重放与签核后核对

使用完全相同的请求再调用一次。两次响应必须返回同一个 `signoff_id`，数据库同一 Task 的
Signoff 记录数仍为 1。然后重新读取 Summary，确认：

```text
approved → Task completed / Candidate accepted
rejected → Task rejected / Candidate rejected
automatic_release_allowed=false
```

同时保存：请求文件、两次响应、最终 Summary、服务源码 Commit、OpenAPI 版本、复核清单、
Evidence 根 Hash 清单和最终资源状态。请求中的 actor/reason 属于项目审计数据，发布到公开
仓库前需要再次检查信息边界。

## 7. 异常处理

| 现象 | 处理 |
| --- | --- |
| `409 conflict` | 重新读取 Summary；检查状态、EvidenceBundle 与幂等键是否漂移，不修改数据库绕过 |
| 幂等键输入冲突 | 保留冲突证据；若确需新决定，先由 A 确认旧决定未写入，再使用新的稳定键 |
| API 超时 | 先 GET Summary 判断写入是否成功，再重放相同请求；不得直接构造第二个决定 |
| Hash 不一致或 URI 丢失 | 停止签核并标记证据无效；恢复原始证据后重新独立复核 |
| 资源仍被占用 | 先完成 Fencing/Health；Signoff 不能代替资源清理 |
| 已批准后想改 reason/decision | 不 UPDATE 历史行；新增撤销/更正机制需要单独 ADR，目前保持历史不可变 |

## 8. M1 与 M2 的分界

本 Runbook 只适用于一个 Task、一个 Candidate 的 `manual_candidate`。M2a 将签核整个候选
家族和 Round Evidence，并要求内容寻址的 `signoff-decision.json`；不得复用 M1 Signoff 表
或把多个 Candidate 逐个走本 Runbook 来绕开 Round Barrier。

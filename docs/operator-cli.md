# OX-1 Scripted Operator CLI

该 CLI 是控制面的受控入口，不实现第二套优化状态机。它调用同一组 Operator API，读取
SearchRound/Candidate/Budget/Evidence 权威对象，只支持 `synthetic Scripted`，不会运行 HCU、
开放 Formal Signoff 或自动发布。

## 部署前置

`hcuopt api` 默认 fail-closed：不配置 Candidate Package Store 时 Preview 会阻塞，不配置 Plan
Authority 时 Start 会失败。Scripted 部署显式提供：

| 环境变量 | 约束 |
| --- | --- |
| `HCUOPT_OPERATOR_PACKAGE_ROOT` | 部署拥有的只读、内容寻址 Candidate Package 根目录 |
| `HCUOPT_OPERATOR_OVERLAY_ROOTS_JSON` | JSON 字符串数组，例如 `["sglang"]` |
| `HCUOPT_OPERATOR_MOUNT_TARGETS_JSON` | replacement point 到容器内只读挂载路径的 JSON 映射 |
| `HCUOPT_OPERATOR_PLAN_SECRET_HEX` | 至少 32 字节的十六进制 D-owned Plan Authority 密钥；不得写入仓库或日志 |

服务还需要 PostgreSQL 中已有匹配的 synthetic Target/Stage 0/Baseline/Hotspot Authority，以及
Package Store 中已有经审核的 fixture Candidate。CLI 不绕过这些前置，也不负责直接写数据库。

## 命令闭环

```text
hcuopt profile list
hcuopt profile show target m2-scripted-target --version 1

hcuopt round plan operator-plan.json --output preview.json
hcuopt round start preview.json --actor operator-a --output start.json
hcuopt round status start.json --output summary.json
hcuopt round report start.json --output report.json
```

如果 Preview 含 warning，Start 必须显式加 `--ack-warnings`；CLI 会提交完整且规范排序的 warning
code 集合。Start 的默认幂等键由 Preview ID 确定，重复命令返回同一 Intent/Round，不重复创建。
如果 StartIntent 未 finalized/executable，`round start` 与 `round run` 返回非零退出码；Start 文件仍
会保存安全错误摘要，`round run` 不会继续伪造 Summary/Report。

一条命令版本：

```text
hcuopt round run operator-plan.json --actor operator-a --output-dir results/operator
```

该命令保存 `preview.json`、`start.json`、`summary.json` 和 `report.json`。用户不需要从输出中复制
Preview ID、Plan Hash、Intent ID 或 Round ID。当前 Start finalized 只表示 Round Authority 与
Candidate Family 已安全建立；刚启动后的 Report 通常是 `interim`，下一动作通常是
`await_build_terminals`，不表示优化已经执行。

四个交接文件使用原子写入。输出目录会绑定首个 `preview.json`：相同 Preview 可安全幂等重放；
如果目录已属于另一 Preview，或缺少 Preview 却残留 Start/Summary/Report，命令会安全失败，避免
把不同逻辑 Round 的文件混在一起。新 Plan 应选择新的 `--output-dir`。

## Plan Spec

`operator-plan.json` 是人类可保存和审查的输入。Profile 只写 ID/Version，CLI 从当前服务读取
精确 Profile Hash 与 Service Identity；Hotspot 和 Candidate Package 仍必须引用上游可信 Intake
已产生的内容寻址证据。

```json
{
  "name": "Scripted Operator Preview",
  "target_profile": {"profile_id": "m2-scripted-target", "profile_version": 1},
  "workload_profile": {"profile_id": "m2-scripted-workload", "profile_version": 1},
  "measurement_profile": {"profile_id": "m2-scripted-standard", "profile_version": 1},
  "hotspot": {
    "source": "profiler",
    "hotspot_id": "<approved UUID>",
    "hotspot_intake_hash": "sha256:<approved hash>",
    "profiler_evidence_uri": "<approved URI>",
    "profiler_evidence_hash": "sha256:<approved hash>",
    "correctness_evidence_uri": "<approved URI>",
    "correctness_evidence_hash": "sha256:<approved hash>",
    "replacement_point": "sglang.fixture.layer_norm",
    "workload_hash": "sha256:<approved hash>",
    "shape": [1, 128],
    "dtype": "float16"
  },
  "candidates": [
    {
      "ordinal": 0,
      "source_package_ref": {
        "candidate_source_hash": "sha256:<approved hash>",
        "source_package_hash": "sha256:<approved hash>",
        "manifest_hash": "sha256:<approved hash>",
        "manifest_schema_version": "m1-candidate-source-v1"
      },
      "optimization_intent": "exercise approved fixture candidate"
    },
    {
      "ordinal": 1,
      "source_package_ref": {
        "candidate_source_hash": "sha256:<approved hash>",
        "source_package_hash": "sha256:<approved hash>",
        "manifest_hash": "sha256:<approved hash>",
        "manifest_schema_version": "m1-candidate-source-v1"
      },
      "optimization_intent": "exercise second approved fixture candidate"
    }
  ],
  "max_promoted": 1,
  "idempotency_key": "operator-preview-scripted-001"
}
```

## Read Model 边界

- OX-1 只提供 Round/Candidate/Build 终态计数、Budget settle 计数、Evidence 可用性和权威
  `next_action` 的基础视图；Resource/Lease/Cleanup、通知、故障引导、递归 Evidence 校验和
  Signoff readiness 属于 OX-2；
- Summary/Report 带固定 `synthetic=true`、`automatic_release_allowed=false`；
- `next_action` 来自现有 `reconcile_scripted_search_round()`，CLI 不自行推导状态机；
- Candidate state、Artifact、失败证据和 Budget ledger 只从权威表读取；
- `conclusion_boundary=synthetic_only_no_real_performance_claim`；
- Scripted Report 永远不提供 Formal Signoff。

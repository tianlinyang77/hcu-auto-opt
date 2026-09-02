# M2a Formal 就绪审计

## 目的与边界

M2a Scripted 已证明多 Candidate 的控制流可以工作，但它不等于真实 HCU Formal 已经可运行。
本审计在不访问 HCU、不创建 Round 的前提下，逐项回答以下问题：

- 真实 Target、Stage 0、Baseline、Workload 和 Hotspot 是否有可追溯的冻结引用；
- Formal Profile、Plan、StartIntent、测量 Adapter、持久化和证据终结路径是否真实存在；
- 2～4 个业务 Candidate 是否已经形成同一热点下的不可变 Family；
- A/B/C/D 是否分别接受进入一次具体 Formal 窗口；
- 项目所有者是否已经授权精确主机、设备、时间窗口、Family Hash 和预算。

审计器是只读门禁，不是执行入口。无论报告内容如何，它都固定输出：

```text
profile_registration_allowed = false
formal_round_creation_allowed = false
hcu_accessed = false
automatic_release_allowed = false
```

`ready_for_window_authorization` 也只表示可以向项目所有者申请一次独立窗口评审，不表示已经
获得 HCU 权限，更不创建 Round 或发布 Candidate。

## 冻结输入

[nmz36 M2a Formal readiness manifest](../config/m2/nmz36-formal-readiness-v1.yaml)
保存审计基线 Commit、Real Profile 草案、历史 Target/Stage0/Baseline/Hotspot 引用、候选预算、
13 个完整 gate 和 A/B/C/D review。当前草案明确保持：

- Profile 为 `draft_unregistered`；
- Candidate Family 为 `missing`，目标成员数为 2；
- 预算为 `pending_b_review`；
- HCU 窗口为 `not_requested`；
- 自动发布为 `false`。

证据使用仓库相对 POSIX 路径，不保存开发机绝对路径。每个路径、`digest_mode` 与 SHA256
一起构成可移植的 Evidence 引用；审计时从显式 `--repository-root` 重新读取文件并计算 Hash。
源码、Markdown 和 YAML 使用 `text_lf`，先把 CRLF 规范为 LF，确保 Windows/Linux checkout
得到同一摘要；需要逐字节保真的二进制证据必须显式使用 `raw_bytes`。绝对路径、`..`、
反斜线、任意路径分量上的符号链接、文件缺失和 Hash 漂移都会 fail closed。

## 运行方法

PowerShell：

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
python -c "import hcuopt; print(hcuopt.__file__)"
python -m hcuopt.cli formal-readiness `
  config/m2/nmz36-formal-readiness-v1.yaml `
  --repository-root . `
  --json
$LASTEXITCODE
```

Bash：

```bash
PYTHONPATH=src python -m hcuopt.cli formal-readiness \
  config/m2/nmz36-formal-readiness-v1.yaml \
  --repository-root . \
  --json
```

退出码 `2` 表示审计正确执行但结论为 `HOLD`，不是 CLI 崩溃；只有
`ready_for_window_authorization` 才返回 `0`。解析失败、证据根无效等安全失败同样返回 `2`，
并在标准错误中给出原因。

报告逐 gate 输出 declared/effective status、证据路径、期望 SHA256、证据类型和重新验证状态。
`blocker_codes` 是机器可消费的稳定阻塞清单；不要仅凭进程退出码丢弃 JSON 报告。

## 2026-08-29 审计结论

当前结论是 **HOLD**。13 个 gate 中只有 3 个历史能力通过，且这 3 项不能升级为 M2 Formal
授权：

| 已验证能力 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| Historical Stage 0 | 历史 nmz36 测量闸门为 `DEGRADED_MANUAL_INTAKE` | 当前环境仍新鲜、HCU 7 已独占 |
| Historical M1 Candidate provenance | 单个业务 Candidate 的 Source/Artifact/证据链曾闭环 | 已有 2～4 个 M2 Candidate Family |
| Scripted Barrier/FWER/Evidence | synthetic Barrier、Bonferroni 和递归证据校验可 fail closed | Formal 数据可写入、可终结或可签核 |

待解决项按责任边界归并如下：

| 责任人 | 当前 blocker | 下一份可验收交付 |
| --- | --- | --- |
| A | Formal Authority、Finalizer 和 Signoff/Outbox 地基已实现；Real Profile、Formal Plan、StartIntent、生产身份认证与 Signer 尚不存在 | 先由 PostgreSQL 17 验证签核恢复链，再单独建设受保护的 Formal 启动和生产签核入口 |
| B | Formal phase-aware Adapter、预算确认和当期 Target Lock 尚未完成 | 不访问 HCU 的 Adapter/计划验证；窗口批准后才刷新 HCU 现场 |
| C | 业务 Family 合同、source family Hash 和 fail-closed Verifier 已实现；仍只有一个历史业务 Candidate | 同一 Baseline/Hotspot/replacement point 的第二个真实业务 Overlay 包、双成员 Manifest 和独立复核证据 |
| D | Formal Barrier/Reveal/FWER/Evidence 表和 Finalizer 已实现；生产受保护 Evidence Root 与 D 的正式接受尚未完成 | 验证 PostgreSQL 17 结果，并对生产 Store、Verifier 身份和递归 Evidence 规则签署接受证据 |
| 项目所有者 | 尚未授权精确资源窗口 | 前四方接受后，另行批准主机、HCU、时间、Family Hash 和预算 |

因此当前不得注册 `nmz36-m2a-formal-v1`，不得把 Scripted 表的 `synthetic=true` 约束改成兼容
真实数据的宽松布尔值，也不得访问 nmz36/HCU 7 来“先跑一次看看”。

C 线的 [业务 Candidate Family 冻结合同](m2a-business-candidate-family.md) 已明确区分 Round
创建前的 `source_family_hash` 和 Intake Close 后的 `candidate_family_hash`。前者让项目所有者
先审阅真实业务包，后者仍按 ADR-0009 绑定 Round 内身份；当前两者都不能因为 scripted fixture
存在而被标记为就绪。

## 更新规则

后续每解决一组 blocker，都应更新审计基线并重新计算相关证据 Hash；形成正式窗口申请时再
冻结为新的 manifest 版本，不覆盖已签署的历史审计记录。Gate 只有在实现、测试和不可变证据
同时存在时才能声明 `pass`；A/B/C/D 的
`accepted_for_formal_window` 必须引用独立签署证据。所有 gate 与四方 review 通过后，仍需在
本 Issue 之外取得一次项目所有者窗口授权，才能建设或调用真实 Formal 执行入口。

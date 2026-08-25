# Codex 项目本地已知问题

## Windows PowerShell 转发 nmz36 复杂内联命令会丢失引号

- Scope: mixed
- Symptom: 通过本地 PowerShell 调用 `ssh github@10.17.1.2 "..."` 时，远端
  `python -c` 或带括号的 `grep -E` 表达式在 Bash 解析前已经失去预期引号，报
  `SyntaxError` 或 `syntax error near unexpected token '('`。
- Evidence: 2026-08-25 读取正式 Stage 0 JSON 的两次内联命令均在解析阶段失败；未启动
  容器、未写证据，也未改变 HCU 状态。
- Cause: 本地 PowerShell、OpenSSH 参数转发和远端 Bash 形成多层解析边界，复杂引号、
  反斜杠及括号无法依赖目测保证原样到达远端。
- Proven workaround: 简单检查只用无正则元字符的 `sha256sum`/`head`/`sed`；需要解析
  JSON 或包含复杂逻辑时，把脚本作为项目文件 `scp` 到隔离 Worktree，再用一条简单
  `ssh` 命令执行。
- Validation: 远端显式运行脚本并校验其 SHA256、退出码和产出文件 Hash。
- Applies to: Windows Codex → nmz36 SSH 的 M1 诊断、证据读取和集成脚本。
- Do not repeat: 不要继续叠加 PowerShell → SSH → Bash → Python 四层内联转义。
- Last updated: 2026-08-25

## nmz36 并发 `hy-smi --showpids` 可能异常退出

- Scope: project-local
- Symptom: M1 连续 HCU 进程归属监控中的 `hy-smi --showpids` 以 `Aborted (core dumped)`
  退出，探针因此 fail-closed，中止并清理受管容器。
- Evidence: Run `20260825-m1-allocator-invariant-v1` 在 2026-08-25 17:08:01+08:00
  写入 `could not continuously verify HCU 7 process ownership`；无不变量证据产生，清理后
  HCU 7 为 0 利用率、0 显存且无受管容器残留。
- Cause: 尚未确认；当时两个外部 `Runner.Worker` 正在运行，可能存在并发管理工具调用，
  但没有证据把异常归因于任一外部任务。
- Proven workaround: 把管理工具异常视为证据失败，不复用部分输出；对 `hy-smi` 非零退出
  最多执行三次有限重试，瞬态失败写入 Telemetry warning，连续失败把完整重试历史写入
  failure evidence 后继续 fail-closed。若 Job 已因这一精确基础设施故障耗尽预算，只能在
  Task/Candidate/Job、错误、前置 Job 和无 verdict/signoff 均匹配时人工授权一次新尝试，
  保留旧 attempts、last_error 和事件历史。
- Validation: Formal Performance 第 4 次使用新 Measurement ID 完整完成 40/40 acquisition、
  400 个原始样本且零 Telemetry warning；D 独立复算得到 `faster`，最终 HCU 7 为 0%/2 MiB、
  Resource available、无受管容器残留。第三次的 37/40 部分证据仍单独保留，未混入成功结果。
- Applies to: nmz36 上需要连续调用 `hy-smi` 的 M1 诊断和正式验收。
- Do not repeat: 不要把监控工具崩溃的部分运行标成可接受，不要清零重试历史，也不要只凭
  清理后的空闲状态补签或复用旧样本。
- Last updated: 2026-08-25

## M1 后续 Job ID 不能从幂等键自行推导

- Scope: project-local
- Symptom: Formal recovery 能找到固定 Task 和 Candidate，却找不到脚本用 `uuid5` 推导的
  Performance Job；真实数据库 Job ID 为另一 UUID。
- Evidence: 控制面数据库中的正式 Performance Job 为
  `7a95738b-b4cd-41bb-88ba-66fa44ab4a1f`，而部署脚本自行推导得到
  `1f86aca7-625f-5107-8ca9-8da22278ac23`；第一次 recovery 被 fail-closed 护栏拦截，数据库
  未发生写入。
- Cause: 当前 `enqueue_job()` 使用数据库入队时生成的 Job UUID；`idempotency_key` 用于唯一
  约束和重放，不是 Job ID 的公开生成公式。
- Proven workaround: 在一次性 Formal recovery 中绑定数据库已产生且人工核对的精确 Job
  ID；通用代码必须通过 Repository 按 Task、Job 类型和幂等键读取，不能重写内部 ID 公式。
- Validation: 部署常量和单测均绑定真实 Job ID；recovery 测试验证只恢复精确的 `3/3`
  telemetry 失败，并且重复执行不增加额度。
- Applies to: M1 Build 后动态排队的 Correctness、Performance 和 Adjudication Job。
- Do not repeat: 不要看到稳定 idempotency key 就假设 Job ID 也是同一字符串的 UUIDv5。
- Last updated: 2026-08-25

## Codex PowerShell 工具会话可能不继承本机 Python PATH

- Scope: mixed
- Symptom: 同一任务前序命令可运行 `python`，后续新工具会话却报告命令不存在；解释器
  文件仍然存在。
- Evidence: `Get-Command python` 与 `where.exe python` 曾无结果，但已确认的 Python 3.12
  安装路径仍为普通文件；后续工具会话恢复了该绝对路径的执行权限。
- Cause: 当前 Codex 工具会话的 PATH 发生变化；具体注入原因未确认。
- Proven workaround: 先用 `Get-Command`/`where.exe` 验证，再直接使用已经确认的解释器绝对
  路径；若某次工具会话仍拒绝执行，保留原命令并在新会话复验，不重装 Python，也不改项目
  依赖。PostgreSQL 集成测试仍交给带 PostgreSQL 17 Service 的 Linux CI。
- Validation: 权限恢复后，同一解释器完成 Ruff 全量检查及 M1 聚焦回归，结果为
  `46 passed, 6 skipped`；跳过项均为本机未配置 `HCUOPT_DATABASE_URL` 的 PostgreSQL 测试。
- Applies to: 本项目的 Windows 本地验证。
- Do not repeat: 不要把 `python` 命令不可见当成测试失败，也不要因此改仓库配置。
- Last updated: 2026-08-25

## Measurement 包顶层重导出 M1 Harness 会形成导入环

- Scope: project-local
- Symptom: 从 `hcuopt.measurement.__init__` 重导出 `M1WorkloadFactory` 后，pytest 在收集
  API 测试时报告 `EvidenceReadError` 来自 partially initialized module。
- Evidence: 导入链为 `evaluation.evidence_reader → measurement.__init__ → m1_harness →
  evaluation.stage0_verifier → evaluation.evidence_reader`，测试尚未执行即失败。
- Cause: `m1_harness` 依赖 Evaluation 的 Stage 0 Reader，而 Evaluation Reader 本身依赖
  `measurement.evidence`；包顶层的 eager import 把原本分离的子模块连成环。
- Proven workaround: `M1WorkloadFactory` 只从
  `hcuopt.measurement.m1_harness` 导入，不在 `hcuopt.measurement` 包顶层重导出。
- Validation: 移除顶层重导出后重新运行 M1 控制面和 D Verifier 测试。
- Applies to: M1 Worker/部署代码新增 Measurement Harness 接线。
- Do not repeat: 不要为了缩短 import 路径，把依赖 Evaluation 的 Harness 类加入
  `measurement/__init__.py` 的 eager exports。
- Last updated: 2026-08-25

## Windows 普通账户无法运行 F1-C 文件语义测试

- Scope: project-local
- Symptom: Windows 全量单测中的 F1-C Source/Builder/Artifact Store 用例报
  `WinError 1314`（无权创建符号链接）或 `WinError 5`（无法删除已设只读的临时制品）。
- Evidence: PR #38 修复验证中，M1 聚焦测试全部通过；全量结果为
  `288 passed, 15 skipped, 14 failed`，14 个失败均来自
  `test_f1c_pipeline.py`、`test_git_source_manager.py`、
  `test_local_artifact_store.py` 和 `test_noop_builder.py`。
- Cause: 测试依赖 Linux 文件语义；当前 Windows 会话没有创建符号链接权限，且只读位的
  删除语义与 Linux 不同。
- Proven workaround: F1-C 正式验证使用 Linux CI；Windows 上只运行明确支持 Windows 的
  测试集合。不要用缩短 `--basetemp` 掩盖权限问题，短路径只能解决路径长度。
- Validation: `tests/unit/test_m1_measurement.py` 在默认 Windows 深路径下为 `8 passed`，
  证明 PR #38 的路径修复不受上述 F1-C 限制影响。
- Applies to: 普通权限 Windows Python 3.10+ 的仓库全量单测。
- Do not repeat: 不要把这 14 个既有 F1-C 权限失败归因于 M1 PR；也不要为跑绿而删除
  symlink/只读语义测试。
- Last updated: 2026-08-24

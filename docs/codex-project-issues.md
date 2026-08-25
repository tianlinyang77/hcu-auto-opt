# Codex 项目本地已知问题

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

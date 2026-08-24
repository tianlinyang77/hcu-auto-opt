# Codex 项目本地已知问题

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

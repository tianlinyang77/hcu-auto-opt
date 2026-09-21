# Formal 当期数据库材料读取验证

关联 PR #169、Issue #167，ADR-0025 仍 Proposed。

## 交付

- 按部署领取身份、Intent、Task、Round、精确 Candidate 读取数据库材料。
- 缺少构建/Frozen Artifact Family、制品关联、非合成标记或封存 Context 时拒绝。
- execute_current_once 从数据库 Reader 取快照，不接收外部传入的三个权威快照。
- 配置 Reader 后，在 B 三个检查点重读；采样后的漂移保留失败回执，不发布成功测量。
- 不自动改变 Round/Candidate 状态，不申请硬件，不自动重试或发布。

## 本轮验证

- 定向单测：91 passed、2 skipped，7.52s；跳过为原有 Windows 符号链接权限用例。
- 范围：test_formal_phase_materials、test_formal_phase_prepare、test_formal_phase_consumer、
  test_formal_execution_checkpoint、test_m2_formal_execution、test_formal_dispatch。
- PostgreSQL：test_formal_phase_materials_postgres + test_formal_phase_journal_postgres，
  **6 passed，64.07s**。使用既有测试实例和随机 schema，临时隧道已关闭。
- 新数据库用例验证越界候选、真实 intake 尚未构建、停止请求均被拒绝；没有写入阶段
  日志或伪造状态推进。其余五项为原持久化阶段日志回归。
- 成功读取、三次重读和采样后漂移路径使用显式内存数据库/Harness/预算夹具，调用真实
  B 代码及本地回执存储；不是 PostgreSQL 成功执行链，更不是物理 HCU 验收。
- Ruff 和 diff 检查通过。无前端改动，没有运行 GitHub CI 或占用 HCU。

## 接下来的实际缺口

1. 正式 Build 回执接线：不能直接使用要求 synthetic Artifact 的 Scripted 入口。
2. 正确性推进、Artifact Family 冻结与封存 Context 的生产串联。
3. 正式预算与执行身份接线：通用预算存储当前要求 queued Job/Attempt。
4. 租约/Target Lock 真实读取、资源恢复及运行中协作停止验证。

数据库中的 non-synthetic 标记本身不是实际构建或制品内容正确的证明。
本次不关闭整体 Issue，automatic_release_allowed=false，继续保持默认关闭。

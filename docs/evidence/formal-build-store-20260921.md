# Formal 构建成功结果原子落库

关联 PR #169、Issue #167、ADR-0025（Proposed）。

## 交付

新增默认关闭的部署侧 PostgresFormalBuildStore。复用现有 SourceSnapshot、Artifact、
BuildResult、BuildTerminal 表和契约，不把 Scripted synthetic 检查改为放行。
在当前领取/授权核验后，把源码快照、制品记录、两个候选状态、Round 状态和审计一起提交。
相同输出重放不新增记录；不同输出不覆盖已归档结果。

## 本轮验证

真实 PostgreSQL 随机隔离 schema，既有测试实例，无新增容器或 HCU 使用。

```text
python -m pytest -q tests/integration/test_formal_build_postgres.py tests/integration/test_formal_phase_materials_postgres.py
```

首轮构建存储 3 passed；补充边界后上述完整组合 **5 passed，57.29s**：

- 成功写入、精确重放、不同结果拒绝覆盖。
- 在最后一条审计写入后注入异常，制品/源码/状态均回滚。
- 停止请求拒绝归档。
- 错误基线父快照、可写文件和内容 Hash 篡改均拒绝。
- 现有材料 Reader 仍拒绝尚未构建的 intake 和越界/停止请求。

材料读取/请求准备/阶段消费三个单测文件回归 **46 passed，3.68s**。
Ruff、diff 检查通过；临时数据库隧道已关闭。
测试构建结果与签名来源为显式夹具，数据库事务和制品文件 Hash 检查真实；
本次没有把真实 Builder 与数据库放在一次联验中，也没有 HCU/正确性/性能验收。

## 边界和下一步

本接口只归档已完成的成功构建。数据库回滚不会删除之前发布的制品文件。
仍需构建 Worker 的授权/执行日志/预算结算/失败证据，以及正确性、Family 冻结衔接。
不开放 Web 执行权限，不自动发布，不关闭总 Issue；GitHub CI 按用户要求不重跑。

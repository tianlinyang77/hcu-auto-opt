# 真实 Builder → PostgreSQL 联合验证

## 结果与范围

在既有 Linux 容器、Python 3.10.12、PostgreSQL 随机隔离 schema 上运行。
新增测试实际建立 Git 基线和源码包，调用真实源码管理、Overlay 构建、内容寻址制品发布，
再通过 Formal Job/预算/日志/原子入库链路。没有模拟 Builder 输出。

检查：制品字节和 Hash 匹配、只读、候选 worktree 清理、基线 Hash 未变且 Git 干净、
Job succeeded、一次 Artifact 和一次预算结算、原输入重复调用返回相同结果。

首次功能联验发现发布层将候选 Commit 与基线 Commit 要求相等，真实 Builder 创建子提交
因此被误拒。修复为核对真实直接父提交与 Git Tree，保留其他来源与内容约束。
新增拒绝基线原 Commit 和错误 Tree 的回归，原数据库夹具也改为真实 Git 父子对象。

Linux 组合初轮 **24 passed / 24.01 秒**：真实全链 1、Job 隔离 2、日志/结算 7、
制品发布 6（含 2 项新 ancestry 拒绝）、原 Formal Builder 8。
最终追加基线 Git 干净断言后的完整复跑：**24 passed / 25.62 秒**。
本地相关单元 **80 passed**；Ruff 通过。不是全仓测试或 HCU 验收。

## 复现

在 Linux/Python 3.10，安装项目及测试依赖，提供仅用于测试的 HCUOPT_DATABASE_URL。
若用不含 .git 的归档，须提供实际基线提交 HCUOPT_SOURCE_COMMIT，另保留归档 Hash 区分未提交测试改动。

```sh
python -m pytest tests/integration/test_formal_real_builder_postgres.py \
  tests/integration/test_formal_job_lane_postgres.py \
  tests/integration/test_formal_build_journal_postgres.py \
  tests/integration/test_formal_build_postgres.py \
  tests/unit/test_formal_candidate_builder.py -q
```

测试归档以 067f9dc 为代码基线加本次工作树改动，SHA256：
`85d16d9ef7bdd60796b7ab1e6acb2694fab145d9028e9e321224bba0711c5420`。
只清理各例自己创建的 schema；未清空共享表、未启动或停止已有服务、未访问 HCU。
Windows 已知只读临时 hardlink 清理问题未改，本联合测试显式限定 Linux。

## 后续：双候选构建与 Artifact Family 冻结

联合测试已扩展为两个真实文件候选，分别创建 Job、预算预留、调用日志、制品与结算。
首个候选完成时提前冻结被拒绝；两个候选完成后复用现有
`freeze_search_round_artifact_family`，按持久化成员重算集合摘要，进入 `correctness`。
验证重复冻结只产生一个冻结事件、错误集合 Hash 被拒绝、两个 Job 均成功，
且 `automatic_release_allowed` 仍为 false。未新增生产开关或另一套冻结算法。

Linux/Python 3.10/PostgreSQL 同组回归 **24 passed / 26.75 秒**，Ruff 通过。
该次运行后仅对测试中一个断言折行，不改变执行语义。
这证明整轮真实构建能够衔接正确性阶段入口，不证明正确性执行或性能评测完成。

## 后续未覆盖

测试用短 Python 源码不是业务优化候选；授权、热点和 Stage 0 为明确测试夹具。
未加载模型、未做 SGLang 正确性/性能测试、未自动冻结 Family、未签核或发布生产候选。
下一步仍需正确性/评测接线、有界执行及未知失败恢复，然后实机单轮验收。

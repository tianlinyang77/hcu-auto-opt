# M2a 无 HCU 工程阶段验收（2026-09-07）

## 结论与验收范围

本阶段按项目所有者确认的范围收口：**无 HCU 工程闭环、代码合入与阶段验收报告；实机另行安排。**

工程代码基线为 `4dbbfb831a5332095f8879dde8ec268137ba20ac`（main，PR #145）。
[主分支 CI 34079614134](https://github.com/tianlinyang77/hcu-auto-opt/actions/runs/34079614134)
六项全部通过：Linux 全量单测/Ruff、Windows 定向单测、PostgreSQL 17、双平台 Scripted Smoke、Web。
本阶段的接口、不可变持久化、故障恢复和独立验证链路验收通过。

这里的闭环是部署组件与确定性测试夹具组成的工程闭环。CI 中使用真实 PostgreSQL 和实际服务实现；
HCU、原始测量输入及签名设施由已有测试夹具提供。没有运行真实 HCU Formal Round，
没有生产 Registration、密钥或真实签署的 D Review，也没有新的性能结论。

## 交付与证据矩阵

| 子系统 | 已验证能力 | 主要验收入口 |
| --- | --- | --- |
| A：控制面 | 冻结 Preview/授权/Family，启动身份绑定、幂等与恢复、鉴权只读状态；未配置生产 Verifier 时拒绝 | `tests/unit/test_formal_operator_start.py`、`tests/integration/test_formal_start_intent_postgres.py` |
| B：执行接口 | Search/Holdout 隔离，Lease/fencing/窗口检查，预算 reserve/settle/release，超时和清理失败留证；执行 Profile 注册不可改绑 | `tests/unit/test_m2_formal_execution.py`、`tests/unit/test_formal_execution_profile_registry.py` |
| C：输入 | 两成员业务 Family 的不可变源码/制品引用、独立验真和跨组件身份绑定 | `tests/unit/test_m2_business_candidate_family.py`、`tests/unit/test_m2a_business_candidate_family_verification.py`；历史真实源码验真见 `m2a-business-candidate-family-20260902.md` |
| D：终态验证 | 递归重读 Context、Family、Barrier、Reveal/FWER、Bundle、Owner Signoff；零晋级与 Holdout 两条终态；错误身份和篡改拒绝 | `tests/unit/test_formal_evidence_acceptance.py`、`tests/integration/test_m2_formal_authority_postgres.py` |
| D：终态持久化 | Snapshot Registry → 递归服务 → 签名 Review 存储 → 新 Repository/Registry 实例 → 鉴权 HTTP Report；接受和 blocked 记录均可重读 | `tests/integration/test_formal_evidence_acceptance_postgres.py` |
| D：启动持久化 | 密封计划注册按 Preview 唯一绑定，重算 Hash，核对授权/Plan；重启后的 Registry 可供实际 D issuer 消费 | `tests/integration/test_formal_evaluation_registry_postgres.py`、`tests/unit/test_formal_evaluation_registry.py` |
| 横向边界 | SQL append-only、并发幂等、索引/载荷漂移检查、API 缺鉴权返回 503/拒绝返回 403/证据无效返回 422 | 上述 PostgreSQL 用例及 `tests/unit/test_formal_evidence_acceptance_report_api.py` |

表中测试覆盖的是各模块与组合链路，不把分散用例描述为已完成一轮真实端到端优化。
Agent/Apex 仍只负责开发环境的候选生成与编排，Skills 作为知识输入；测量、独立裁决、签核和发布边界不变。

## 本次收口的合入记录

| PR | 内容 | 合入提交 |
| --- | --- | --- |
| [#142](https://github.com/tianlinyang77/hcu-auto-opt/pull/142) | 递归 Formal Evidence 接受与结构化 D 签名 | `a50065e` |
| [#143](https://github.com/tianlinyang77/hcu-auto-opt/pull/143) | Snapshot/Review 持久化与鉴权 Report；迁移 20 | `5aebdc0` |
| [#144](https://github.com/tianlinyang77/hcu-auto-opt/pull/144) | PostgreSQL 持久化整链重启验收 | `ab18339` |
| [#145](https://github.com/tianlinyang77/hcu-auto-opt/pull/145) | D pre-start Registry；迁移 21 | `4dbbfb8` |

主分支 CI 已包含上述全部改动。迁移只增加 Schema，不创建生产注册数据；部署通过现有迁移入口应用。
最初 #143 的 CI 检出了 Repository 接线导致的证据 Hash 漂移；复核后更新引用，保留原有 HOLD Gate，
随后 Linux、Windows 和 PostgreSQL 均通过。不是通过减少断言或将 blocker 改为 pass 来消除失败。

## 可重复验收

在仓库根目录使用 Python 3.10 和安装了项目依赖的独立虚拟环境：

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest tests/unit -v
```

PostgreSQL 测试必须指向**可清空的独立测试数据库**，不能使用生产库；测试会清空部分表。
将 `HCUOPT_DATABASE_URL` 设为该测试库连接串后运行：

```bash
python -m pytest tests/integration -m postgres -v
```

快速聚焦本次持久化闭环：

```bash
python -m pytest tests/integration/test_formal_evidence_acceptance_postgres.py tests/integration/test_formal_evaluation_registry_postgres.py -v
```

readiness 审计（应返回 HOLD，退出码 2 表示门禁未放行）：

```bash
python -m hcuopt.cli formal-readiness config/m2/nmz36-formal-readiness-v1.yaml --repository-root . --json
```

本地如果存在多份 checkout，先确认导入的是当前仓库。Windows 可在当前 PowerShell 中设置
`$env:PYTHONPATH=(Resolve-Path src).Path`。本次本机缺少 PostgreSQL，数据库运行证据来自上方 CI，
不能把本地跳过的数据库测试算作本地通过。

## Formal readiness 与下一阶段交接

在上述代码基线上重跑现有审计，32 个唯一仓库证据引用全部 verified；
审计 ID 为 `nmz36-m2a-formal-readiness-20260904-v4`，总体仍为 `hold`。
该审计是既有部署 readiness 快照，不能因本报告的工程验收而改写为已获得实机接受。

保留的 blocker：`formal_authority_persistence`、`formal_evidence_finalizer`、
`formal_measurement_adapter`、`formal_operator_profiles`、`formal_plan_compiler`、
`formal_start_intent`、`round_signoff_outbox`、`target_lock_refresh`、
`review_a_pending`、`review_b_pending`、`review_d_pending`。
这些状态记录的是部署与独立接受尚未完成，不表示同名模块都还没有实现。

后续由 #102 汇总，#126 跟踪真实执行部署；本报告不关闭这些实机/部署闸门：

1. 部署侧准备身份、鉴权、Signer/Verifier 密钥托管、protected Evidence Root、最小权限数据库账户和注册数据；执行与 D 写权限分离。
2. 冻结实际 Profile/Family/协议/预算，形成当期 readiness 与各方独立接受证据；启动前签发和终态 D Review 分别处理，不能拿未来的终态 Review 替代启动许可。
3. 项目所有者另行授权精确主机、HCU、CPU/NUMA、时间窗口、Family Hash 和预算。不能沿用历史临时 HCU 7 放行。
4. 获得对应权限后再进行当期 Target Lock refresh、真实 Lease/fencing/恢复接线及 Formal 实机验收；任何一项不满足均继续阻塞。

全程保留 `formal_round_creation_allowed=false`（当前 readiness）、`hcu_accessed=false`、
`owner_window_authorization=not_granted`（D Review）和 `automatic_release_allowed=false`。

## 可转发简版

M2a 无 HCU 工程阶段已完成代码合入和验收：控制面启动与恢复、执行约束、候选 Family、
独立递归验证、不可变注册及签名评审存储、鉴权只读报告已形成可测试链路。
主分支六项 CI 全绿，PostgreSQL 实测覆盖并发、不可改绑和重启后的报告查询。
下一阶段安排真实部署与独立授权窗口；当前没有 HCU 性能结论，也未开启自动发布。

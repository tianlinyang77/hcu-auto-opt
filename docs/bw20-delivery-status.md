# BW20 功能链路交付索引

本页是当前状态入口。其他调研/验收文档保留历史过程；下文索引优先于旧文档的待办描述。
本交付是范围受限的 Framework Smoke，不是完整自动优化系统验收。

## 已完成与明确未完成

| 层次 | 当前证据 | 边界 |
| --- | --- | --- |
| 固定源与运行时 | 完整 Python 源码/wheel/安装目录差异核对、真实 No-op Source/Build/Store | 不代表底层 native 库完整重编来源已证明 |
| 正常 API 功能链 | 原 Worker/Coordinator、真实容器、原 D、数据库，最终 awaiting_signoff | No-op 归档仅挂载，不是候选代码生效 |
| 证据回放 | 外部固定收据 Hash、逐文件重读、原始输出/容器/清理核对 | 历史证据核验，不是再次运行或实时资源授权 |
| 结果页 | 单任务只读身份、真实数据库读取、实例级启动/状态/停止 | 本地服务，不是完整多用户操作台或签核授权 |
| BW20 优化 | 独立目标和窄策略已有 | Stage0、候选激活/恢复、性能接受与人工签核仍未完成 |

## 审查顺序

1. `workers/sdk.py` 和 `test_worker_completion_heartbeat.py`：先停后台心跳，再同步核对所有权和完成，修复任务已提交终态后心跳误报失租的竞态。
2. `bw20_runtime_binding.py`、`bw20_build_worker.py`、`bw20_build_transport.py`、`bw20_pair_prepare.py`：固定输入、原协议与来源关系。
3. `adapters/bw20_execution.py`、`bw20_pair_bridge.py`、`bw20_admission.py`、`bw20_profile.py`：单设备范围、租约、清理、显式准入，不默认注册、不放宽旧 Target。
4. `bw20_evidence.py`：独立核验入口；保留失败记录，不能把新成功覆盖成旧任务成功。
5. `framework_smoke_viewer.py`、`viewer_service.py`、Web FrameworkSmokeInspection：只读投影、访问/管理权限分離、清理与凭据轮换。

远端 bridge/build 校验现用显式异常，不再依赖可被 `python -O` 关闭的 assert。
这项加固通过本地优化模式回归，尚未再次在 HCU 上执行；不得把此前实机日志声称为本次全部代码逐字节复验。

## 阅读入口

- [正常 API 实机验收结果](bw20-api-acceptance-result.md)：顶部为成功任务，下文保留首轮失败与恢复历史。
- [Python 运行时来源](bw20-python-source-parity.md)。
- [证据回放命令](bw20-evidence-replay.md)。
- [结果页部署与停止](framework-viewer-deployment.md)：凭据从外部注入，严禁写入 Git。
- [人工签核下一切片](framework-viewer-signoff-integration.md)：计划，不冒充已经实现。

## 四步执行顺序

1. 交付当前源码、测试及文档，通过 PR 审查和 CI；不直接推 main。
2. 基于原签核 API 实现独立按任务签署身份和显式人工确认；只确认 Framework Smoke 功能链。
3. 在当期获准资源窗口内完成 BW20 测量可信度和候选真实加载/恢复，不能继承旧机器验收。
4. 冻结一个热点，由 Agent 产候选，经原 Intake/Build/Harness/D 路径验收，在页面呈现接受或拒绝的依据。

步骤 2 不依赖重跑已通过的 No-op；步骤 3/4 需要当期目标/协议/预算和资源检查。
Skills 仅为知识输入，Agent/Apex-like 不具备测量判决、签核或发布权限。

## 部署与回退

本 PR 未增加默认生产 Profile、数据库迁移或自动执行入口。旧目标行为保持原路径。
禁用显式 BW20 Profile/停止自己的托管结果页即可退出该切片；保留现有数据库和证据。
结果页只绑定 loopback，默认只读。现场原始证据和秘密仍保留本地受控目录，不随 Git 分发；
文档中的 results 路径用于现场追溯，普通 clone 不自带原始证据或部署凭据。

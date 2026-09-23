# Formal 正确性异常核查与补账

补账入口供持有当前 Job journal 的可信部署进程使用。网页仅提供受保护的只读核查。
不接受外部提交的正确性判决、清理成功布尔值或新的资源身份。

## 操作

```python
from hcuopt.storage.formal_correctness_recovery import PostgresFormalCorrectnessRecovery

recovery = PostgresFormalCorrectnessRecovery(journal)
report = recovery.inspect(input_hash)
# 展示或保存 report；其中没有 claim token，也不导出原始结果载荷。

if report["reconciliation_allowed"]:
    result = recovery.reconcile_known_result(
        input_hash,
        expected_snapshot_hash=report["snapshot_hash"],
        requested_by=operator_identity,
        request_id=stable_recovery_request_id,
    )
```

`journal` 必须来自原任务的部署绑定；不要根据网页传入字段拼装 Owner。
`operator_identity` 由可信部署传入，不是此模块实现的登录认证。
`input_hash` 取原 invocation 记录；不是重新拼一份输入计算的新 Hash。

## 状态含义

| 状态 | 可做的事 |
| --- | --- |
| `not_invoked` | 尚无执行记录，不走结果补账；不能据此自行重领资源 |
| `invocation_unresolved` | 可能仍在运行，也可能中断；不能自动重试 |
| `unknown_requires_manual_recovery` | 结果未知，保留资源占用与预留预算 |
| `result_ready` | 已留存结果，复用原 finalizer 检查归属、处理清理证据并结算 |
| `settlement_pending` | 已记录释放，只补原预算账和 Job 完成状态，不再碰设备 |
| `completed` | 原结果幂等返回；不代表候选被接受或性能提升 |
| `ownership_or_budget_conflict` / `inconsistent` | 状态冲突，拒绝补账，人工核查 |

补账先比较状态快照 Hash 并记录操作者、请求 ID、输入 Hash。
同一请求失败后使用完全相同的参数重试：即使第一次已释放资源，也不会再执行候选或二次释放。
同一个请求 ID 改操作者或参数会被拒绝。新请求使用过期快照会被拒绝。

## Unknown 的边界

这版只提供核查清单，不提供强制释放 Unknown 的后门。完整恢复必须先证明原执行器
不能再启动任务，再检查精确归属的容器/进程，采集新鲜清理与健康证据，并核算未知用量。
租约过期、停止请求已写入、CLI 退出或历史 `failure.json` 都不能单独证明资源安全。
不把未知用量当作零，不把 Unknown 改成正确性成功，不自动重新生成执行尝试。

此步骤不运行模型、不动频率、不扩大 HCU 授权、不改 Round/member 的判决。
实机恢复仍是后续验收项。

## 只读页面接线

部署方选择原任务 journal 后，显式配置读取器（默认不启用）：

```python
from dataclasses import replace

management = replace(management, recovery_reader=PostgresFormalCorrectnessRecovery(journal))
```

正式启动页面的“查看执行核查”通过 `GET /v1/operator/formal-correctness-recovery`
读取该绑定任务。浏览器不能提交 Job、Owner、input Hash 或清理结果来选择其他任务。
接口复用原独立 Bearer 凭据，重新验证签名、有效期、Intent/Round、计划 Hash 和服务身份；
返回 no-store，查询后页面清空凭据，不写本地存储。

此只读授权不包含补账或资源释放：没有对应 POST 入口，
`web_reconciliation_allowed=false`。未配置读取器、尚无 invocation、凭据失效或绑定冲突
都会显示未知，不回退到演练数据。本地 Vite 预览本身不代表真实后端已经配置该读取器。

# BW20 SGLang 端点延迟采集：6/8 部分结果

- 日期：2026-09-17
- 状态：**计划未完成，仅为探索性证据**
- 目标：比较已签核 Agent Candidate 与 Baseline 在固定 SGLang 短请求上的服务级 E2E 延迟
- 自动发布：`automatic_release_allowed=false`

## 执行范围

预定执行 8 个独立 B-C-C-B 组。每次 acquisition 都重新启动服务并使用独立缓存命名空间，
先预热 20 次，再测量 100 次。冻结工作负载为 Qwen2.5-0.5B-Instruct、5 个输入 token、
8 个输出 token、单并发和宿主 `auto` 频率策略。

实际完成 6 个完整成功组：

- 24 次独立服务启动；
- 24 个独立缓存命名空间；
- 2,400 次有效 measured requests；
- 同一进程内的请求没有被当成独立统计实验。

## 停止原因

组 06（第 7 组）对应：

- Run：`a4c51642-0d3b-5bc5-870b-3ddf1140d68a`
- Job：`18e12908-fc74-5fc4-a129-ca03a2c5f05d`
- Fence：`62`

该组四次 acquisition 的原始文件均已生成，但 Worker 在提交成功之前执行最终资源安全守卫，
得到 `ExecutionSafetyError: BW20 HCU 7 is not idle in the accepted auto window`。控制面因此将
Job/Run 标为 `failed`、Task 标为 `rejected`，并按 `max_attempts=1` 的不重试语义停止。
组 07 未创建，campaign 没有完成。

组 06 没有纳入性能分析。原始文件存在不等于控制面已经接受该组，不能通过事后清理或补写
状态把失败 Run 改成成功。

## 6 个成功组的探索性结果

| 指标 | 结果 |
| --- | ---: |
| Baseline 平均延迟 | 41.9484 ms |
| Candidate 平均延迟 | 42.1421 ms |
| 配对延迟降低 | -0.2865% |
| 近似 95% CI | [-5.5966%, 4.7566%] |
| 结论 | `inconclusive` |

负的“延迟降低”表示 Candidate 点估计略慢约 0.29%。区间跨过 0，而且六个独立组方向和幅度
波动明显，因此没有证据证明 Candidate 在该服务场景更快或更慢。

局部 allocator 微基准约 16.60% 的收益仍只属于其冻结微基准，不能写成 SGLang 服务收益。

## 资源恢复

失败后使用 Fence 62 和一次延迟的独立只读守卫完成 reconciliation。当前控制面记录为：

- Resource：`available`
- owner/job/lease：空
- HCU 7：`auto`、busy 0%、无 KFD 进程
- 本任务容器：无残留

失败 Run 保持 `failed`，没有被恢复流程改写。2026-09-17 再次通过资源 API 与宿主 sysfs
只读复核了上述状态。宿主上三个 2026-08-04 起的 SGLang zombie 进程没有 KFD/HCU 占用，
不属于本次 campaign，也不作为成功证据。

## 证据文件

本地证据目录（默认被仓库忽略，不应把大型原始证据直接提交 Git）：
`results/endpoint-latency-20260917-v1/`。

| 文件 | SHA256 |
| --- | --- |
| `latency-analysis-partial-6-of-8.json` | `c49ebbe7e440eb10bc77266b33aa87190fb5596f393d6fec194bc625ee33bdb5` |
| `raw-evidence-hashes-partial-6-of-8.json` | `46f982e45c735d56766035d09dbedf6ddcdc270d47a923cf833617dee2e12a1e` |
| `group-06-resource-reconciliation.json` | `c24f3e290db46f848faee255a78b62cc2a829509e87817553b7828412ad0e7c6` |

## 结论边界与后续修复

当前结果只能表述为“6/8 计划未完成的探索性端点测量，结论不确定”。它不代表高并发吞吐、
TTFT、TPOT、多卡或其他模型/请求长度，也不是正式 D 裁决。

下一次正式 campaign 不应从第 7 组续跑或替换失败组，而应在修复后重新创建完整计划。修复
方向是在服务进程清理后增加有界的静默期，并要求连续两次独立空闲读数后再提交 Worker
成功；超时仍然 fail closed。不得通过放宽 busy、进程或频率守卫阈值来“跑通”。

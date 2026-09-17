# BW20 SGLang 端点延迟采集：完整 8/8 结果

- 日期：2026-09-17
- Campaign：`endpoint-latency-20260917-v2`
- 状态：**8/8 完成，探索性结论为 `inconclusive`**
- 执行代码：`468f6cdfada36e67194bd0e502e93d444bac0be1`
- 自动发布：`automatic_release_allowed=false`
- 正式 D 裁决：无

## 为什么重新执行

首轮 v1 在完成 6 组后，组 06 的四次 acquisition 虽已生成原始文件，但 Worker 在提交成功
前的单次资源空闲复检失败，按不重试语义停止。v1 的失败 Run 和 6/8 部分结果保持不变，
没有被续跑、替换或混入本轮。

修复保持原 `auto`、busy、VRAM、KFD 进程和设备归属阈值，只在 SGLang 清理后对精确的
“尚未空闲”状态进行最长约 60 秒的有界等待，并要求连续两次空闲。遥测损坏、资源不匹配
或归属不明仍立即 fail closed。

## 执行范围

- 8 个独立 B-C-C-B 组；
- 32 次独立服务启动和 32 个独立缓存命名空间；
- 每次启动预热 20 次、测量 100 次；
- 共 3,200 次 measured requests；
- Qwen2.5-0.5B-Instruct，5 个输入 token、8 个输出 token；
- 单并发、非流式、宿主频率策略 `auto`；
- 统计单位是独立 ABBA 组，不把同一进程的请求当成独立实验。

所有 8 个 Run 均为 `provisional_passed`，Task 均为 `endpoint_provisional_passed`，Job 均为
`succeeded`。Fence 依次为 63–70；每组收尾都记录两次连续空闲、`resource_guard.clear=true`。

## 探索性结果

| 指标 | 结果 |
| --- | ---: |
| Baseline 全部 measured requests 平均延迟 | 40.211056 ms |
| Candidate 全部 measured requests 平均延迟 | 40.211550 ms |
| 配对延迟降低 | -0.000554% |
| 近似 95% CI | [-0.654733%, 0.649372%] |
| 结论 | `inconclusive` |

负的“延迟降低”表示 Candidate 点估计略慢，但差异只有约 0.00055%，远小于当前区间宽度。
置信区间跨过 0，因此该固定短请求场景没有证据证明 Candidate 更快或更慢。

这也说明局部 allocator 微基准约 16.60% 的收益没有在当前 SGLang 服务级 E2E 延迟中形成
可测收益。它不否定微基准结果，但两者的结论范围不能互换。

## 资源终态

Campaign 结束后独立复核：

- Resource：`available`，owner/job/lease 均为空；
- Fence：70；
- HCU 7：`auto`、busy 0%、VRAM 约 2.1 MiB；
- KFD 进程：无；
- 本任务受管容器：无。

## 证据

本地证据索引位于 `results/endpoint-latency-20260917-v2/`，大型逐请求原始证据继续保存在
BW20 目标机，不直接提交 Git。

| 文件 | SHA256 |
| --- | --- |
| `latency-analysis.json` | `479c2a449604d1fe37adf5bac9a8b246d095414ab03af7f3870a89c8e54dcfce` |
| `raw-evidence-hashes.json` | `c6ee73271575f667fe4b55ae9d99908d2e0ef9f14cd303ee1bfe3db1f1b157b6` |
| `campaign-completed.json` | `4789c05f820e0137bec597bc500062a008e0df046020af63b6c567f092ffa01a` |

## 结论边界

本轮是完整但仍属 `provisional` 的端点探索性 campaign，不是正式 D 裁决。它只覆盖固定微小
prompt、单并发、重复 warm-cache 的非流式 E2E 延迟，不代表高并发吞吐、TTFT、TPOT、多卡、
其他模型或请求长度，也不授权自动发布。

若下一步继续验证服务价值，应另建协议测试更可能放大 allocator 影响的 workload，或做并发/
吞吐与 TTFT/TPOT 分解；不得通过重复当前 campaign 直到出现正收益来选择性报告。

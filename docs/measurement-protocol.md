# 测量协议 v0.1

## 所有权

- B 维护唯一 Measurement Harness 代码。
- D 维护采样、Holdout、ABBA、FDR/FWER 与晋级规则。
- D、Profiler 和 E2E Runner 不得自建第二套计时实现。

## Harness 必须支持

- JIT/缓存预热与预热证据；
- GPU 同步语义；
- 批量循环放大短 Kernel；
- 多次重复与原始样本；
- 跨进程 restart；
- 热/冷缓存模式；
- 频率、温度、功耗与后台进程采集；
- 单调时钟和设备计时器说明；
- 置信区间、σ、CV、MDE；
- 机器可读的 MeasurementRecord。

## 分辨率与噪声

计时分辨率是测量链能够可靠区分的最小时间差；σ/CV 是相同对象重复测量的环境波动。候选晋级至少满足：

```text
观察差异 > 有效计时分辨率
观察差异 > 预定义噪声/MDE 门限
统计判定通过
Holdout 复验通过
```

显示更多小数位不是更高的有效分辨率。

## 随机化和隔离

- 候选与基线运行顺序由 D 的计划生成，Harness 只忠实执行。
- Kernel 轮次必须区分 Search Shapes 与 Holdout Shapes。
- 服务级使用 ABBA：基线、候选、候选、基线；每段独立启动并记录环境。
- 同一结果不能同时用于搜索和最终证据。

## 原始记录

每条记录至少包含：

```text
measurement_id, task_id, candidate_id, baseline_epoch
hardware_fingerprint, software_fingerprint, workload_id
lease_id, fencing_token, process_id, restart_index
warmup_plan, sample_plan, raw_samples_ns
temperature, clocks, power, cache_mode
timer_kind, timer_resolution_ns, created_at
```

没有原始样本和环境指纹的“1.06x”不进入 EXPDB 的 Verified Knowledge。


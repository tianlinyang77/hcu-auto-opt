# Formal B 执行器检查点与回执绑定验证

关联 ADR-0025、PR #169、Issue #167。

## 已实现

- 将可注入检查点接入既有 B 执行器的三个实际调用位置。
- 新增 FormalClaimCheckpoint，把请求的任务、轮次、计划、授权、候选绑定到领取记录。
- 已预留预算后停止，仍走既有清理、结算和失败回执，未采样的 Harness 时间为零。
- 回执发布后回读，精确匹配整个请求（包括 Phase/Attempt/Lease/Fence），
  不以同一个 Candidate/Phase 的另一次执行替代。

调用方式（由未来受控 Consumer 装配，不是当前开放的启动器）：

```python
checkpoint = FormalClaimCheckpoint(claims, intent_id, worker_id, claim_token)
outcome = adapter.run(
    round_authority=round_authority,
    formal_authority=formal_authority,
    member=member,
    request=request,
    output_dir=output_dir,
    execution_checkpoint=checkpoint,
)
receipt = adapter.receipt_store.load_for_request(outcome.receipt_ref, request)
```

## 验证

`test_formal_execution_checkpoint.py`、既有 `test_m2_formal_execution.py`、
readiness：**50 passed、2 skipped（4.75s）**。
测试调用真实 B 适配器代码和本地回执存储，Harness、预算、资源清理以及领取存储使用测试夹具。
覆盖三个检查点拒绝、零/一次 Harness 调用、预算结算、清理失败保留失败状态、
成功路径三次检查、回执跨 Lease/Fence/Job/Attempt/成员拒绝、Claim 跨 Intent 绑定拒绝。
跳过项是 Windows 符号链接权限依赖，不计为通过。
加入正式入口、派发状态、迁移等相关回归后：**122 passed、2 skipped（11.67s）**。
Ruff 与 diff check 通过；未改动前端或数据库 Schema。

不是完整 PostgreSQL→物理 HCU 联验，不宣称真实模型采样或新的加速收益。
自动发布仍关闭，既有读取/领取/停止协议未放宽。

## 尚缺

持久化 Phase/Attempt 启动日志、禁止重复物理执行的受控消费接线、运行中的协作停止、
按当前 B 租约验证清理并收口、超时后的恢复处理与生产窗口装配。
执行器检查点不提供物理 exactly-once；检查后到启动前仍有外部竞争。
旧调用者可不传 checkpoint，因此不能把这一增量等同于所有生产入口已受该检查保护。

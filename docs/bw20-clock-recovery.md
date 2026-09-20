# BW20 默认auto时钟恢复接管契约

> 2026-09-14：当前正式验收不修改频率，因此不需要恢复接管。本模块不接入当前Worker，
> 仅保留为将来主动改频方案的故障安全设计。

状态：**本地协调逻辑与模拟故障已验证；没有真实写频Backend、远端部署或硬件恢复验收。**

## 定位

当原测量任务失去租约、进程退出或恢复回读失败时，原任务不得继续写设备。持久
`ClockJournal`保留未结束记录，Worker将资源维持在`quarantined`。恢复接管是独立的
运维动作，不属于Agent/Apex、候选生成、普通Worker清理或自动发布。

`BW20AutoClockRecovery`只负责编排一个注入的Backend，不含sysfs、`hy-smi --set*`、
sudo、权限修改或网络调用。当前没有CLI/API入口，避免未经审核的调用方触发恢复。

## 接管顺序

```text
新恢复授权 + 新fencing token
→ 确认控制面资源仍为quarantined
→ 读取唯一未结束时钟记录
→ 校验它属于confirmed_default_auto_v1
→ 只读确认当前设备身份与原基线一致
→ 在SQLite中原子领取恢复权
→ 再次确认权限与设备身份
→ 若仍为manual则请求恢复默认auto；若已为auto则不写
→ 回读默认auto基线
→ 再次确认权限
→ 日志标记restored，Worker才可能释放资源
```

恢复授权必须与原测量窗口授权不同。恢复回调由部署方在进程内注入，不从任务JSON、
Agent输出或日志字符串中取得。日志中的`authorization_id`是审计引用，不是权限本身。

## 故障语义

- 当前boot、PCI、驱动接口Hash、支持频率表、固定显存或未触及设置发生变化：领取前拒绝。
- 两个恢复者竞争：只有第一个可以将日志原子切到`recovery_claimed`。
- 恢复写入或回读失败：尝试记录`failed`，原操作回到`reconciliation_required`。
- 硬件/回读失败后，若连`failed`落盘也失败：抛出`ClockRecoverySettlementError`，同时保留
  原恢复异常和日志结算异常；日志可能仍为`recovery_claimed`，不得用后一个错误覆盖前因，
  也不得据此释放资源。
- 硬件已经回读为auto但`restored`回执落盘失败：保守尝试结算为
  `reconciliation_required`并抛出原落盘错误，不能仅凭设备瞬时状态宣告恢复完成。
- 恢复进程在领取后崩溃：保留`recovery_claimed`，继续阻塞资源；当前版本不自动抢占。
- 写入后丢失恢复权限：即使设备可能已经回到auto，也不能提交成功，仍需重新核对。
- 原日志是完整策略路径或内容缺失：窄化的默认auto恢复器拒绝处理，不能猜测策略。

回执固定保留`hardware_restore_verified=false / stage0_accepted=false /
automatic_release_allowed=false`。只有将来已审核真实Backend的实机记录才能形成硬件恢复证据，
且仍不能单独代表Stage0通过。

## 当前验证

- 恢复/日志/Worker守卫聚焦回归：**58 passed**。
- 与Stage0适配器、bootstrap、时钟事务组合回归：**157 passed / 1既有Starlette警告**。
- Ruff：通过。

测试覆盖manual恢复、已经auto的无写恢复、旧授权拒绝、隔离/权限检查、合法身份漂移、
不完整日志、恢复未生效、并发领取、写后失权、领取后崩溃，以及成功/失败结算日志异常。
所有Backend、控制面与设备
均为测试替身；没有连接远端、没有HCU容器、没有修改频率、没有Target变更。

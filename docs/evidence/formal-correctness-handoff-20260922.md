# 正确性到 Search 的批级交接（2026-09-22）

## 简要进展

补齐 Formal 正确性任务已完成、但 Round/成员仍停留在 correctness/built，导致 B 性能入口拒绝执行的状态交接断点。交接重读 D 原始证据，要求整批完整、资源已清理释放、预算已结算，再原子推进。不是绕过 B 租约和授权，也不等于实际性能验收通过。

## 实现与边界

入口为 `FormalCorrectnessDriver.handoff_family(entries=[(原 journal, D adapter), ...])`。

1. 复核当前控制面 Claim、Owner/Token、窗口及停止请求，拒绝其他 driver 的 journal。
2. 要求每个 built 成员恰有一个已完成结果、释放事件与 settled 预算，健康记录必须明确 healthy=true/quarantined=false；unknown、缺成员、重复成员均不放行。
3. 使用既有 D verifier 重读原始文件和已保存的 verification artifact；二者须一致。数值 incorrect 保留失败成员；invalid 证据不进入下一阶段。不调用 producer，不重新跑候选。
4. 整组核对完成后，在单事务内重验授权、冻结成员集合和每个持久化 payload；资料读取复用该事务，不用第二个连接等待自身持有的锁。
5. 成员分别推进 correctness_passed/failed；Round 推进 search_measuring，同时写一条带证据摘要的交接事件。提交前任意失败全部回滚，不留下半批通过。
6. 同组重放读取既有交接记录，并复核原 Job Owner 与已完成事实，不重复执行或更新；组内容不同拒绝。

search_measuring 在这里表示控制面阶段，不表示 HCU 已开跑。B 的独立执行适配器仍须检验签署的授权、sealed Authority Context、实际租约与 Target Lock。
该入口不启动性能进程、不执行最终 D 裁决、不写人工签署，也不允许自动发布。

## 验证方法

- CPU 原始证据测试：调用真实 M1 adapter/verifier，明确使用 CPU 证据夹具；覆盖数值一致、不一致、改判决、坏 Hash、synthetic 拒绝，证明交接不再次调用 producer。
- Linux/PostgreSQL：隔离 schema 内真实源码构建、制品发布、完整双候选正确性任务日志、预算和资源释放后交接；覆盖成功/重放、缺成员、停止、审计异常全事务回滚、坏证据拒绝、复验期间绑定漂移拒绝。
- PostgreSQL 交接测试的数值判决是显式存储夹具；原始数值复验另由 CPU 测试覆盖。这不是一套真实 HCU 整链实验。
- 测试使用工作树源码快照，不将未提交改动描述成某个已提交 SHA 的原样验收。

最终回归：Windows 本地相关单元 38 passed；Linux/Python 3.10/PostgreSQL 组合 145 passed（92.36 秒）；Ruff 和 git diff --check 通过。远端只使用既有 CPU 测试容器与随机隔离 schema，未调用真实模型或 HCU。

## 真实候选核对

同日只读查询 BW20 源库发现两条旧候选（accepted/rejected 各一条），但源码 Hash 同为
`sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2`，制品 Hash 同为
`sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a`。
这是同一个独立源码候选在旧任务中的记录，不能凑成当前 Formal 契约要求的 2–4 个不同候选。
没有改写旧结果、造第二个候选或把旧 Stage0 记录无条件作为当期闸门。

## 剩余工作

需要形成真实且去重的新 Candidate Family，装配当期签署的 compiler/B/D 权威配置，再贯通实际 Search/Holdout、最终裁决与页面。身份密钥已初始化，但不能由此推导窗口批准或实机结果。
本次不声明整体 MVP 已完成；数据库升级、身份初始化和这里的控制面交接分别有独立证据。

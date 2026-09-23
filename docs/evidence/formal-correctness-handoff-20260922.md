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

## 后续修复：轮次隔离与 Windows 制品发布

简要结论：交接历史现在按 Task + Round 查询，避免其他轮次的记录误阻塞本轮；制品和构建缓存在 Windows 使用不覆盖已有目标的原子 rename，解决只读临时硬链接删除时报 WinError 5 的问题。Linux 保留原硬链接发布路径。

详细范围：

- 新增 PostgreSQL 测试：同一任务存在其他轮次历史时，本轮交接和重放正常；当前轮次存在重复审计记录仍拒绝。
- 新增制品测试：目标已存在时不覆盖、不改变其只读状态；发布异常清理私有临时文件；缓存可重复读取，冲突仍拒绝。
- Windows 定向单元测试 39 passed，Ruff 通过。扩大到 Git Worktree 构建测试时，独立重跑仍卡在 Git 子进程的输出读取，已终止本次测试进程；不将 Windows 完整构建回归记为通过。该构建链路在 Linux/PostgreSQL 回归中另行覆盖。
- 最终 Linux/Python 3.10/PostgreSQL 工作树快照组合回归：153 passed（101.37 秒），包括真实隔离源码构建、缓存复用、当前新增测试及原交接回归；仅有两条依赖弃用警告，不是 HCU 性能验收。
- 没有模型调用或 HCU 启动，没有修改频率、他人容器或签署验收。临时问题及重跑方式记录于本地交接目录，不上传私有材料。

旧 Agent 草稿仅供诊断：一份补丁引用 Baseline 中不存在的辅助函数；另一份将“无相邻重复”误当作“全局唯一”，例如页序列 `[1, 2, 1]` 会漏去重。这不是冻结 page-contiguous 范围内的失败证据，但说明它没有一般性安全证明，不能直接充当已评审的第二个业务候选。未晋级这些草稿。

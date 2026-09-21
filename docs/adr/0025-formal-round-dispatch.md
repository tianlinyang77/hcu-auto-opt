# ADR-0025：Formal Intent 到 Round 的原子派发桥

- 状态：Proposed；不授权生产开放或 HCU 执行
- 日期：2026-09-20
- 关联：#167、ADR-0015、ADR-0014、ADR-0024
- 前置：PR #168 的非执行入口；本提案不属于该入口的完成证明

## 当前代码的实际断点

`FormalStartCoordinator` 已完成部署对象重读、验签、Plan 重验及角色分离，但只写 Intent。
`PostgresRepository.create_search_round()` 明确拒绝 Formal，并校验 Scripted/synthetic/fixture。
`create_search_round()`、`add_round_candidate()`、`close_search_round_intake()` 分别自行提交事务。
因此不能依次调用三个方法，也不能删除 Formal 拒绝条件后直接复用 Scripted 启动器。
目前 `cancel_formal_start_intent()` 与 reconcile 都锁 Intent；新增派发必须加入同一锁协议。

## 决定提案

### 1. 保留旧 Intent，独立保存派发事实

旧 Intent v1 的三个 false 字段和 SQL 约束保持原义：这份记录本身不授予执行能力。
新增版本化 Dispatch Contract 与表，不把 `ready_for_round_creation` 改名成 running。
新记录一对一绑定 Intent、Task、Round、Preview、Plan、owner/B/D Authority、服务身份与候选成员。
`intent_id`、`task_id`、`round_id` 分别唯一；ID 复用既有确定性推导，不增加浏览器幂等键。
所有冻结绑定不可修改；状态变更及事件必须在同一事务中提交。

派发状态拟定为 `queued / claimed / recovery_required / completed / cancelled / expired / failed`。
`claimed` 只说明控制面已领取；物理运行来自 B Receipt，不从派发状态推导。
`completed` 只说明派发职责完成，不等于评测通过或加速；D Review 和人工签核仍独立。
具体终态所需证据在 Contract 评审中冻结，不能先实现一个宽松通用状态字符串。

### 2. 事务前重验，事务内锁定版本

部署显式注入派发能力，默认不注册 Worker 或新的 Web 执行路由。
先复用并抽取现有 coordinator 的完整验签/重读逻辑，输出内部验证快照；不得复制第二套验证器。
快照绑定 Intent version、完整输入摘要、服务身份、验证时间及所有授权的最早失效时间。
快照不是可由客户端提交的通行证，不能单靠对象类型或 ready 布尔值放行。

存储层新增一个单事务方法，执行：

1. 锁定 Intent 行；已有同绑定 Dispatch 时返回已有结果，不再创建、派发或执行。
   读取重放仍须部署身份验证；过期时可以查询已发生事实，但不重新授予执行能力。
2. 对新派发检查 Intent 非取消/失败、ready、版本及全部绑定一致。
3. 锁定可变的 Profile/Stage0/Baseline 权威行，核验撤销状态；统一锁序为
   Intent → 权威行（固定主键顺序）→ Round → Dispatch → Job，取消/恢复遵循同一顺序。
4. 使用数据库实际当前时间检查 owner/B/D/Preview 有效区间；长事务不能使用事务开始时间冒充现在。
   验签对象按 Hash 不可变，注册撤销需版本检查；外部撤销不能声称与数据库事务原子同步。
5. 原子写 Task、Formal Round、完整 business Candidate 集合、冻结 Family、Dispatch/outbox 及审计事件。
   任一步失败全部回滚，不能留下部分候选或可被普通调度扫描领取的半成品。

抽取接收同一 connection 的存储 helper；禁止在 helper 中另开连接或提交。
Search/Holdout、family alpha、Evidence Root 等只从 D Authority 读取，Adapter/窗口/预算只从 B/Plan 读取。
源码 Family、Round Candidate Family、Artifact Family 是不同 Hash 域，必须分别重算和交叉核对，不能互填。
本步骤不揭示 Holdout，不预造 Artifact Hash，不触碰设备。

### 3. 取消与 reconcile 必须同时升级

迁移启用时同步升级所有会写 Intent 的进程，不能让旧取消实现继续忽略 Dispatch。
部署采用停写、迁移、升级写进程、验证、再开放；不做新旧写进程混跑。

- 无 Dispatch：沿用原取消。
- Dispatch queued：在 Intent 锁内检查尚未领取，原子取消 Dispatch 和待投递记录并更新 Intent；
  已创建的 Round 保留审计，具体取消表示须与现有 Round 状态契约共同评审。
- 已 claimed：不能直接把 Intent 标 cancelled；提出停止请求，由持 Fence 的执行者安全停机和清理。
  清理未确认时不得显示“已释放资源”。
- reconcile 发现已派发：不得把历史 Intent 的新一次过期判定当作从未执行，或重新创建 Round。
  派发后的健康、到期、停止与恢复转入 Dispatch 处理；旧 ready 只是历史闸门结果。

数据库需提供取消/派发互斥约束或统一写函数作为第二道防线，而不只依赖 Python 调用约定。

### 4. 投递幂等不等于物理执行可以重试

outbox 使用持久化唯一业务键与 `FOR UPDATE SKIP LOCKED` 领取；领取提交后再做外部工作。
投递允许重复，Job 消费必须校验 Dispatch、Round、Phase、Candidate 与 Attempt 的固定绑定。
普通 Scripted Router 不得消费 Formal outbox，也不能因发现新 Round 自动绕过派发闸门。
Worker 在物理操作前重验当前授权、Target Lock、预算及 B 的 Lease/Fence。

消息领取期限与设备 Lease 是两个不同概念。消息期限到了，不意味着旧进程或设备空闲。
Worker 失联进入 `recovery_required`：先用持久化 Receipt 判断是否已执行，再由 B 完成 Fence
隔离及资源清理证明；仅在明确未开始或已安全恢复的情况下按预算/授权规则允许后继 Attempt。
旧 Worker 的迟到写入须带 Fence 比较，不得覆盖新状态或启动第二次物理运行。
队列本身只能保证逻辑幂等，不能承诺物理 exactly-once。

### 5. 页面与发布边界

页面分开显示“意图已受理”“轮次已创建/排队”“执行者已领取”“实机已开始”和“证据待签核”。
这些状态来自相应持久化记录，刷新和网络重试不产生新执行身份。
入口保持默认关闭，测试仅用隔离 schema 和模拟执行者；生产签名、资源窗口和 B/D 注册未具备时不开放。
`automatic_release_allowed=false`；不提升 Baseline、不改频率、不停止无关进程。

## 交付与评审顺序

当前开发切片只实现 `queued / cancelled` 与原子完整 intake，默认关闭，
没有 Consumer，不创建通用 Job；其余状态为后续设计，不属于本次已实现范围。
`PostgresFormalDispatcher` 为独立部署服务，准备阶段复用 coordinator 验签与重读，
事务内再次锁定并校验数据库 Authority、Intent version 与数据库实际时间。
Profile Catalog 为部署快照，撤销须更新部署并停用旧进程；不宣称支持外部撤销原子同步。

1. A/B/D 审阅本 ADR，明确取消、恢复、Round 状态兼容及部署切换方案。
2. 定义 Dispatch Contract 和数据库迁移，实现原子创建/取消，真实 PostgreSQL 验证。
3. 接受同一事务的存储 helper 与受控 outbox Consumer，使用模拟执行者完成崩溃恢复测试。
4. 页面接持久化派发读模型，显示准确执行层级；生产部署另行验收。

验收矩阵见 `docs/plans/formal-dispatch-acceptance.md`。评审前可以开发隔离测试，
不能通过删除现有保护条件抢先开放 Formal 执行。本提案不关闭 #102/#126/#167。

## 2026-09-21：单次控制面领取切片（仍 Proposed）

新增迁移 28，独立 `formal_dispatch_claims` 保存一个 Intent 的唯一领取 Token、
Worker、期限和 `claimed / recovery_required`。原创建 outbox 不被改写成物理执行回执。
领取默认关闭，在 Intent 锁内重验当前 Authority、精确成员 Family 和授权窗口。
消息期限不能超过授权期限。重复领取直接拒绝，包括原 Worker 丢响应后的重试。
本阶段没有重新入队、续租、成功完成或执行回调入口，故不授予 HCU 执行能力。

领取与取消使用同一 Intent 锁。取消先提交则领取拒绝；领取先提交则旧取消路径被
数据库守卫拒绝，不能把可能仍有执行者的任务标成安全取消。
这不是受控停止已完成：停止请求、B Fence、资源清理证明与恢复后的重试仍待后续实现。
超时只持久化 recovery_required，不释放设备，也不创建第二个 Attempt。
读取页面可显示领取/恢复状态，`execution_consumer_enabled=false` 仍表示物理执行消费者未接。
部署前必须迁移并同步升级全部读写进程；旧状态模型不能读取新领取状态。

## 2026-09-21：持久化停止请求及执行前检查（仍 Proposed）

迁移 29 增加追加式停止请求，唯一绑定当前 Intent/Worker/claim_token，
请求人与固定原因枚举保存为审计；身份与 Token 从数据库读取，客户端不能指定。
这是受信部署服务方法，**没有把旧 Web Intent capability 扩大成停止权限**。
没有领取的任务仍走旧 queued 取消；已领取任务可以记录停止请求，但不能据此释放资源。
重复同请求返回原事实，修改请求人或原因拒绝；授权/领取过期仍允许同部署请求停止。

`assert_active` 在每个阶段之前重验签名、Authority、Intent 版本、Worker/Token、
有效期和停止请求。它不调用执行器，不申请设备，不签发 B Lease/Fence。
停止请求若在检查后才到达，仍需后续执行器协作取消以及 B Fence；
数据库检查与外部进程启动不是原子操作，不能宣称消除了这一竞争。

只读页面增加 stop_requested，文字明确“不代表已停止或资源已释放”。
超时标记后以 recovery_required 优先显示；停止请求的原始审计不被覆盖。
本阶段不新增“清理通过”布尔字段：清理终态须复用 B 执行回执及真实资源身份验证，
待接完整 phase/Attempt/Lease 绑定后才能收口；当前不允许重新入队或自动发布。

## 2026-09-21：B 执行器检查点接线（仍 Proposed）

既有 `M2FormalPhaseExecutionAdapter.run` 增加部署注入的可选 execution_checkpoint，
在进入执行器、预算预留后且进入唯一 Harness 前、Harness 返回后各调用一次。
`FormalClaimCheckpoint` 先绑定 Intent 的 Task/Round/Plan/授权 Hash 及候选成员，
再调用持久化领取检查；不创建第二个调度器或计时 Harness。
旧调用者没有注入时保持原行为；**新 Formal Consumer 必须显式注入**，默认部署未开放。

预留前拒绝不采样、不预留预算；预留后拒绝走 B 既有清理、实际预算结算、失败回执。
尚未进入 Harness 的路径记 harness_active_seconds=0，不能把检查/清理时间当作采样时间。
采样返回后停止不发布成功 measurement_ref；清理失败继续记录 cleanup_failed。
不由控制面领取 Token 替代设备 Fence，不据失败回执自动释放资源。

发布后回读回执的不可变内容，核对完整 ExecutionBinding 与 request_hash；
新增 `load_for_request` 允许后续 Consumer 对 Phase/Attempt/Lease/Fence 精确绑定验真。
此操作只证明回执归属与内容，不证明当前资源已空闲。

本切片没有自动扫描/启动器，没有持久化 phase-attempt 的执行中标记，
也没有运行中强杀/协作中断线程。失联重试、当前租约清理验真及物理启动竞争仍需下一步。

## 2026-09-21：持久化阶段日志与单阶段消费入口（仍 Proposed）

迁移 30 新增 formal_phase_journal 和追加式事件。唯一业务槽位是
Intent/Candidate/Phase，另存 execution_id、完整请求和 Hash、Worker/claim_token。
故障后改变 Job/Attempt 不能绕过唯一槽位。当前没有自动重试或重新开启槽位的方法。

调用 B 之前先提交 invoking：其含义是“可能已调用”，不是 HCU 已运行的证明。
事务回滚意味着没有取得调用权；提交后崩溃则保留 invoking，重复消费拒绝调用，
需恢复核验。外部调用与数据库提交不是原子操作，选择保守停住而不是重复执行。
收到经现有 Store 验真的终态回执后提交 receipt_recorded；该回执可能是失败的，
不能从日志终态推导 D 通过、资源释放或性能收益。
未知异常转 recovery_required，迟到回执不得覆盖恢复状态。终态和审计不可修改删除。

FormalPhaseConsumer 默认关闭，只处理部署方已准备好的单阶段请求，不扫描队列、
不申请 HCU 资源、不自动生成 Artifact/Holdout，也不创建通用 Job。
调用时强制注入 FormalClaimCheckpoint；同一日志已有回执只回读文件，不再次调用 B。
已有 invoking/recovery_required 直接拒绝，保留原阶段日志。
失效领取不允许新调用；历史回执只读重放不授予新权限。

尚缺生产扫描/调度装配、从 Round 到完整 B PhaseRequest 的真实准备、
运行中协作停止以及当前资源清理/恢复验收。旧执行器直调入口不在此日志保护范围，
不能据新 Consumer 单测宣称所有物理执行都 exactly-once。

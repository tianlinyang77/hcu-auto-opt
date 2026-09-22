# 本周完整 MVP 执行计划

2026-09-22 用户批准实施；替代此前“暂停多候选”的临时安排。

范围：BW20/HCU 7、auto 频率、SGLang、固定 allocator 热点、两个不同源码候选。
不停止他人工作负载，不绕过当期授权或签署，不要求必须测出加速。

## 工作包与验收

| 工作包 | 必做 | 完成条件 |
| --- | --- | --- |
| A 候选与配置 | 复用历史候选源码但重新绑定本轮；Agent 产生第二份实现并审核；冻结目标、当期测量适用性、授权和预算 | 两份不同源码、来源和审核齐全；没有把旧签署当作本轮结果 |
| B 执行主链 | 复用 #168/#169 与唯一 Harness；串行构建/正确性/Search/Holdout/Bonferroni FWER/D | 真实 Round 完成，恢复不重复物理执行；整批 Holdout 后校正，alpha=0.05 |
| C 页面与服务级 | 已登记对象启动、进度、拒绝原因、签署；有入选者才跑新 Endpoint Campaign | 新来源独立核验；结果与签署 Hash 对齐；刷新不重复执行 |
| D 封版 | 评审、获准合入、持久化、备份恢复、固定版本及交付包 | 重启可读、证据可追溯；历史页面保留回退 |

第 1 天完成 A 与主链接线；第 2 天 PostgreSQL 联验及首轮实机；第 3 天裁决和适用的
服务级验证；第 4 天收尾封版。属于目标排期，阻塞按事实记录，不降低验收线。

## 约束

- 第二候选最多两次有记录的模型请求；重复源码、伪造变化不计数。
- Search/Holdout 隔离；多个入选者按冻结 Search 排名选一个，不用 Holdout 反复调参。
- 无入选者可正常结束并接受证据，服务级分支明确本轮不适用，不能声称已实机验收。
- Endpoint 增加区分 M1 与 Formal 的来源引用；旧请求兼容，不伪造 M1 任务或重复旧 Campaign。
- 签名身份存在不等于执行窗口获批；未知执行结果先恢复，预算冻结后不自动扩容。
- 继续同一 PR 链，不能将本计划当成合并授权。多机、多框架、自进化和自动发布不在范围内。

## 本轮进展

- [x] 恢复完整 MVP 目标并固定依赖顺序。
- [x] 执行两次现有 Messages 源码提案入口请求，保留回执；未创建 Formal 候选。
- [ ] 第二候选通过审核并冻结：当前阻塞，见下述证据记录。
- [x] 新增显式 M1/Formal 来源引用契约及兼容/拒绝测试。
- [ ] Endpoint 数据库准入、迁移、Worker/D 来源校验和新写入口接线。
- [ ] 多候选真实 Search/Holdout/D、页面和适用的服务级整链。
- [ ] 数据持久化、代码获准合入及最终封版。

## Agent 尝试记录

模型 deepseek-flash，既有批准的 Messages 服务；固定来源 Commit
`dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，allocator.py 文件 Hash
`sha256:ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27`。

| 尝试 | 结果 | Token | Runner Receipt Hash |
| --- | --- | --- | --- |
| 1 | 可应用但未审核草稿；bool(torch.all(...)) 隐式同步，未晋级 | 7005 | `sha256:9191aec67189d9bb97f3a7c3340ad0810e10714340e6e10e86d53f184478778c` |
| 2 | no_proposals | 5987 | `sha256:556b25d7d55ba4fabd18000b8b560a58320447306fa8046fe15d9ad8723708ae` |

两次合计 12992 Token；没有第三次请求，没有 HCU、性能判决或候选注册。
这是独立 source-only 开发提案入口，不是 PostgreSQL/Apex 已领取的正式生成任务；
`scheduler_claimed=false`、`formal_intake_allowed=false`，后续不可改标签直接晋级。
第一份的同步是优化风险及本次提示约束不符，不是已测得数值错误；未对其执行 HCU 测量。
候选策略改变或扩展生成预算需要新的明确决定。B/C 的不依赖真实候选工程仍可推进。

验证：本轮 Endpoint 来源、旧 M1 控制面、D 裁决、签署身份及 Console 单元回归 44 passed；
前端 Campaign 3 passed；Ruff 通过。新引用目前不接收于旧 Endpoint API，不改变现有签署。
nmz36 CPU 验证容器的 SSH 本次连接超时，未在那里执行测试，不能报告 PostgreSQL 或 HCU 本轮验收。

### 服务级来源校验增量

已增加仓库层 `_verify_formal_endpoint_source`：在调用者事务中重读并锁定 Round、Task、
人工签署、EvidenceBundle、Baseline、Artifact、Round 成员与批级 D 结果。拒绝未签署、
错基线/候选/制品、合成证据，以及非 faster 的成员。它只检查来源资格，不创建 Job，
也不等于已通过执行授权；尚未接入新 Campaign 写入口。

发现现有 D `recommended_candidate_id` 按 Holdout 下界排序，不能直接作为本计划的
服务级候选选择。历史结果保持不变；新路径必须根据冻结 Search 排名，在通过整批 D 的
成员中选一个，并绑定进本轮签署材料。该选择及持久化接线仍待完成。

新增来源拒绝测试与既有 Endpoint 回归合计 59 passed，Ruff 通过。
BW20 SSH 连接超时，本次没有实机执行或新的性能结论。

### Search 排名承接增量

新增 `formal-search-selection-v1`，绑定 Round、Artifact Family、规则 Hash 和 Search
测量引用；按 Search mean effect 降序、UUID 升序确定顺序。Formal Finalizer 从已校验
Hash 的 Search 输入读取排名，在批级 D 通过的成员中选择一个，将选择结果写进待签署
EvidenceBundle。没有通过成员时明确 `not_applicable`；旧摘要不自动获得新资格。
仓库来源校验进一步要求候选就是签署材料中的 Search 入选者。

该代码已接 Finalizer，但 Search producer 尚需实际产出新格式，Campaign/Worker 写链仍未
开放。此增量不代表实机或全链验收。针对性回归 55 passed、1 skipped，Ruff 通过；
BW20 SSH 仍超时。没有额外模型请求，也未操作 HCU 或他人进程。

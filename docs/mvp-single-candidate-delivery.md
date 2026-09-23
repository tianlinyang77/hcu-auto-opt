# 首次 MVP 交付：单候选真实 Agent 优化闭环

> 范围裁定（2026-09-23）：当前最小 MVP 以[单热点、单候选、单目标环境](plans/minimal-mvp-scope.md)为准。
> 双候选 Formal Round 是后续扩展，不是当前交付的先决条件；本文保留 2026-09-22 已完成案例的历史记录。

2026-09-22 收口。交付名称为 **HCU 自动优化系统：单候选受控验证版**。
本页是首次 MVP 的范围与演示入口；多候选 Formal 调度是后续增强，不再作为本次交付前提。
固定 BW20 单候选流程的操作步骤见[操作手册](mvp-single-candidate-operations.md)。

## 已合入代码的交付基线

2026-09-22 的历史案例演示源码固定为 main Commit `3c1fc64e335d7501b92fdba773549bdcd84c20c5`。
已从该 Commit 的独立干净 Worktree 完成前端构建、7 项 Console 后端及 3 项 Campaign 前端测试，
并在 4196 页面回查 completed / accepted / inconclusive 与下述正式 Result Hash。
这条展示路径不依赖尚未合入的 PR #168/#169；不必合入多候选工程才能查看已有验收。

本地交付包包含固定源码 ZIP、同版本预构建前端 ZIP 和 DELIVERY.md，不包含 node_modules、
本机私有配置、数据库或模型凭据。接收方仍需 Python 依赖、VPN/SSH 和已部署的远端 API。

| 交付文件 | SHA256 |
| --- | --- |
| source-3c1fc64.zip | `594e6a9b98ed9cf70f8e952a4a36e4312c75f8e50562faa9f854b83a5bf6e327` |
| frontend-3c1fc64.zip | `6a18faf25df35feb2a1013a3c4d33516e82bb1cda827d67c9c827a6916621c3e` |

源码归档核对包含 CLI 与历史实机证据说明，不包含 `.git`、`.codex` 和 node_modules 目录。
前端预构建只省去接收方的 Node 构建步骤，不是独立离线运行优化系统的安装包。

## 一句话总结

已完成一个真实 Agent 候选的生成、构建、HCU 正确性与性能验证、独立裁决和人工签署；
另完成固定 SGLang 工作负载的服务级验证与签署。局部 allocator 微基准耗时降低约
16.60%，服务级结论为 inconclusive，不能宣称服务加速。不是无人值守自动找热点或自动发布。

## 已交付案例与证据

| 层次 | 身份与结果 | 证据入口 |
| --- | --- | --- |
| 真实 Agent 单候选 | Task `73f6f07d-14ed-5614-a8e5-75e77c4356f0`；Candidate `bbcdc4f0-0369-54d0-b4cd-57763203c272` | [微基准完整证据](evidence/m1-formal-bw20-20260916.md) |
| 正确性 | 冻结范围内 correct；12 条结果、302 个比较元素、0 不匹配 | 同上，不推广为任意输入正确 |
| 局部性能 | faster；耗时降低 16.60%，95% CI 约 [14.54%, 18.81%] | 同上，仅限冻结 allocator 微基准 |
| 微基准签署 | Signoff `e131588e-546c-5553-9095-0ce8fc5d38ea`；2026-09-16 接受 | 同上 |
| 服务级原始采集 | 8/8 ABBA 组、32 次服务启动、3,200 次 measured requests | [历史采集报告](bw20-endpoint-latency-result.md) |
| 服务级正式裁决 | Campaign `86e84235-64c7-50c1-919d-a310fed85e8b`；inconclusive | 下述实时结果页 |
| 服务级签署 | `22f54d45-c9b5-572b-b6de-04f0bd83c808`；2026-09-20 接受；Campaign completed | 2026-09-22 实时 API 只读回查 |

服务级正式 D Result Hash：
`sha256:99d9cbd0a545549fd9a0d93b046a277fd9ebcf04fde9f228cb16be8a07d8415c`。
本次 API 回查确认 formal_d_adjudication=true、裁决 Job succeeded、签署 accepted、
automatic_release_allowed=false。回查是持久化记录核验，没有重跑 HCU 或重新逐文件复验所有原始证据。
9 月 17 日报告中的“无正式 D 裁决”是当时的历史状态，不是 9 月 22 日的当前状态。

## 三分钟演示

1. 打开下述 Endpoint 结果页，确认不是 `?demo=1`，核对 Campaign ID。
2. 展示 completed、正式 D 裁决、inconclusive、人工接受记录。
3. 对照微基准证据说明：Agent 确实产出了候选，局部有收益，但服务级没有可测收益。
4. 展示原始证据索引与发布边界；接受证据不等于发布代码。

不要为了演示再次调用付费模型、重跑测量、重复签署或停止其他人的任务。

Agent 生成终态与 M1 正式验收是两个独立入口：`/?agentEvidence=<generation_run_id>` 用于查看
Agent 生成与审核记录；`/?manualCandidate=<task_id>` 用于查看 M1 Candidate、D 裁决和签核。
只使用各自 API/控制面返回的真实 ID，不把一个入口的 ID 填到另一个入口。固定演示 Commit
上的 M1 Summary 不返回 Agent 来源事件或源码 diff。后续 main 已通过 #170 增加新任务的
Agent 来源事件和签核校验，通过 #171 修正历史任务的来源缺口展示；历史已签任务不会凭空补出
新来源事件，也不能把其他案例的 diff 当成本次改动。
签核前按 [M1 人工签核 Runbook](m1-signoff-runbook.md) 独立核对冻结证据。

## 启动与停止

在仓库根目录使用 Python 3.10+、Node.js 与已有 SSH 配置：

```text
python -m pip install -e .
npm --prefix web ci
npm --prefix web run build
python -m hcuopt endpoint-console serve config/deployments/bw20-endpoint-console.example.json
```

默认地址：`http://127.0.0.1:4196/?endpointCampaign=86e84235-64c7-50c1-919d-a310fed85e8b`。
需 VPN、BW20 SSH 和远端已有 API；启动器会验证 Campaign，失败时不会用 Demo 替代。
该配置只读取此 Campaign，不开放测量或签署写接口。端口冲突时调整配置，不杀已有进程。
启动输出包含实例目录；管理命令见 [统一结果页入口](endpoint-console.md)。

```text
python -m hcuopt endpoint-console status <instance_directory>
python -m hcuopt endpoint-console stop <instance_directory>
```

普通 clone 不包含私有原始证据、数据库或模型凭据；以上是查看已部署案例的步骤，
不是换任意机器一键重跑优化。新实验仍使用现有准入、唯一 Harness、独立 D 与人工签署。

## 本次不再扩展

- 第二个候选、2–4 成员 Family、自动 Search/Holdout 与完整 Formal 新入口验收。
- 自动找热点、多框架、多机、多用户、自进化、生产灰度与自动发布。
- 为新 Formal 通道重新签窗口或把旧测量冒充当期结果。

已有相关代码保留，不删除、不默认启用；后续独立排期。Skills 仅作知识参考。

## 交付限制与封版边界

2026-09-22 交付入口核验：从当前工作树构建前端成功；结果页 HTTP 200，
托管实例 running / upstream available / read_only=true；API 返回 completed、accepted、
正式 D inconclusive、automatic_release_allowed=false。Endpoint Console 后端 7 项与前端
Campaign 3 项定向测试通过。本次只读恢复展示，没有重跑优化、改变签署或写入旧任务。

- 当前交付证明一个固定真实案例与受控操作链，不承诺服务级正收益或完全一键化。
- 历史交付时的代码审查与部署问题见下方按日期补记；不能把文档收口称为生产上线。
- 历史 BW20 数据库仍未切换；新独立数据库已有卷挂载和恢复演练，但正式切换、异地备份和长期运维交接未完成。
- 当前工作分支还包含后续 Formal 工程；历史实机结果只属于各自记录的执行版本，
  不能给最新分支所有代码背书。封版不得把尚未实机验证的多候选功能混入验收承诺。

## 2026-09-23 最小 MVP 收口检查点

- 当前范围固定为单候选 M1；多候选 Formal 与 Endpoint 新来源接线暂停，不是最小 MVP 阻塞项。
- 新增的 BW20 Agent→M1 来源事件、M1 页面展示和服务端批准校验已在独立的 `feat/mvp-single-candidate-closeout` 分支实现，基于 `origin/main`，不包含 PR #169 的 Formal 提交。
- 干净主分支基线验证：Web 54 passed；Agent 晋级/来源校验 5 passed、PostgreSQL 用例本机 1 skipped；Ruff、ESLint、前端生产构建通过。PostgreSQL 来源联验在 nmz2 的临时 CPU 容器与随机数据库中 6 passed，覆盖事件幂等、Source Hash 绑定及缺少来源时拒绝批准；容器 `devices=[]`，数据库、容器和暂存源码均已自动清理。
- Windows 全量 unit：2031 passed、47 skipped、17 failed（494 秒）；16 项是当前 Windows symlink 权限限制，1 项 Formal readiness 测试期望 32 个 evidence、实际为 30，属于暂停的 Formal 范围。本结果不是全量绿色。
- 来源绑定代码已通过 [#170](https://github.com/tianlinyang77/hcu-auto-opt/pull/170) 合入 main，Commit `e12aac0e31abc28ce9b971a2b86268038024b5c4`。最新代码的一次新鲜 BW20/HCU 7 Agent→M1 实机运行与签署仍未完成。

## 2026-09-23 BW20 部署联验补记

- 在 BW20 的独立目录部署 `e12aac0` 源码，固定归档 SHA256 为 `a2caf9c31585d0dc3b8651218713337f84bdfc96068e4fb639069fb528886d3a`；Linux/Python 3.10 的 Agent 晋级及来源校验定向测试 5 passed。
- 对现有主库做 PostgreSQL 自定义格式备份，SHA256 为 `ea5d30b81cecaf1d08c9d4f873bdc6a834478c1d7c105cf2693886a7a3203a91`。在独立的卷挂载实例恢复并重启；迁移/Task/Candidate/事件/签核/Agent Run 行数分别为 `32/29/2/113/1/10`，与原库一致。原库仍在运行，未切换写流量。证据目录的长期备份与恢复尚待完成。
- 用最新源码和恢复库启动了限于本机回环地址的短时 API，真实 HTTP 读取历史 M1 Task `73f6f07d-14ed-5614-a8e5-75e77c4356f0`，返回 `completed/accepted`；前端经隧道读取并显示原有 D 结论、测量字段与签核。历史 Task 没有新版本 Agent 来源事件，因此不能当作来源绑定正向验收。
- 联页发现历史已签 Task 被错误显示为当前来源校验失败；本次修订把它标为历史字段缺口，待签任务的来源缺失/重复/漂移仍按原规则拒绝批准。前端 55 项测试、ESLint、生产构建通过。
- 本次没有启动 HCU Worker、重新调用模型、复测性能或重签旧 Task。新候选的完整当期实机链路仍是最小 MVP 的最后一项验收。
- 不把 2026-09-16 已签历史候选冒充为最新工作树验收；不声明服务级加速。本轮只运行了隔离 PostgreSQL CPU 联验，未运行 HCU。

## 2026-09-23 最新 main 与新 Agent 轮次

- #170、#171 均已合入；本轮源码固定为 main `c2b0eba70523a5a4823b8ba9db18c251e9c70840`。#171 的前端修正通过 55 项测试、ESLint 和生产构建；最新源码的短时 API 经真实 HTTP 读取历史 Task `73f6f07d-14ed-5614-a8e5-75e77c4356f0`，返回 `completed/accepted`。这只证明兼容读取，不是最新版本的 HCU 优化验收。
- 在 BW20 建立独立、卷挂载的 PostgreSQL `hcuopt-mvp-db-20260923`，仅绑定 `127.0.0.1:55437`。从原库备份恢复并重启核对，迁移/Task/Candidate/事件/签核/Agent Run 行数为 `32/29/2/113/1/10`。原库 `hcuopt-bw20-stage0-db-feb58e54` 保持运行，未切换写流量。
- 新 Task `083b2443-1a53-521b-9b85-ca92e3f75b4e` 绑定热点 `53a58cb1-4d5d-5e3a-b0ca-c5aa40b346c5` 与基线 Epoch `cc1e6295-572d-531a-af1e-01fa6b07a3c1`。Run `8286d500-55b4-5add-8efd-6f8e8e66458c` 先因 300 秒租约超过冻结的 210 秒上限而在领取前被拒；改为 210 秒后，宿主机可见 `/dev/kfd` 触发 Agent 安全守卫。租约到期后按正式 reconcile 结算为 `failed`，没有调用模型。
- 第二个 Run `3ebc9a88-d450-554a-9d91-734f25336fa3` 在无 HCU 设备映射的 CPU 容器执行一次；Attempt `66c7a009-92f0-554f-961a-dcf15d92448a` 的正式收据为 `failed/provider_transport_error`、无 Proposal、清理 `verified`。收据记录约 22.28 秒用时；本地启动标记与收据写入时间约为 2026-09-23 09:59:34–09:59:57 UTC，内部 Request ID 为 `87bcb453-7167-5b67-92f2-dad546e6eb8d`（不是服务商请求 ID）。这些字段可供对账，但无法证明请求未到服务端或未计费。无凭据直连 `/anthropic` 得到预期 401，只证明基础网络可达，不能解释这次模型请求的具体传输故障。
- 冻结计划的两个有记录生成尝试已用尽；本轮没有可审核或晋级的候选，不得创建第三个 Run、重放带 `started` 标记的 Attempt、补造差异、启动 HCU 测量或签署新 Task。后续如需继续，先对账模型服务端请求/计费及传输故障，再为**新轮次**明确生成预算与授权；旧 Attempt 保持不可变。
- 新库在两次失败后备份为 BW20 私有目录 `mvp-persistence-20260923/backups/hcuopt-post-agent-c2b0eba.dump`，SHA256 `e9ef157b58c703756e39cfbf1652fefe5bdf42025b2d3e437c1ee7c54278a83f`；Agent 输入与收据归档 `agent-evidence-c2b0eba.tar.gz`，SHA256 `5f6fb1d8554907ca3afd28eeb55f6f43f132fc0ac07a7d16b7d1278147e765f7`。两份归档已做格式读取核验，仍在同一主机磁盘，不能代替异地备份。模型凭据不在 Git、交付包或本页中。

**本次收口判定：**历史单候选案例可演示、代码及历史读取兼容；最新 main 的全新 Agent→候选→HCU 正确性/性能→签署链路**未验收**。正式交付可以如实称为“固定案例的受控验证版”，不能称“新任务一键跑通”或“服务级已加速”。

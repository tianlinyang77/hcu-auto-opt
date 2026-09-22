# 首次 MVP 交付：单候选真实 Agent 优化闭环

> 当前完整 MVP 目标已扩展为[双候选及适用的服务级验证](plans/mvp-multicandidate-week.md)。
> 本页仅记录已经交付的单候选基础，不代表新增完整目标已完成；“本次不再扩展”属于历史范围。

2026-09-22 收口。交付名称为 **HCU 自动优化系统：单候选受控验证版**。
本页是首次 MVP 的范围与演示入口；多候选 Formal 调度是后续增强，不再作为本次交付前提。

## 已合入代码的交付基线

演示源码固定为 main Commit `3c1fc64e335d7501b92fdba773549bdcd84c20c5`。
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
- 代码审查与合入、部署长期持久化和迁移交接仍须独立完成；不能把文档收口称为生产上线。
- 当前 BW20 数据库使用临时存储，已有备份但仍需管理员落实持久化，不能作为长期可靠部署。
- 当前工作分支还包含后续 Formal 工程；历史实机结果只属于各自记录的执行版本，
  不能给最新分支所有代码背书。封版不得把尚未实机验证的多候选功能混入验收承诺。

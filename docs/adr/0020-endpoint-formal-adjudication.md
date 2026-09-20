# ADR-0020：端点正式裁决独立重读八个 ABBA 证据组

- 状态：Accepted
- 日期：2026-09-20

## 背景

PR #156 已证明 BW20 Endpoint Worker 可以在独占 Lease 下完成一个完整 B-C-C-B 组，并在
每次 acquisition 使用新服务进程和独立缓存 Namespace。后续 v2 campaign 串行完成了八组，
但当时的汇总脚本不属于控制面的 D，因此结果只能标记为
`formal_d_adjudication=false`，不能进入正式签核。

如果让测量 Worker 同时计算并发布 verdict，Producer 就可以选择样本、忽略失败或改变统计
口径；如果只信任汇总 JSON，原始 sample、生命周期、激活和清理文件被篡改后也无法被发现。

## 决策

1. 第一版正式端点裁决固定消费八个独立 Endpoint Run。每个 Run 必须包含一个完整
   Baseline-Candidate-Candidate-Baseline 组，共 32 个 acquisition。
2. D 只接受本地 `file://` 证据和显式白名单根目录。它重新读取原始 Hash Manifest，并校验
   acquisition 目录中的全部文件，禁止网络读取和目录逃逸。
3. D 独立核对控制面保存的 `result.json`、Overlay 激活、缓存 Namespace、进程 start/reap、
   stop/cleanup 以及每个 measured request 的 request/response/sample。
4. acquisition 是统计独立单位。每个 acquisition 先计算请求平均延迟；每个 ABBA 组再计算
  两条 Baseline 与两条 Candidate 的均值及 log ratio；八个组使用预先冻结的 95% Student-t
   区间。不得把 3,200 个请求冒充 3,200 个独立 run。
5. verdict 只允许 `faster`、`slower`、`inconclusive`、`invalid`。区间跨越零即为
   `inconclusive`；Hash、数量、顺序、进程、缓存、激活、token 或清理任一不符即为
   `invalid`，且不发布任何性能统计。
6. D 输出固定 `formal_d_adjudication=true`，但仍固定
   `automatic_release_allowed=false`。D 裁决不是发布授权，之后仍需独立人工签核。
7. Agent/Apex 不得调用内部函数绕过该重读过程，也不得提供 verdict。

## 第一版边界

- 固定 8 个 ABBA 组、95% 区间和非流式 E2E latency。
- 继续使用 PR #156 已验证的单组 Worker，不在本 ADR 中改写 HCU Runner。
- 控制面 Campaign 持久化、D Job 编排、页面和人工签核在本契约之上继续实现。
- 当前完整 v2 campaign 可作为输入准备材料，但只有证据仍可从授权目录独立读取并通过本
  协议时，才能生成正式 D 结果。

## 后果

- 端点统计从一次性外部脚本升级为可测试、可重复、fail-closed 的仓库能力。
- 测量与裁决权分离；即使 Producer 汇总文件被修改，D 仍以原始证据和 Hash 为准。
- 八个组中的任一组损坏都会使整个 campaign 变成 `invalid`，不会自动丢组后重算。

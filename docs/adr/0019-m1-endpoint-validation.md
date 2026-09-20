# ADR-0019：M1 端点验证使用独立、只读引用的证据链

- 状态：Accepted
- 日期：2026-09-16

## 背景

BW20 上的首个 Agent Candidate 已完成 Kernel 级正确性、ABBA 性能测量、独立 D 裁决和
人工签核。该证据只证明冻结 allocator 微基准约 16.60% 的收益，不能推导 SGLang 服务
端点收益。已有 Framework Smoke 只发送一次功能请求，其 `e2e_latency` 不具备端点性能
统计意义。

若直接把端点测量追加到已签核 EvidenceBundle，会改变已批准对象的含义和内容身份；若
复用一个长期运行的服务进程比较 Baseline/Candidate，又无法区分进程启动、缓存、Overlay
激活和请求波动。

## 决策

1. 端点验证创建独立的 `Endpoint Validation Run`。它只读引用已签核 M1 的 Task、
   Candidate、Baseline Epoch、Target Snapshot、Candidate Source、Artifact、
   EvidenceBundle 和 Signoff，不修改原 M1 verdict、EvidenceBundle 或 Signoff。
2. 第一轮冻结 SGLang 0.5.12、单节点、单 HCU、TP=1、closed-loop concurrency=1、固定
   镜像 Digest、源码 Commit、模型、请求模板、token 预算、sampling seed、attention
   backend、page size、CPU/NUMA 和缓存策略。并发扫描、多卡和多节点另开协议。
3. `PROVISIONAL` 探针只验证端点请求、Overlay 激活、生命周期、清理和证据采集是否可行，
   不生成正式性能结论。探针通过后才能创建 `FORMAL` Run。
4. 正式测量使用完整 Baseline-Candidate-Candidate-Baseline acquisition 组。每个
   acquisition 必须启动新服务进程、使用新的空缓存 Namespace，并保存 start/reap、
   ready、warmup、逐请求结果、server log、Overlay 激活和清理的原始 Hash。
5. 统计独立单位是 acquisition。一个服务进程内的多次请求只是该 acquisition 的观测，
   不能冒充独立 run。D 使用 paired/block 方法重读原始证据后，才可产生
   `faster`、`slower`、`inconclusive` 或 `invalid`。
6. 非 2xx、超时、JSON/流式解析错误、token 约束不符、Overlay 身份错误、进程未回收、
   缓存复用、Lease/Fencing 失效、资源未恢复均 fail closed；不得丢弃失败请求后只统计
   成功样本。
7. 非流式请求记录 E2E。只有流式协议稳定解析并保存原始 chunk 时才记录 TTFT/TPOT；
   不得从单个聚合 `e2e_latency` 反推 TTFT/TPOT。
8. Agent/Apex 仍只负责提出 Candidate。Runner、Lease/Fencing、测量、统计裁决、签核和
   发布权限继续由控制面、B、D 和人工签核边界负责。
9. 所有端点验证结果保持 `automatic_release_allowed=false`。通过端点验证也不构成生产
   发布授权。

## 后果

- M1 Kernel 证据保持不可变，端点结论可以被单独撤销、重测或升级协议。
- 一次端点运行成本高于 Framework Smoke，但可区分 Candidate 效应、服务启动波动和缓存
  污染。
- 第一阶段允许在没有性能结论的情况下尽早暴露镜像、协议、Overlay 和清理问题。
- 正式端点收益必须明确写成端点工作负载结论，不得沿用 allocator 微基准的 16.60%。

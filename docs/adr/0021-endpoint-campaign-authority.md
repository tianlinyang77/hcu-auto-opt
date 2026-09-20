# ADR-0021：正式端点 Campaign 冻结八个已完成 Run 后才进入 D

- 状态：Accepted
- 日期：2026-09-20

## 背景

Endpoint Worker 的权威单位是一个 B-C-C-B Run，而 ADR-0020 的正式 D 输入固定为八个
独立 Run。此前八组关系、顺序和原始 Hash Manifest 只存在于一次性 campaign 文件中，
控制面不能回答“这次裁决究竟消费哪八个 Run”，也无法阻止使用未完成、不同环境或不同
计划的组拼成一个结论。

## 决策

1. 新增 `Endpoint Validation Campaign` 控制面对象。第一版只允许绑定恰好八个不同的
   Endpoint Run，并保留调用方给出的顺序作为 group ordinal 0..7。
2. Campaign 创建时在同一 PostgreSQL 事务中以共享锁重新读取八个 Run、Task 和 Job。
   每个组必须为 `provisional_passed`，底层 Job 必须 `succeeded` 且已完成 Workflow
   advance；Run 与 Job 保存的 result 必须一致。
3. 八个 Run 必须共享 Signed M1 Task、Target Snapshot、Adapter Profile、环境指纹、
   Workload、Plan 和 Plan Hash。任一不同即拒绝创建，不允许在 D 中降级兼容。
4. Campaign 创建时由控制面生成完整 `EndpointFormalAdjudicationRequest`，冻结 32 个
   acquisition 的 URI 与 Hash、受信 Adapter 固定的 Baseline Module Hash，以及原始
   Manifest URI/Hash；调用方不能自报 Baseline Hash。
5. Campaign 的绑定字段由数据库 Trigger 设为不可变。状态和未来的 D result/signoff 可按
   状态机推进，但不得修改输入后沿用同一个 Campaign 身份。
6. 初始状态固定为 `awaiting_adjudication`，并继续固定
   `automatic_release_allowed=false`。本 ADR 不允许 API 进程同步代替 D，也不增加自动
   发布。

## 后果

- 外部脚本收集的八组结果必须先登记为一个不可变 Campaign，D 才有正式输入身份。
- 未完成、未 advance、跨计划、跨环境或重复的 Run 无法混入正式裁决。
- 后续 D Job、read model、页面和人工签核可以引用稳定的 `campaign_id`，无需重新解释
  一组松散文件。

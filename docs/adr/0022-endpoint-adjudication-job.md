# ADR-0022：正式端点裁决由独立 Evaluation Job 执行并失败关闭

- 状态：Accepted
- 日期：2026-09-20

## 背景

ADR-0020 已定义独立 D 的证据重读与四态 verdict，ADR-0021 已把八个已完成 Endpoint
Run 冻结为不可变 Campaign。此前两者之间没有受控 Worker/Job：Campaign 创建后停在
`awaiting_adjudication`，只能由人工运行 CLI，结果也不能原子地回写控制面。

## 决策

1. Campaign 创建事务同时生成唯一的 `endpoint_adjudicate` Job。Job 只接受
   `evaluation` Worker，使用 `lease_scope=none`，其 payload 必须逐字段等于 Campaign
   保存的不可变 `adjudication_request`。
2. D Worker 使用单独的 `endpoint-formal-adjudicator-v1` Adapter Profile。部署者必须
   显式提供允许读取的本地证据根目录；Worker 不取得 HCU、签核或发布权限。
3. Job 被领取时 Campaign 从 `awaiting_adjudication` 进入 `adjudicating`。控制面只在
   Job、Campaign、payload、campaign_id 和 result 全部一致时保存结果。
4. `faster`、`slower`、`inconclusive` 进入 `awaiting_signoff`；证据校验得到的
   `invalid` 进入 `invalid`。Worker 异常、取消或不可恢复丢失进入
   `adjudication_failed`，不得伪造或推断 verdict。
5. Job completion 和 Workflow advance 保持可重放：相同结果幂等，不同 payload 或
   result replay 拒绝。所有状态继续固定 `automatic_release_allowed=false`。

## 后果

- 创建 Campaign 后可由普通 CPU Evaluation Worker 自动完成正式 D，不再依赖人工复制
  CLI 参数。
- HCU 执行权与 D 证据裁决权继续隔离；白名单根目录以外的文件不能被读取。
- `adjudication_failed` 表示基础设施或 Worker 失败，不等价于候选 `invalid`，运维人员可
  明确区分重新执行需求与证据本身无效。
- 后续人工签核和页面只消费 Campaign 的持久化 result，不直接信任 Worker 输出或临时
  报告。

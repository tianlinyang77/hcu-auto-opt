# ADR-0023：端点 Campaign 人工签核绑定当前 D 结果

- 状态：Accepted
- 日期：2026-09-20

## 背景

正式 D 只能给出 `faster`、`slower`、`inconclusive` 或 `invalid`，不能代替人决定是否接受
本次 Campaign。页面签核还必须防止用户看到结果 A、提交时却绑定到已变化的结果 B，也不能把
Agent、Worker 或浏览器自报的 actor 当作可信身份。

## 决策

1. 只有状态为 `awaiting_signoff` 且已有正式 D result 的 Campaign 可以签核；`invalid`、
   `adjudication_failed` 或其他状态不能签核。
2. 签核请求必须携带当前 D result 的规范 JSON SHA-256。控制面在同一事务中锁定 Campaign、
   重算 Hash 并比较；不一致即拒绝。
3. 决策限定为 `accepted` 或 `rejected`，分别把 Campaign 推进到 `completed` 或
   `rejected`。二者都不允许自动发布。
4. signer actor 只能由部署注入的、按 Campaign 授权的 authorizer 确认。API 未配置身份源时
   返回不可用，payload actor 与已验证身份不一致时拒绝。
5. 签核记录不可变且每个 Campaign 只能有一条。相同 idempotency key 和完全相同输入可重放；
   修改理由、结果 Hash、身份或决定后重放均拒绝。
6. Campaign Summary Read Model 只复制 Campaign、D Job、八个 Run、结果 Hash 与 Signoff 的
   持久化状态。页面不得重新计算 verdict、效应值或置信区间，只展示 D 已发布的结果。

## 后果

- UI 可以安全地展示并提交一次人审结果，丢失 HTTP 响应后仍能按同一请求幂等恢复。
- 当前只定义签核身份插槽；具体单用户、短期、Campaign-scoped 凭据由部署层提供，不把模型
  API Key 复用为登录或签核凭据。
- `accepted` 只表示人接受该次验证结论，不表示自动发布或生产灰度获准。

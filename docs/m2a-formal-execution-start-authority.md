# M2a B execution Start Authority

## 解决的问题

A3 的 `FormalStartCoordinator` 只能消费已经签名的 B Authority。测试 helper 可以构造该对象，
但不能代表部署侧真的重读了 Preview、owner window 和已注册 Adapter Profile。本切片增加
`M2FormalExecutionStartAuthorityIssuer`，让 B 在不创建 Round、不申请 Lease、不访问 HCU 的前提下
完成这一段授权链。

## 受控输入

调用方只向 issuer 提交 `preview_id` 和启动幂等键。其余字段全部从部署边界读取：

1. `M2FormalExecutionPreviewStore` 按 ID 重读完整 A2a Preview；
2. issuer 复算 Resolved Plan Hash，确认除 `formal_start_authority_not_bound` 外没有阻塞项；
3. issuer 复算并验证项目所有者窗口 Authorization 的 Hash、Signer identity、签名和当前窗口；
4. `M2FormalExecutionProfileRegistry` 返回该窗口内已注册的精确 Adapter Profile 和
   lease/fencing/cleanup policy Hash；
5. B 的部署 Signer 对规范内容 Hash 签名，输出 `FormalExecutionStartAuthority`。

客户端不能提交 Candidate Family、预算、Profile 版本/Hash、host/resource/window 或 policy Hash。
这些值全部来自 Preview、owner Authorization 和注册表。

## 时间顺序与 v2

Formal Preview 只能在 owner window 已开始后生成，因此绑定 Preview/Plan Hash 的 B Authority 也只能
在窗口内签发。`m2a-formal-execution-start-authority-v2` 明确要求：

```text
window_starts_at <= issued_at < window_expires_at <= expires_at
```

A 消费时还会拒绝 `now < issued_at` 的未来 Authority。旧 v1 要求在窗口开始前签发，与 Preview 的
生成顺序互相矛盾；项目尚未发布任何生产 v1 Authority，因此 v1 不进入生产兼容面。

## 固定边界

- 注册表是部署侧接口；仓库不提交真实 Profile 注册记录或密钥。
- owner verifier 与 B signer 的 identity/Hash 必须分离。
- issuer 只生成授权对象，不创建 StartIntent、Task、Round、Lease 或执行 Job。
- 输出固定 `synthetic=false`、`automatic_release_allowed=false`；没有性能结论。
- 未注册 Profile、窗口/Plan 漂移、签名组件缺失或异常均 fail closed。

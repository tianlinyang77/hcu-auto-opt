# M2a D evaluation Start Authority

## 解决的问题

A 的 `FormalStartCoordinator` 已要求 D 提供独立签名的 evaluation Start Authority，但测试 helper
不能证明 Search/Holdout 计划和生产 Evidence 边界来自 D 控制的部署注册表。本切片增加
`M2FormalEvaluationStartAuthorityIssuer`，把这些 pre-start 输入冻结到一个可验签对象中。

## 受控输入

调用方只提交 `preview_id` 和 Start 幂等键。issuer 从部署边界重读：

1. A2a Formal Preview、Resolved Plan Hash、Candidate Family 和协议 Hash；
2. 项目 owner window Authorization、Verifier identity 和签名；
3. 与该 Preview/Plan/窗口绑定的 Search Plan Hash；
4. 带 nonce 的 Holdout commitment 与独立 HoldoutPlanAuthority ID/Hash；
5. selection rule、family alpha、production Evidence Root 和独立 D Verifier identity；
6. D 部署 Signer，由它签署规范化 Authority 内容 Hash。

客户端不能提交或覆盖 Search/Holdout、Evidence Root、Verifier、Family、窗口或统计规则。注册缺失、
Preview/Plan/协议/窗口漂移、owner 验签失败、角色复用或 Signer 故障均 fail closed。

## 角色隔离

pre-start 链要求 operator actor、owner window verifier、B signer、D signer、HoldoutPlanAuthority 和
独立 Evidence Verifier 的 ID 与 identity Hash 全部不同。D issuer 可独立检查 owner、D signer、
Holdout 与 Evidence Verifier；A 在同时拿到 B/D Authority 后再次检查完整六方集合。

## 固定边界

- 注册表和 Signer 都是部署接口；仓库不提交 production 注册记录、Holdout 明文、nonce 或密钥。
- issuer 只发布 D Authority，不创建 StartIntent、Task、Round、Lease 或执行 Job。
- 本切片不读取终态 Evidence，不替代后续递归 EvidenceBundle/Barrier/FWER 验真。
- 输出固定 `synthetic=false`、`automatic_release_allowed=false`，没有 HCU 访问或性能结论。

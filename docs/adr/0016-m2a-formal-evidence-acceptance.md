# ADR-0016：M2a Formal Evidence 接受由 D 递归重读并独立签发

- 状态：Accepted
- 日期：2026-09-07
- 相关：ADR-0009、ADR-0012、ADR-0014、ADR-0015，Issue #102/#127

## 背景

production Evidence Root 已能验证内容寻址、对象大小和 Producer 角色，Formal Round Finalizer
也已能分别重建零晋级与 Holdout/FWER 两条终态路径。但是两者此前没有接成一个 D-owned
接受动作，`FormalEvidenceAcceptanceReview.signature` 也只是任意非空字符串。因此对象 Hash
通过仍不能证明 Round/Context/Family/Barrier/FWER 的递归语义一致，更不能证明项目 Owner 的
Signoff 来自部署 allowlist 中的身份。

## 决定

1. 调用方只能提交 `round_id` 与 `readiness_audit_id`。部署侧 Snapshot Reader 返回该终态的
   Evidence Root、Verifier、Authority 和内容寻址引用；调用方不能提交“已经验证”状态或决策。
2. D 服务先从受保护 Root 重读 `FormalAuthorityContextDescriptor`，再使用
   `ProductionEvidenceVerifier` 验证全部顶层对象、Producer 角色和输入摘要。
3. 顶层证据必须包含独立的 `round_authority`，并按终态精确包含一个 Search Barrier；
   Holdout 路径还必须包含 Reveal、Holdout Barrier 和 FWER，零晋级路径禁止携带这些对象。
4. D 服务复用现有 `M2FormalRoundFinalizer` 重建 Evidence Index 和 EvidenceBundle，不实现第二套
   Barrier、Holdout 或统计语义。
5. 项目 Owner Signoff 从受保护 Root 重读。D 服务从签名 Artifact 重建 immutable Intent，交给
   `M2FormalRoundSignoffFinalizer` 做 allowlisted 验签，并再次绑定 Round、Context、Bundle、
   Candidate/Artifact/Holdout Family 和项目 Owner 身份。
6. 只有对象、递归语义和 Signoff 全部通过，D 服务才构造
   `decision=accepted_for_formal_window` 且 blocker 为空。任何缺失、篡改、跨 Round、身份漂移或
   Signoff 拒绝都生成带稳定 blocker 的 `blocked` Review。
7. 验证摘要先发布到同一个 protected Root 并立即重读；D Review 随后由结构化签名签发。签名
   固定 Verifier ID、key ID、identity Hash、算法和值，消费方必须使用部署 allowlist Verifier
   重算 Review Hash 并验签。
8. D Review 只表示该终态证据可作为 Formal readiness 输入；项目 Owner 窗口授权仍保持
   `not_granted`。Review 不创建 Round、不访问 HCU、不提升 Baseline，也不允许自动发布。
9. Review Schema 从预留但不可接受的 v1 升为 v2。v1 只允许测试/占位的 `blocked` 记录，仓库
   从未发布生产 v1 Review，因此没有生产数据迁移；使用新字段或结构化签名必须声明 v2，禁止
   在同一个 v1 Schema 名下改变 canonical Hash 语义。

## 失败语义

- Snapshot 无法读取或返回另一 Round/audit/Verifier：不生成 Review，返回稳定身份错误。
- Snapshot 可读但证据无效：签发 `blocked` Review，保留具体稳定 blocker 和验证阶段状态。
- 验证摘要不能写入 protected Root，或 D 自身签名不能通过 allowlist：不发布 Review。
- 未知异常统一失败关闭为 `formal_evidence_verification_failed`，不能升级为接受结论。

## 后果

`accepted_for_formal_window` 不再是调用方可手工构造的布尔值，而是可重放的 D 侧验证结果。
`automatic_release_allowed=false`、`hcu_accessed=false` 和独立项目 Owner 授权边界保持不变。
本切片使用注入式确定性 Signer/Verifier 测试；生产 key、实际 Snapshot 注册和真实 D Review
仍由部署系统提供，不进入仓库。

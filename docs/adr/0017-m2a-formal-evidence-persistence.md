# ADR-0017：M2a Formal 接受快照与 D Review 采用内容寻址、只写一次注册

- 状态：Accepted
- 日期：2026-09-07
- 相关：ADR-0016，Issue #102/#127

## 背景

ADR-0016 已经定义 D 如何递归重读 Formal 终态证据并签发 Review，但当时的 Snapshot Reader
仍是注入式 Protocol，签发后的 Review 也没有持久化边界。仅靠进程内对象无法跨重启重放，
更无法证明同一个 Round/audit 没有被换成另一组 Evidence Root、Authority 或引用。

## 决定

1. PostgreSQL 新增独立的 Snapshot Registry 与 D Review Store。Snapshot 和 Review 均保存完整
   Contract JSON 及 canonical SHA-256；Round/audit、内容 Hash 和 Review ID 均有唯一约束。
2. 同一个 `round_id + readiness_audit_id` 只能绑定一个 Snapshot 和一个 Review。相同字节重复
   注册是幂等成功；相同身份但不同字节必须冲突，不能 last-write-wins。
3. 两张表都由数据库 Trigger 禁止 UPDATE/DELETE。内容模型在每次读取时重新验证，Snapshot
   重新计算 Hash，Review 重新验证自身 Hash 与 Snapshot 的全部终态绑定。
4. `DeploymentFormalEvidenceAcceptanceRegistry` 同时实现 ADR-0016 所需的 Snapshot Reader，
   并在发布、重读 Review 时调用部署 allowlist 的签名 Verifier。未验签的记录不能进入 Store，
   已存记录也不能因为曾经验过一次就跳过重验。
5. protected Evidence Root 仍保存原始 Formal 对象。PostgreSQL 保存的是查找所需的冻结 Snapshot
   和签名 Review，不复制或替代 Root 中的 Measurement、Barrier、FWER、Bundle 或 Signoff 事实。
6. 提供一个鉴权只读 GET Report。服务端必须注入 Report Service 和鉴权器；任一项缺失返回 503，
   鉴权拒绝返回 403，持久化 Hash/签名/绑定漂移返回 422。没有 POST、Signoff 或接受端点。
7. 本迁移只提供 Schema 和代码路径，不写入生产 Snapshot、Review 或 key，也不代表真实 Formal
   Round 已发生。`owner_window_authorization=not_granted`、`hcu_accessed=false`、
   `automatic_release_allowed=false` 固定不变。

## 数据流

```text
部署内部注册器
  -> write-once Snapshot(round + audit + snapshot_hash)
  -> ADR-0016 D 验证服务从 Registry.read() 重读
  -> protected Evidence Root 递归验证
  -> allowlisted D Signer 生成 Review
  -> Review Store 再验签并绑定 Snapshot Hash
  -> 鉴权 GET 重读 Snapshot + Review、重算 Hash、重新验签
```

## 失败语义

- 并发提交相同内容：一个创建、其余幂等重放。
- 并发提交相同身份的不同内容：冲突，不覆盖先到记录。
- Snapshot/Review JSON、列级身份、Hash、签名或跨对象绑定不一致：读取失败关闭。
- 数据库或鉴权/Verifier 配置不可用：不降级成未鉴权读取。

## 后果

Formal D 接受记录现在可以跨进程重启重放，并能作为 #102 readiness 的受保护输入；它仍只是
“该终态证据通过独立复核”，不是 HCU 时间窗口授权、性能结论、Baseline 提升或发布许可。

# ADR-0012：Real Operator Profile 只由精确 Formal 窗口授权激活

- 状态：Proposed / A1 Contract implemented, no window authorized
- 日期：2026-09-02
- 跟踪：GitHub Issue #125、总体 Issue #102
- 依赖：ADR-0009、ADR-0010

## 背景

Scripted Operator Facade 已能用 synthetic Profile 演示 Plan/Start，但 Real Profile 一旦进入
Catalog，后续组件就可能把它解析为 Formal 输入。旧构造器虽然默认拒绝 Real Profile，却保留
`allow_real_profiles=True` 布尔参数；它没有绑定 readiness、四方评审、Candidate Family、预算、
具体设备或时间窗，不能成为生产授权。

Formal readiness 又和项目所有者授权属于两个连续但不同的决定：前者回答“实现是否已经具备
申请窗口的条件”，后者回答“是否允许在这个主机、资源和时间内运行这组确切输入”。把项目
所有者授权作为 readiness 自身的通过条件会形成循环依赖。

## 决策

### 1. 移除裸布尔开关

普通 `OperatorProfileCatalog` 永远只接受 synthetic Profile。Real Profile 只能通过独立的
`build_formal_operator_profile_catalog()` 建立；该入口必须收到授权对象、对应 readiness
Manifest/Report 和部署侧签名 Verifier。

### 2. 授权对象内容寻址且范围精确

`m2a-formal-profile-window-authorization-v1` 冻结以下内容：

- readiness audit ID/base Commit、Manifest Hash 和 Report Hash；
- target/workload/measurement 三个 Profile 的 ID、Version、Kind 和内容 Hash；
- business source Family Hash 和不可放大的 Round Budget；
- 精确 host、resource、窗口开始/失效时间；
- 项目所有者、授权证据 URI/Hash 和部署侧 Verifier/Key 身份；
- `formal`、`degraded_manual_intake`、`synthetic=false`、
  `automatic_release_allowed=false`。

授权内容先计算 canonical SHA-256，再由部署侧 Verifier 验证签名。Contract 只定义 Verifier
接口和身份绑定，不在仓库中保存生产密钥，也不生成 nmz36 的实际授权实例。

### 3. readiness 与窗口授权分两步

readiness Report 只有在所有实现 gate（不含 `owner_window_authorization`）和 A/B/C/D review 均
通过时，才可成为 `ready_for_window_authorization`。此时 owner gate 必须仍为 `hold`，其策略
证据必须可验证。随后项目所有者签发或拒绝独立窗口授权；授权不得反写或覆盖历史 readiness
Manifest/Report。

### 4. 注册时和使用时都 fail closed

注册时重新计算授权、Manifest、Report 和 Profile Hash，并核对 Candidate Family、Budget、
Resource、Stage 0 协议、Adapter、Workload 以及四方接受记录。授权缺失、决定为 rejected、签名
失败、Verifier 身份漂移、尚未生效、已过期或任一 Authority 漂移时拒绝注册。

Catalog 即使在有效窗口内建立，每次把 Real Profile 解析为可运行输入时仍检查窗口；过期后只
能保留只读历史，不能启动新工作。

## 影响

- A2 Formal Plan Compiler 可以消费一个已验证且带授权 Hash 的 Catalog，不需要信任客户端字段；
- Scripted Profile、Plan 和 Start 路径不变；
- readiness 审计不再因尚未发生的 owner 决定而永远无法到达“可申请窗口”；
- 部署必须在 A4 提供生产认证和签名 Verifier，缺少配置时维持关闭。

## 当前非授权声明

本 ADR 和 A1 代码只建立无 HCU Contract 与门禁。当前没有签发 nmz36/HCU 7 授权，不注册真实
Profile，不创建 Formal Round，不产生性能结论，不提升 Baseline，也不启用自动发布。

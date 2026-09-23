# Formal 候选进度只读展示

关联 PR #169、Issue #167、ADR-0025（Proposed）。

本次把现有 RoundCandidate 的状态和制品 ID/Hash 接入受保护的派发状态读取接口及
正式入口页面；读取精确核对 Intent 成员集合，不新增写权限。状态使用中文说明，
明确区分构建、正确性、采样和裁决，不将 built/measured 当成加速或发布结论。

验证：

- 后端定向单测 11 passed（2.97s）。
- PostgreSQL 构建归档与派发组合回归 9 passed（141.32s）；新增断言确认归档后
  read_status 返回该候选 built 及对应 Artifact ID/Hash。随机 schema 和临时隧道已清理。
- Web lint、57 项单测、build、4 项 Sites 包装测试通过。构建仍有大于 500kB 的 chunk 警告。
- 浏览器打开本地正式入口并检查首屏；未输入生产凭据，因此没有完成授权后候选卡片
  的浏览器端联合验收。测试中的构建输出、身份和授权为显式夹具；没有 HCU 验收。

交付收口顺序见 docs/plans/mvp-delivery-cutline.md。当前只提升真实进度可见性，
不代表构建 Worker、预算结算、正确性推进或正式端到端已完成。

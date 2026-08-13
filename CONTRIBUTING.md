# 贡献与协作

## 分支和 Pull Request

- `main` 禁止直接推送；所有改动通过 Pull Request。
- 分支使用 `feat/...`、`fix/...`、`docs/...`、`experiment/...`。
- 至少一名 CODEOWNER 审核，跨模块接口还需接口另一侧负责人审核。
- 默认 Squash Merge。PR 应尽量小于 500 行有效改动。

## Contract 变更

`src/dcuopt/domain/`、数据库 Schema、事件格式和 Worker 协议属于 v0.1 Contract。
Contract 可以演进，但必须：

1. 新增或更新 ADR；
2. 上游负责人同意；
3. 下游负责人同意；
4. A 批准；
5. 同时提交迁移、兼容或契约测试。

禁止在调用方偷偷兼容一个未记录的新字段。

## 测量规则

- B 维护唯一测量 Harness；其他模块只能消费它。
- 任何性能结论必须保存环境指纹、原始样本、计时计划和基线版本。
- 未通过 Stage 0 时，不得把任何加速数字作为项目成果。
- RMSNorm 等夹具结果只用于验证管道，不代表业务收益。

## 完成定义

Issue 完成至少满足：

- 输入、输出与错误语义符合 Contract；
- 有单元测试，涉及 DB 并发时有 PostgreSQL 集成测试；
- 有可复现实验或明确说明无需硬件；
- 更新相应文档与 ADR；
- 不引入第二套计时、状态或 Artifact 格式。


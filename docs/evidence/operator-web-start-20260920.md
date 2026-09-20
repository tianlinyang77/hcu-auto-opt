# Scripted 页面启动与刷新恢复验收

范围：真实浏览器、FastAPI、PostgreSQL 随机隔离 schema；测试输入为 synthetic fixture。
未运行 HCU、模型服务或性能评测，未改变正式 Endpoint Campaign 与签核。

## 实际操作

通过页面选择 Target / Workload / Measurement Profile、两个候选，生成 Preview，填写
测试操作者并确认预算，点击启动。首次请求返回 500，但保存了原幂等键。
刷新页面后出现恢复入口，确认恢复原请求，控制面返回 finalized，启动审计显示 2/2
候选绑定和 version=7。GET 回读与页面一致。

- Preview：`27170151-dad0-5eed-8183-1bdb0cb84eb0`
- Intent：`cf334fe6-89bf-59e9-ac90-827c1a02503e`
- Round：`1064d988-83e1-5153-8e4d-77ced0e6656a`
- 幂等键：`ui-start-5cc75a7d-2723-4ae3-95ce-2b9527369386`
- `automatic_release_allowed=false`

## 联验发现与修复

初次 500 原因为应用主机时钟比数据库快约三秒。创建 Intent 使用应用时间，后续状态
推进使用数据库 now()，触发 operator_start_time_order 约束。修复为首次入库的创建和
更新时间统一使用数据库 now()；后续推进沿用数据库时间。没有修改主机频率或系统时钟。

浏览器恢复验证使用原进程，在时间差消失后恢复原 Intent。另在新建隔离 schema 中执行
修复后的 PostgreSQL 回归：注入比数据库快一小时的应用时间，首次入库使用数据库时间，
完整推进到 finalized，最终时间顺序正确。该回归 schema 已清理。

页面恢复验收和修复回归为两次独立检查，不能描述为修复后重新执行了一遍全部浏览器流程。
临时浏览器演练 schema 保留到验收页面停止时清理，不作为正式优化证据。

## 结论边界

已验证 Scripted 轮次创建、PostgreSQL 持久化、页面恢复和启动审计；finalized 不代表
构建、正确性、Search/Holdout 或正式 HCU 执行完成。关闭标签页或清空浏览器数据后的
跨会话恢复不在本次范围内。

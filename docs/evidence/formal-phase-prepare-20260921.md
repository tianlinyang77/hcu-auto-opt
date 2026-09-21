# Formal 阶段请求装配验证

关联 PR #169、Issue #167、ADR-0025（Proposed）。

## 已实现

- 从已有 Round、Candidate、Authority、授权 Reader、预算计划组装 B 的原有请求契约。
- 制品、Family、Profile、阶段计划与授权窗口不接受 deployment 参数覆盖。
- Lease/拓扑/Target Lock 材料全部必填，不生成占位 Hash 或临时设备授权。
- 装配复用 B 的授权校验，缺材料或状态不符时不占用阶段日志槽位。
- Consumer 增加默认关闭的 prepare_and_execute_once，之后复用已有一次消费和检查点。

## 验证

命令：

```text
python -m pytest -q tests/unit/test_formal_phase_prepare.py tests/unit/test_formal_phase_consumer.py tests/unit/test_formal_execution_checkpoint.py tests/unit/test_m2_formal_execution.py tests/unit/test_formal_dispatch.py
```

结果：80 passed、2 skipped，7.54s。Skip 为已有 Windows 符号链接权限用例。
覆盖 Search/Holdout 请求等价、每项部署材料缺失、禁止覆盖、制品/正确性/轮次/预算
错误、过期授权、默认关闭、准备后调用与回执重放。Ruff 通过。

采用显式签名/源码/租约/预算/Harness/日志夹具，实际执行 B 适配器和本地回执存储代码。
本轮没有 PostgreSQL、HCU 或前端重测，不沿用此前结果声称本轮通过实机验收。
首个组合测试命令误引用不存在的 test_formal_claim.py，零测试运行；修正为上述真实
文件后才得到所列通过结果。首次 Ruff 提示长行，已修正并重新检查。

## 剩余生产接线

1. 当前 Formal Round 仍可能在 intake_closed；必须完成真实构建、正确性及 Family 冻结。
2. 部署读取器需要从权威存储取当期材料，不能把本文件测试夹具用于生产。
3. Lease/Target Lock 实时采集、调度启动、运行中协作停止和当前资源恢复仍待完成。

装配通过不是设备可执行证明，更不是性能通过或发布许可。automatic_release_allowed
继续为 false；未改频率、未接触他人服务。GitHub CI 按用户要求暂不等待。

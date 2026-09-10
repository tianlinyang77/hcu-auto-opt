# BW20 transport 实机兼容记录

## 2026-09-10 CPU-only 演练

- 范围：项目本地；固定 BW20 Docker27.2.1 与现有 OpenSSHCommandRunner。
- 症状：创建失败但没有原始原因；修复诊断后又遇到 bytes 无法写入JSON；正常退出后清理误判。
- 原因：传输层误以为 CommandResult 为文本；`--pid=private` 不是有效 Docker 参数；
  原不存在回执白名单未包含本机daemon文案。
- 修复：按原bytes契约在传输边界UTF-8解码；保留创建失败的plan/result供对账；
  省略PID模式参数并核验实际独立namespace；增加精确完整CID的daemon不存在文案。
- 验证：真实CPU正常/超时两条链路通过，完整收据见 `bw20-stage0-integration.md`。
  bytes契约、创建失败、异常CID、默认拒绝CPU测量协议均有测试覆盖。
- 不要重复：不要用只返回str的测试runner代替真实bytes契约；不要因namespace权限错误
  改成hostPID/sudo；SSH失败不等于容器消失；没有CID的创建不确认必须对账，不能按名称盲删。
- 保留失败：`4c6aa877...`/`4aeedd96...`/`6e5e9bf3...`为创建失败尝试，第二份诊断JSON
  因bytes序列化中断而不完整；`2263488c...`为正常退出后清理误判。均保留在操作任务目录，
  不篡改为成功；最终完整成功记录为`37756ec6...`。
- 适用边界：仅CPU传输、进程身份和容器清理；不能作为Stage0/HCU测量或优化收益依据。

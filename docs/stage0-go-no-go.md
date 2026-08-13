# Stage 0：前置 Go/No-Go

## 三个独立闸门

### G0-M 测量可信度

输出：

- 完整环境指纹；
- 计时 API 与计时开销；
- 有效计时分辨率；
- 跨进程 σ、CV、置信区间；
- 在给定预算下的 MDE；
- 已知大信号夹具是否被正确识别。

失败动作：全项目停止接受性能结论。

### G0-P Profiler 探针

只回答当前 DTK 是否能够稳定产生：

- Kernel 名称和耗时；
- Shape/dtype/meta；
- Python/HIP 调用位置或足够的关联信息；
- 可机器解析的输出。

失败动作：进入人工候选降级模式，禁止自动发布；若连粗粒度热点也不可得，则只保留配置轨道。

### G0-H 热补丁探针

验证一个候选能否：

1. 替换；
2. 启动新进程并生效；
3. 完成正确性与性能运行；
4. 清理缓存并恢复基线；
5. 证明恢复后的 Hash 和行为与基线一致。

失败动作：改为启动时 overlay、配置轨道或更换目标；`_C.so`/系统库进入阶段二。

## 决策权

- 任意成员可以提出 `STOP`。
- B 可因测量证据不可信直接停止性能实验。
- A 是 Go/No-Go DRI，负责记录决定并改变项目状态。
- 从停止状态恢复需要 B（测量可信）、D（判定协议有效）、A（范围和资源）三方同意。

## 能力结果

Stage 0 不是简单布尔值，而是能力声明：

```json
{
  "measurement": "pass",
  "profiler": "pass|degraded|fail",
  "hot_patch": "pass|overlay_only|fail",
  "automatic_release_allowed": false
}
```

MVP 默认 `automatic_release_allowed=false`，即使全部探针通过也只产生人工签核候选。


# BW20 rotary 数值验证结果（2026-09-09）

结论：**48/48 个冻结用例通过；仅为 bounded cases，不是完整验收。**
用户批准待审设备范围，并允许同范围诊断继续推进，不再逐项询问。

## 本次实际执行

- UTC 10:14:00.405—10:14:48.045，SSH 与 Docker die 均为退出码0。
- 容器 `087312153f0b6f83969d134015e8503195dd92ea006e190c53abd08b5d6a50d4`。
- Python3.10.12 / torch2.10.0 / HIP6.3.26113 / gfx936，PCI `0000:b1:00.0`。
- 实际加载 `sgl_kernel/common_ops.cpython-310-x86_64-linux-gnu.so`，
  SHA256 `6c1cb45465cff84b6563fa930a73a2da6e4091539c17d55291a765864944291e`，
  与 r2 安装清点一致；wrapper 模块 `sgl_kernel.elementwise`。
- live HostConfig 独立确认：UID/GID65534、断网、只读root、cap-drop ALL、
  no-new-privileges、CPU64–79/NUMA4、8GiB RAM/无额外swap、PID256、
  仅 kfd/renderD135、唯一宿主挂载只读 `/opt/hyhal`、4GiB exec tmpfs。
- 同一容器 die0 后 destroy；精确名称不存在。HCU7 busy=0、VRAM=2207744 bytes，
  与前测一致，KFD进程目录为空。没有修改库、共享源码、模型、时钟或其他进程。

## 数值覆盖与结果

Q14/KV2/d64、BF16、NeoX；tokens1/8/248/256，seeds0/17，连续/带head padding布局。
每组配置 eager + 2次换输入的 graph replay，48个唯一用例，48项副作用检查通过。
独立CPU FP32参考，冻结 `atol=rtol=1/128`；全体 mismatch_count=0。

- Q 最大绝对误差：0.007781982421875。
- K 最大绝对误差：0.0076904296875。
- 近零值的相对误差可以较大；判定采用冻结的绝对+相对混合容差，不单看相对最大值。
- 原始日志保留每组 max/mean absolute/relative、RMSE、输入输出Hash、stride和位置。

**警告未消失：**实际调用仍报告 launch448 超过 compiled bound256。
本次证明固定库在这些输入上的输出满足容差，不能证明所有输入安全、编译来源完整、
模型实际dispatch相同或该警告已修复。未采集 kernel function attributes。
`model_path_verified=false`、`launch_bound_issue_resolved=false`、
`framework_pair_accepted=false`、`automatic_release_allowed=false` 保持不变。

## 证据

目录：`results/bw20-rotary-correctness-20260909/`（本地保留，未入Git）。

| 文件 | SHA256 |
| --- | --- |
| executed-input.py | `9f49f995ca2d22d6b9da3776046f8065d30f55e0d7e1dcb5ef893245551dc072` |
| stdout.log | `977cfdf000f95e737776fe41acc6cbad4ae3b24c9fcda040706ab8567221d037` |
| stderr.log | `9ed01c3f35110f07b72c84fbf91dd86af7b1eaa08d2b1e017db3baabbc277038` |

另含 execution.json、preflight.txt、hostconfig-observation.txt、docker-events.jsonl、
cleanup-observation.txt。stderr 还记录不可写 hylog 的提示；未为日志创建宿主可写挂载。

下一步：真实 SourceManager/Builder/Store 制品与隔离成对执行；D按原证据契约判断，
不把本报告用作 Stage0、性能提升或发布许可。

# BW20 rotary 数值验证（已授权并执行，保留原审核范围）

2026-09-09 更新：用户已授权，48/48用例通过，清理完成；警告仍存在。
实际结果与边界见 `bw20-rotary-correctness-result.md`。以下保留执行前冻结方案。

这是总体验收中的算子诊断，不是优化候选或性能测试；不修改源码、库、模型或时钟。
采用 kernel-model-triage 的独立 reference/预冻结容差/副作用检查规范，保留原系统门禁。
硬件知识库仅用于背景核对，实际设备绑定仍以现场 PCI/NUMA/容器可见性为准。

## 固定输入与判定

- 固定 r2 的 `sglang-kernel` 版本及 common_ops SHA256（详见 CPU 清点报告），
  先校验文件，再验证实际加载的 `sgl_kernel.common_ops.__file__` 与哈希。
- 仅 gfx936 / PCI `0000:b1:00.0`，物理 HCU7、renderD135、逻辑0。
- BF16、Q heads14、KV heads2、head_dim=rotary_dim64、NeoX half-split；
  token 数1/8/248/256，seed0/17；连续布局及每个 head 加8个 padding 的 strided 布局。
- 使用 Qwen 配置的 rope_theta=1000000、max_position=32768；CPU 生成再量化 BF16
  的 cos/sin cache。位置覆盖0、32767和重复位置。输入为确定性的正负 sin/cos，
  幅度不超过1，并补0、±1和1e-4。不是生产所有值域/所有 layout 的穷尽证明。
- reference 不调用 kernel 或项目自带 reference：对实际 BF16 输入/缓存做独立
  CPU FP32 half-split 数学计算。运行前冻结逐元素
  `abs(actual-reference) <= 1/128 + (1/128)*abs(reference)`。
  输入幅度<=1时该阈值用于容纳 BF16 乘法、加减及最终存储舍入；不因失败放宽。
- 每个配置做 eager + 2次 graph replay，两次 replay 更换输入和位置但保持地址，
  共48组 Q/K 比较。检查指针未变、padding/前后哨兵未写、positions/cache 未改。
- 保存输入/输出 Hash、位置、stride、误差 max/mean absolute/relative、RMSE、
  mismatch count 和前16个失败位置；原始日志包含所有警告。NaN/Inf、加载身份错误、
  任一数值/副作用失败立即停止并返回非0。
- 只有全部48组通过才输出 `bounded_cases_passed`。仍保持
  `launch_bound_issue_resolved=false`、`framework_pair_accepted=false`、
  `automatic_release_allowed=false`。warning 消失或这些有限样例通过，都不等于
  launch bound 的来源问题已修复或完整模型路径已验收。

程序：`src/hcuopt/deployment/bw20_rotary_correctness.py`。
审核版本 SHA256：`9f49f995ca2d22d6b9da3776046f8065d30f55e0d7e1dcb5ef893245551dc072`。
本地13项纯 CPU reference/判定测试、Ruff、Python3.10语法检查通过；
Windows 没有 torch，因此尚未执行 torch/CUDAGraph 路径，不能冒称已实机验证。

## 资源与安全范围

2026-09-09 只读观察：HCU7 busy=0、VRAM used=2207744 bytes、KFD进程目录为空；
renderD135 指向 b1:00.0、NUMA4。仅是瞬时观察，不是窗口预约。
设备节点权限666，故本次拟用 UID/GID65534，不需要 root 或额外设备组权限。

- 执行前复核主机、镜像ID、PCI/NUMA、设备占用和同名容器不存在。
  发现忙卡或未知 KFD 进程就停止，不杀其他进程、不开第二张卡。
- 仅映射 `/dev/kfd` 和 `/dev/dri/renderD135`；唯一宿主挂载为只读 `/opt/hyhal`
  （现场解析为 `/usr/local/hyhal`，与之前成功 smoke 一致）。
- 不挂模型、源码或 Docker socket，不启动 SGLang server；stdin 传入固定脚本。
- CPU64–79/NUMA4、8GiB主机内存/不额外 swap、PID256、4GiB exec tmpfs、shm1GiB、
  单文件上限2GiB。PyTorch allocator 设置为设备容量1/32（约2GiB）；这不是设备驱动
  或第三方分配器的全局硬上限，实际使用在前后观察并记录。
- 断网、只读root、cap-drop ALL、no-new-privileges；最多300秒+10秒kill grace。
  除新容器临时目录外无写入，stdout/stderr/HostConfig/events 保存在本机新证据目录。
- 结束 `--rm` 删除本次容器，复核设备恢复。失败保留证据，不临时扩大权限、重编库、
  禁用 graph 或改容差来过关。此授权不包含完整模型成对运行。

## 待审核的精确命令

在 BW20 执行，stdin 是上述审核脚本的原始字节：

```bash
docker run --rm -i --pull=never \
  --name hcuopt-bw20-rotary-correctness-20260909 \
  --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --user=65534:65534 \
  --cpuset-cpus=64-79 --cpuset-mems=4 \
  --memory=8g --memory-swap=8g --pids-limit=256 \
  --device=/dev/kfd --device=/dev/dri/renderD135 \
  --mount type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=4g --shm-size=1g \
  --ulimit fsize=2147483648:2147483648 \
  -e HOME=/tmp -e XDG_CACHE_HOME=/tmp/cache -e TRITON_CACHE_DIR=/tmp/triton \
  -e HF_HOME=/tmp/huggingface -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e OMP_NUM_THREADS=8 -e PYTHONNOUSERSITE=1 -e PYTHONPATH= -e PYTHONHOME= \
  -e HIP_VISIBLE_DEVICES=0 -e ROCR_VISIBLE_DEVICES=0 -e HSA_VISIBLE_DEVICES=0 \
  --entrypoint /usr/bin/timeout \
  10.16.1.152:5000/jenkins/model_test_env/sglang@sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c \
  --signal=TERM --kill-after=10s 300s python3 -I -
```

通过后仍需按完整框架要求取得真实制品/执行环境/同模型 Baseline-No-op/D/清理证据，
才能评审成对功能闭环。CPU清点和本诊断不代替该验收，也不产生性能提升结论。

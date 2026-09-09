# BW20 单案例环境接入

本页延续原有 Target → Source/Artifact → 唯一 Harness → D 独立验真 → 人工签核链路。
新增目标不改变公共 Contract，不注册可运行的 Real Profile，也不复制旧机器的验收状态。

## 已确认与未完成

2026-09-09 的人工授权环境探针已验证：固定 SGLang 0.5.12/Python 3.10 镜像在
github-bw20 上可以导入 PyTorch，单个元素的设备分配、同步和读取成功。它**没有**验证
SGLang 服务启动、模型加载、源码等价性、计时分辨率、噪声或任何加速收益。

| 项目 | 原 nmz36 | 新 BW20 |
| --- | --- | --- |
| 架构 | 原 Target 配置 gfx938 | 实测 gfx936 |
| 物理设备 | HCU7 | HCU7，PCI 0000:b1:00.0，renderD135 |
| NUMA / CPU | 7 / 112–127 | 4 / 64–79 |
| 驱动 | 原 Target 配置 6.4.0 | 实测 6.3.31-V1.5.1 |
| Stage 0 | 已有历史证据 | pending，不可继承 |

只映射 renderD135 的容器中，物理 HCU7 被枚举为逻辑 `cuda:0`。不能在此命名空间
继续设置 `HIP_VISIBLE_DEVICES=7`。探针在分配前核对可见设备数、PCI 地址和架构。

镜像 digest 为 `sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`，
Image ID 为 `sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`。
它包含之前从固定源码 Commit 现编的 SGLang 包，不是另换一个 latest 镜像。
宿主没有 DTK 安装，容器继续使用自己的 DTK；`/opt/hyhal` 按原部署规定只读挂载。
首次缺少此挂载导致 `librocm_smi64.so.2` 导入错误，补齐后通过。

## 如何查看配置和生成复跑命令

在安装本项目的 Python 环境中，从仓库根目录运行：

```bash
python -m hcuopt target-validate config/targets/bw20-sglang-0.5.12.yaml
python -m hcuopt.deployment.bw20_environment config/targets/bw20-sglang-0.5.12.yaml
python -m hcuopt.deployment.bw20_environment config/targets/bw20-sglang-0.5.12.yaml --format shell
```

后两条只打印计划或 POSIX Shell 命令，**不会执行 Docker、申请资源或写入验收状态**。
这是部署诊断工具，不是第二套测量 Harness。输出中 `executed=false`、
`stage0_accepted=false`、`production_profile_registered=false`。

执行打印出的命令前，在获准窗口内用 SSH 确认 hostname、Image ID、PCI/render 绑定和
设备占用均匹配；名称冲突时不要删除已有容器。只在目标主机执行，不在本机执行，且不要
直接 `eval` 未审核输出。原宿主 Python 3.6 不满足本项目要求，不向它安装依赖。

容器断网、只读根文件系统、不提权、无 Docker socket、只映射单卡及 HYHAL 只读目录，
CPU64–79/NUMA4，4 GiB 内存、128 PIDs；任务超时 120 秒，再给终止宽限 10 秒。
执行后复查精确容器名称、HCU7 显存和 KFD PIDs。该工具不停止任何其他容器或进程。

## 配置中的频率不是既成事实

`expected_sclk_mhz=1500`、`expected_mclk_mhz=1800`、`manual` 是从现场支持档位选出的
**待评审测量策略**，没有应用，也没有获得改频率授权。现场读到的是 auto / 600 / 1800 MHz。
`measurement_clock_policy_unapproved` 仍阻塞 Stage 0。环境探针不读这些字段来调整频率。
若策略变化，必须更新目标指纹；不得用空闲瞬时频率当作稳定测量环境。

## 后续按这一条线收口

已在 `/home/github/hcu-auto-opt-runtime/sglang-das` 准备独立的干净 detached checkout：
Commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，Git tree
`a883d7eb4b4667768c19f0c9b0456d41f52da91c`；无 submodule 条目或工作树改动。
`python/sglang/srt/mem_cache/allocator.py` SHA256 为
`ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27`。
这是源码准备证据，不是全树 canonical source hash、wheel 重建或运行包等价性结论。

1. 核对已准备源码与镜像内包/Overlay 的关系，冻结模型和单案例输入。
2. 接入新目标专属部署 Profile；共享已有控制面、Build、Harness 和 D，保留旧 nmz36 的
   硬绑定，不靠改标签借用旧 Adapter。
3. 核对 Worker 的单设备映射、取消/超时、资源恢复和预算闭环，获得当期范围内的执行授权。
4. 针对 gfx936 新目标重新采集 Stage 0，再运行固定案例、形成新 Run 的独立验证和页面结果。

源码与运行包等价性、工作负载、部署接线、时钟策略、测量窗口与 Stage 0 均保持对应 open blocker。
本文件与配置不能自动解除它们，也不能充当 owner Authorization、签名或人工接受。

探针日志曾出现 `/var/log/hylog` 不可写警告，计算正常结束；没有为隐藏警告扩大挂载权限。
结束时观测到短暂 KFD PIDs，其中一个映射 HCU0，另一个未给出设备；复查时均已退出，
无法确认归属。保留这个不确定性，不将瞬时空闲写成整机独占。

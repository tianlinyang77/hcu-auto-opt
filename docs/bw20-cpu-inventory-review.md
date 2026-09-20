# BW20 库指纹清点：待授权的 CPU 容器

状态更新：首轮已经用户授权并执行完毕，结果见 `bw20-cpu-inventory-result.md`。
下面两条命令保留为审批历史；r2 也已单独获准执行，结果见同一报告文末。

2026-09-09。此步骤服务于当前“完成验收”目标，尚未执行，不代表硬件/正确性验收。

现场只读核对：主机 `github-bw20 / 10.17.1.20`；固定镜像 Image ID
`sha256:16fd28e795d55585657efe9a30ead1e2a457c8268e4fd21b53e8137fd9964012`；
SGLang 源码仍为 `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`，git status 为空。
主机 Python3.6.8 不满足清点工具运行要求。当前没有本任务可复用的清点容器，
其他容器保持不动。

## 待审核的精确容器命令

以下命令在 BW20 执行；标准输入为本地
`src/hcuopt/deployment/kernel_binary_inventory.py` 的已审核完整内容，不是交互式命令。
执行时本地记录该文件 SHA256 和 stdout/stderr，不上传或修改共享源码。

```bash
docker run --rm -i --pull=never \
  --name hcuopt-bw20-kernel-inventory-20260909 \
  --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --user=65534:65534 \
  --cpuset-cpus=64-65 --cpuset-mems=4 \
  --memory=2g --memory-swap=2g --pids-limit=64 \
  --entrypoint /usr/bin/timeout \
  10.16.1.152:5000/jenkins/model_test_env/sglang@sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c \
  --signal=TERM --kill-after=10s 180s python3 -I -S -
```

- **零 HCU 映射、零宿主机 bind mount、零外部网络**；非 root 用户，只读容器根目录。
- 最多2个 CPU、2GiB主机内存、64 PID、180秒+10秒强制退出；不改频率。
- `-I -S` 禁用环境注入、用户 site 和 site/.pth 启动执行，工具只通过标准库读取
  安装元数据与 RECORD 所列 `.so` 内容，不 import SGLang/sgl-kernel/AITER。
- 名称冲突直接停止，不删除已存在容器；结束由 `--rm` 清理本次容器。
  如需清理异常残留，只允许按本次记录的精确 container ID/名称处理。
- 若权限或包元数据不足，保留失败/缺失证据，不临时增加权限/设备/挂载。

## 这一步能证明与不能证明的事

可补齐：固定镜像 Python 版本、包版本、安装清单、共享库 Hash、缺失项。
不能证明：真实 rotary dispatch/launch bound、算子数值正确性、成对运行等价性、
性能提升、Stage 0 或发布放行。清点后仍需按结果继续后续验收，不以此结束总目标。

## r2：补查源码声明的发行包名（已单独获准并执行）

固定源码声明发行包为 `sglang-kernel`，而不是首轮查询的 `sgl-kernel`。
新的脚本仅补加该发行包的元数据/库哈希查询；继续不 import 扩展或执行设备代码。
隔离参数保持不变，新名称防止覆盖首轮记录。精确命令如下，stdin 为修正后的
`kernel_binary_inventory.py`：

```bash
docker run --rm -i --pull=never \
  --name hcuopt-bw20-kernel-inventory-20260909-r2 \
  --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --user=65534:65534 \
  --cpuset-cpus=64-65 --cpuset-mems=4 \
  --memory=2g --memory-swap=2g --pids-limit=64 \
  --entrypoint /usr/bin/timeout \
  10.16.1.152:5000/jenkins/model_test_env/sglang@sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c \
  --signal=TERM --kill-after=10s 180s python3 -I -S -
```

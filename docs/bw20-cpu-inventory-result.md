# BW20 CPU 库清点结果（2026-09-09）

最新状态：r2 已单独获准并执行，取得真实 `sglang-kernel` 版本与共享库 Hash，
详见文末。首轮记录保留；总体验收尚未完成。

## 执行与边界

用户批准 `bw20-cpu-inventory-review.md` 的一次 CPU 清点后实际执行。
客户端 UTC 时间 `08:24:27.838`—`08:24:30.774`，SSH 返回0，stderr为空。
Docker die 事件也记录 exitCode=0，随后存在同一 ID 的 destroy 事件。
容器 `c0089e5c8af7153783925196bdb52161386572ddea26a48a07a367caded9cd80`
已自动删除，之后按精确名称查询为空；没有清理其他容器。

运行中的 HostConfig 已独立采集：network=none、readonly root=true、cap-drop=ALL、
no-new-privileges、CPU64–65/NUMA4、memory=memory-swap=2147483648、PID64、
Devices=[]、Binds=null、Privileged=false、AutoRemove=true。非 root UID65534
是本次提交命令中的配置，未另行采集 Config.User；不把它误称为独立实测字段。

本次 Python 为3.10.12；site启动已禁用，未加载 sgl_kernel/sglang/aiter 模块。
没有模型请求、设备分配、性能测量或库修改。

## 实际清点

| 查询的发行包名 | 观察结果 |
| --- | --- |
| `sglang` | `0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1`；1个列入 RECORD 的共享库 |
| `aiter` | `0.1.3+das.opt1.dtk2604.torch2100.2606011547.gf165f1`；52条共享库路径，共1834188416字节，含 build/ 根目录重复副本；不是52个独立算子 |
| `sgl-kernel` | 未找到这个发行包名称的元数据；不能等价为 `sgl_kernel` Python 模块不存在 |

AITER `aiter/jit/module_pos_encoding.so` 的 SHA256 为
`67ff73e3f6e689254b120149588013eb2aa7cef0341e0cbed41c000529791eb9`。
它只是一个已找到的文件，**尚未证明成功请求中的 rotary kernel 来自这个库**。

## 新发现与下一步

进一步只读核对固定源码 commit `dad582f28458cd0e11e0be675fbe7fcc7ab65ac1`：
`sgl-kernel/pyproject.toml:10` 实际写的是 `name = "sglang-kernel"`，
源文件版本为 `0.4.2.post2`，wheel.packages 是 `python/sgl_kernel`。
这解释了为什么必须区分发行包名和 import 模块名；**源码版本不能当镜像安装版本**。

已修正清点入口，后续分别查询 `sgl-kernel` 与 `sglang-kernel`，保留第一轮原始结果
不覆盖。原一次性容器授权执行后，r2 又单独获准并完成。
总体验收尚未完成：runtime dispatch/数值验证及真正成对运行
仍有缺口，不把这次 CPU 清点当作完成 Stage0、性能或发布验收。

## 原始证据

本地目录 `results/bw20-kernel-inventory-20260909/`（未入 Git）：

- `executed-input.py`：实际执行脚本快照，SHA256
  `df86b8c221d5da67e8e892b2a746efa610793bbc1dbba0a4d31487e38d5eb712`。
- `stdout.json`：原始包/库指纹，SHA256
  `52df85b583ea0e5368bd85936c8dca77e306ff688dcaf9d5013de978dd4d3245`。
- `stderr.log`：空文件，SHA256
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- `execution.json`：精确命令、客户端时间与退出码。
- `hostconfig-observation.txt`：运行中容器配置的原始观察。
- `docker-events.jsonl`：该容器 die/destroy 事件；无其他容器操作。

## r2：真实 kernel 发行包与共享库（已执行）

客户端 UTC `09:16:58.544`—`09:17:01.514`；SSH 返回0，stderr为空。
容器 `b2364f397d83dfeda035efbbbd287feb465ccd9d207f715791a7301e5752a633`
的 die(exitCode=0) 和 destroy 事件均已保存，结束后精确名称查询为空。
运行中 HostConfig 的无设备、无 bind mount、断网只读、2GiB、CPU64–65/NUMA4、
PID64、非 privileged、自动删除等字段独立复核通过。未操作其他容器。

实际版本和库文件：

```text
sglang-kernel = 0.4.2.post2+das.opt1.dtk2604.torch2100.2606021957.gdad582
/usr/local/lib/python3.10/dist-packages/sgl_kernel/common_ops.cpython-310-x86_64-linux-gnu.so
bytes = 28133784
sha256 = 6c1cb45465cff84b6563fa930a73a2da6e4091539c17d55291a765864944291e
RECORD sha256 = 1528e723ba839e1c807a01768a2b9e5842ff1d290a2b0e2d948fe66633b7d90d
```

两轮 SGLang/AITER 的完整清点对象逐项比较一致；没有版本/路径/Hash 漂移。
冻结源码 `sgl_kernel/__init__.py` 调用 `_load_architecture_specific_ops()`，
`load_utils.py` 同时存在架构目录、根目录和标准 import 回退路径。因此这次
找到的库是明确候选，但安装清单不能证明模型实际加载了哪个分支，版本后缀也
不等于完整编译来源/参数证明。已知 launch448/bound256 警告仍待数值验证。

下一步：获批设备范围内捕获实际加载路径/Hash，使用独立 reference 检查真实
rotary 算子的 eager 与 graph 输出，再做成对框架验证；不再重复相同包名清点。

r2 原始证据目录 `results/bw20-kernel-inventory-20260909-r2/`：

- `executed-input.py` SHA256：
  `3fbdf66de77257ed202c3c927dd222eb1f4ceffbf9905a18289021aed443e2fe`。
- `stdout.json` SHA256：
  `1864fb71f65486ce1ba737290352c5b25872d6d9c7ff011e31381ae4ed107050`。
- `stderr.log` 空；`execution.json`、`hostconfig-observation.txt`、
  `docker-events.jsonl` 保留精确命令、运行配置与清理证据。

没有改库、频率或模型，没有提交/推送，没有新 HCU/性能测试。

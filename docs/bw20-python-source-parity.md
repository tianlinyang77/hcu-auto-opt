# BW20 Python 源码与镜像对照（2026-09-09）

本次用无 HCU、断网只读 CPU 容器，逐文件比较共享冻结源码、镜像保留的 wheel、
以及实际安装目录；未 import SGLang/torch，未修改任何一方。

源树1974个 Python文件，wheel和安装目录各1973个；wheel与安装目录的 Python文件
集合及Hash一致。与源码共有4条集合/内容差异，不能称“逐字完全一致”：

| 路径（sglang/ 下） | 实际差异 | 归类 |
| --- | --- | --- |
| `_version.py` | wheel/安装目录有，源树没有 | 生成的版本文件；commit_id=null，不能用作完整源码证明 |
| `multimodal_gen/.claude/skills/sglang-diffusion-benchmark-profile/scripts/bench_diffusion_denoise.py` | 源树有，wheel/安装目录没有 | 未打包的开发脚本 |
| `multimodal_gen/.claude/skills/sglang-diffusion-benchmark-profile/scripts/diffusion_skill_env.py` | 源树有，wheel/安装目录没有 | 未打包的开发脚本 |
| `srt/models/qwen2.py` | wheel/安装目录启用了被源树注释的 `get_rope_config` 导入 | 已有最小运行时修复，必须显式记录 |

`qwen2.py` 唯一差异：

```diff
-# from sglang.srt.utils.hf_transformers_utils import get_rope_config
+from sglang.srt.utils.hf_transformers_utils import get_rope_config
```

源文件 SHA256 `bb5899b3d94bbb0455c78d939aaa1ae48aee91a70d12bbab34921d0bfb3f270f`；
wheel/安装文件 SHA256 `fc93b92e4ff7b44376e4d445ecba8fc907a83f147a482dad7c823fe52269b48d`。

旧 `config/targets/nmz36-sglang-0.5.12.yaml` 已声明这个最小修复；
`docs/f1-sglang-cookbook-validation.md` 记录的 wheel SHA256
`20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa`
与本次逐字节校验一致。因此这是已记录修复的现场确认，不是未知漂移；
但不能自动继承旧机器的框架、测量或人工验收状态。

原始对照和逐文件 Hash：`results/bw20-python-source-parity-20260909.json`；
差异及版本文件：`results/bw20-python-source-diff-20260909.txt`。
两次都是 CPU stdout 诊断，不是平台正式 D EvidenceBundle。

尚未证明 native sgl-kernel 编译参数/源码归属、模型实际dispatch、候选加载或完整成对执行。
后续 Source/Runtime 绑定必须纳入这条固定补丁和wheel身份；不可将 No-op源码 tar
直接描述成镜像安装代码，也不可只因本次对照就清除 Target gate。

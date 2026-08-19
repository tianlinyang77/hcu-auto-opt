# nmz36 SGLang F1 现编 wheel 镜像

这个目录定义 F1 Framework Smoke 使用的派生镜像。它从锁定 Commit 的独立
Worktree 现编 SGLang wheel，再把 wheel 安装进 Target Lock 中的不可变基础镜像；
不覆盖原镜像，也不直接修改基础镜像里的 `site-packages` 源文件。

镜像只包含三项确定性修复：

1. 将 `kernels` 从不兼容的 `0.15.1` 锁定为 Transformers 5.6.0 要求的
   `0.12.3`。
2. 将 `apache-tvm-ffi` 从 `0.1.0` 更新为 SGLang、SGL DeepGEMM 和
   XGrammar 共同要求的 `0.1.9`。
3. 安装包含 `qwen2.py` 的 `get_rope_config` 导入修复的现编 wheel。

Dockerfile 在安装前校验 wheel SHA256，并把基础镜像 digest、源码 Commit、
源码修复补丁 SHA256 和 wheel SHA256 写入镜像标签。构建前需把
`SGLANG_WHEEL_FILE` 指定的 wheel 放到本目录；wheel 本身不提交到 Git。
Python 依赖默认从清华 PyPI 镜像
下载，也可以在构建时通过 `--build-arg PIP_INDEX_URL=...` 显式覆盖。

构建时必须使用唯一标签，禁止覆盖 Target Lock 中的原标签：

```bash
docker build \
  --file images/nmz36-sglang-f1/Dockerfile \
  --tag 10.16.1.152:5000/jenkins/model_test_env/sglang:<unique-tag> \
  images/nmz36-sglang-f1
```

发布前必须依次通过：包版本检查、`import sglang`、HCU 7 可见性、
Qwen2.5-0.5B 模型加载、FA3 Ready 探针、一次 `/generate` 请求和容器清理后
HCU 7 显存归零。只有 Registry
返回 digest 后，才能更新 Target Lock 的 `tag`、`image_id`、
`registry_digest` 和 `immutable_reference`。

# Formal 候选真实 Overlay 构建适配

关联 PR #169、Issue #167、ADR-0025（Proposed）。

## 交付与范围

FormalRoundCandidateBuilder 默认关闭，校验冻结 business 候选后调用原 M1 Builder。
复用真实 Git worktree、源码包校验、内容寻址只读制品、构建缓存和清理。
输出原有 BuildResult/BuildTerminal；无 DB 状态修改、无授权或预算绕过。

## 验证记录

- 首轮 Windows 7 个 setup error：共享夹具需要符号链接权限。新增测试改为普通文件
  最小 Git 仓库，不删除或修改既有符号链接覆盖。
- 第二轮 Windows 6 passed、1 failed：真实 LocalArtifactStore 发布后清理只读临时
  文件触发 WinError 5。这是既有跨平台限制，本轮没有放宽只读保护，也未声称修复。
- Linux Python 3.10：在现有项目容器独立临时目录运行下列测试，**12 passed，2.60s**。
  未安装依赖、未重启容器、未调用 HCU。源码通过归档放入临时目录，不覆盖部署代码。

```text
python -m pytest -q tests/unit/test_formal_candidate_builder.py tests/unit/test_m1_candidate_builder.py
```

成功路径实际运行 Git、生成 Overlay 文件、校验内容和只读权限、读取缓存，并确认
Baseline Hash 不变、无 Candidate Worktree 遗留。另覆盖禁用、fixture 候选、Manifest、
Store、Profile、状态错配及注入源码应用异常后的清理。候选源码和 Profiler 引用均为
显式测试夹具，不能据此宣称真实业务热点构建已验收或性能提升。

远端首轮传输因容器 rootfs 只读而不能 docker cp；改为容器内 tar 解包至可写临时目录。
首轮 Linux 测试缺少配置文件，补齐 config/targets 后才得到上述通过结果。

## 下一步

正式构建 Worker 的授权/预算/执行日志、结果原子落库，以及正确性与 Family 冻结。
现有 Scripted 的 synthetic 检查保持不变；不自动发布、不关闭总 Issue。

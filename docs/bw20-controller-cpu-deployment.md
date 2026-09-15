# BW20 同机 CPU 控制端部署验证

2026-09-11。**控制端源码和隔离依赖已落地，Linux/Python3.10 工程回归通过。**
本次没有启动常驻 API/Worker 服务、创建 HCU 容器、修改时钟或接受 Stage0 性能结论。

## 现场部署

- 主机：`github-bw20` / `10.17.1.20`，用户 `github`。
- 部署目录：`/home/github/hcu-auto-opt-runtime/bw20-stage0/bb0fb828-e8c8-47e7-826e-bcbed4aaab41`。
- `controller/`：228个文件，包括 Python、注册协议 YAML 和数据库迁移 SQL。
- `venv/`：Python3.10.20；通过清华源安装
  `config/deployments/bw20-controller-cpu-requirements.txt` 中的固定直接依赖。
  未安装主机 Torch/DTK，未改全局 Python；完整解析版本保存在本地证据的
  `requirements-resolved.txt`，不能把直接依赖 pin 当作全量哈希锁。
- `cpu-validation/`：CPU 单测及公开 Target/协议配置；不是生产任务目录。
- 源码 manifest：`sha256:45d99ea8e720af1ffd09fd11081248b76c007c4c4818fd3b516335cb0c815672`。
- 归档：`sha256:c1ba7de0aafa90cbdc3ab3f8503164e4ba474a55d497d22e05c35751aa80d42f`。

该快照包含未提交代码，`92e021d4388187e6aa2770ef2ae5d1c9b17c7a4a` 仅为 base commit，
**不能用它代替以上 manifest 描述实际运行字节**。快照无 `.git`，API导入需显式设置
`HCUOPT_SOURCE_COMMIT`；正式部署还应使用最终交付 commit 和独立实例身份。
本次 CPU 验证没有把此 base commit 用于接受任何优化证据。

## 结果与可复查证据

- 真实 Python3.10：API、Worker SDK、原 RuntimeProbe/D verifier、BW20七探针组合导入通过。
- `BW20LocalCommandRunner` 直接运行本机 Docker 只读版本查询，返回27.2.1；没有绕过
  SSH host-key 检查，也不需要容器挂载 Docker socket。runner仅允许BW20非root github环境。
- 同机 `stage_controller` 路径实际成功：新建另一独立UUID目录，复制归档后两次独立
  source manifest核验一致。此处fence=1只是计划身份，没有伪造Worker租约或启动容器。
- 工程回归：**394 passed / 1 skipped / 1 warning，13.47秒**。跳过的是仅用于非POSIX的
  fail-closed用例；原Windows下跳过的POSIX证据读取测试及新增本地归档检查在Linux执行。
  warning是Starlette引用anyio弃用别名。测试含明确替身，不是七探针真实硬件运行。
- 源码测试前后重新独立核验，228文件及manifest保持一致。

本地证据（`results/`不提交Git，不包含凭据）：

1. `bw20-controller-cpu-recheck-562b963d-1aae-49e1-a706-1e7ec52c98b0/report.json`
   SHA256：`ff3db627c42ba94e4b103fbb0fad49c3c70fe0c29ef5307508a3d18487ef20c1`。
2. 同目录 `pytest.log`：`9c0ae4a4ffed4093c365548a741d84179769bb2323bb69b1bf875e68ee477263`。
3. `bw20-local-staging-6e82da18-310f-4db0-8be4-4889cee32320/report.json`
   SHA256：`7cac5c3f4936afd22ad8e330312947ef758914931e832b12edad6a7d4b630825`。

首次 `dcde5858-...` 缺少SQL、第二次 `bb0fb828-...` 未设置源码身份的失败报告均保留。
后续在第二次的相同源码/venv上补充启动环境并复核，成功报告明确链接原失败报告，未改写历史。
没有后台测试进程遗留；两套隔离环境及源码目录保留用于复查，未删除旧证据。

## 复查 CPU 入口

在BW20主机执行以下命令；不启动模型、不接入数据库、不创建容器：

```bash
cd /home/github/hcu-auto-opt-runtime/bw20-stage0/bb0fb828-e8c8-47e7-826e-bcbed4aaab41/cpu-validation
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONPATH=../controller/src:.
export HCUOPT_SOURCE_COMMIT=92e021d4388187e6aa2770ef2ae5d1c9b17c7a4a
../venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_bw20_local_runner.py tests/unit/test_bw20_stage0_capabilities.py tests/unit/test_bw20_stage0_cache_protocol.py
```

这只是专项复查，不要把其数量等同于上面的394项扩大回归。

## 距离七探针正式运行还差什么

1. 准备BW20真实C RuntimeProbeProfile：固定Profiler命令、模型/源码挂载、候选制品与
   三阶段执行参数；不能将单测夹具登记为真实候选或沿用No-op归档当作热补丁生效证明。
2. 完成Target中源码/运行时等价性、workload、实际adapter绑定的当期证据审查。
3. 确认正式窗口和manual时钟设置/恢复范围。现场仅只读观察到HCU7无后台进程、
   auto/600/1800MHz；瞬时空闲不等于预留，不能静默解除这两个blocker。
4. 按原控制面实际领取租约，再运行七探针、原D判决；通过后再跑真实Agent优化候选。

本轮未修改Target状态、没有注册生产Profile，也未提交或推送代码。

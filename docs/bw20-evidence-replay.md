# BW20 验收证据一条命令复核

## 简版（2026-09-10）

已将临时验收工具中的核验逻辑整理进正式 `hcuopt` CLI。拿到完整原位证据和可信收据Hash，
不连接SSH、数据库，不占用HCU，就能重新检查历史API联验的文件一致性、容器范围及清理记录。
缺失、篡改、越界或不合格记录均返回非零退出码。它不签核、不产生新的实机或性能结论。

## 本机使用

在已有依赖安装好的工作树执行：

```powershell
Set-Location C:\wa\bw20-target-onboarding
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m hcuopt bw20-evidence-verify results/bw20-api-acceptance-37e51bbb-6171-4c27-a8da-e4d66c7a97aa --receipt-sha256 sha256:43664fb9f4334cdc82365d358fdfd590f4d4873188abe0a7d604ad3e363175bb
```

已经安装当前源码包的环境，也可以使用 `hcuopt bw20-evidence-verify`。
`--help` 显示参数。本命令默认只输出JSON，不写文件、不覆盖原始收据。
本机上述路径存在；普通clone不会带上被忽略的原始results，需要另外通过受控方式交付证据。

- 退出0：`historical_evidence_verified=true`，只代表这份历史记录复核通过。
- 退出2：核验失败，JSON给出稳定错误码；不包含原始凭据或证据内容。
- 参数缺失：argparse显示用法并退出2。
- `persisted_task_state` 是记录时状态，不是当前数据库状态。

## 信任与适用边界

收据Hash必须来自已信任的独立验收记录或人工保管渠道。不要对来历不明的收据现场算Hash，
再把相同Hash输入当作真实性证明。Hash绑定不是数字签名，更不授予签核/发布权限。
可信收据固定plan、API Summary、数据库回读和EvidenceBundle；程序继续检查原始文件清单、
成对输出、进程日志Hash、容器实录和退出/销毁事件。不是只看收据里一个true。

当前是BW20冻结F1夹具专用工具：固定输出/Token数量、CPU64–79/NUMA4、renderD135等检查
来自已批准的案例，不是通用任意模型验收器。没有改公共协议或默认Target/Catalog。
历史日志URI为绝对路径，必须保留原位目录；目录迁移/可移植证据包尚未实现，不能手改历史URI。
核验时应保持证据目录只读且稳定；本工具不是对并发恶意修改目录的操作系统级隔离方案。

Python `-O` 模式也会执行所有检查。旧 `results/verify_bw20_api_acceptance.py` 作为历史验收
来源保留，避免改变旧收据所记录的verifier Hash；后续操作优先使用正式CLI。

## 实现和验证

- `src/hcuopt/deployment/bw20_evidence.py`：核验函数、错误码、路径限制、外部收据Hash。
- `src/hcuopt/cli.py`：惰性加载新的只读子命令，不注册新Worker或执行Profile。
- `tests/unit/test_bw20_evidence.py`：合成小夹具测试，不作为实机证据。
- 与结果页、Worker竞态、Agent CLI合并回归：41passed、1Windows符号链接权限skip；
  1项既有Starlette弃用警告。Ruff通过。
- 子进程测试覆盖普通/优化模式的完整成功和篡改拒绝；其他用例覆盖不完整清单、路径越界、
  文件缺失、容器设备/权限/网络范围变化、缺销毁事件、签核或发布范围扩大。
- 本机真实结果目录普通模式和 `python -O` 复核均退出0；本次没有远程执行或修改数据库。

## 后续建设顺序

1. 已完成：正常API实机联验、结果页只读接入、历史证据CLI复核。
2. 2026-09-10已补正式结果页serve/status/stop、只读连接、凭据生命周期及故障测试，
   保留原4191/4192/4193服务；4194新实例见[部署说明](framework-viewer-deployment.md)。
   数据库凭据仍需由部署环境受控注入，不提供团队秘密管理服务。
3. 人工审核/签核：沿用原控制面身份及状态机，不把一句“继续”当成已接受证据，不能自动代签。
4. BW20 Stage0、真实候选激活/恢复、Agent/Apex生成候选的真实优化评测仍另行推进。
   不能因这份No-op流程证据通过就声明优化收益或放开自动发布。

本轮代码保持未提交/未推送；不是已经部署到main的新版本。

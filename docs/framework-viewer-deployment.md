# 本机结果页：配置化部署与受控停止

## 简版（2026-09-10）

结果页已具备正式CLI的 `serve / status / stop` 入口。每次启动使用独立访问/控制凭据，
只读连接现有PostgreSQL；可选SSH隧道由实例自己创建和清理。停止先验证实例身份，
不按PID、进程名或端口批量杀进程。原4191、4192、4193服务未被停止或替换。

新实例访问地址：
`http://127.0.0.1:4194/?frameworkSmoke=b90b2fdb-24da-4e70-98ca-1a64d38bd0af`。
这仍是既有BW20框架联验的只读结果页，不是自动签核或新一轮性能验收。

## 启动

1. 准备已安装依赖的当前工作树，以及已有前端构建 `web/dist/client`。
2. 使用 `config/deployments/bw20-framework-viewer.example.json`。配置包含task_id、
   schema、profile、静态构建目录、端口及可选SSH目标，不包含数据库密码或模型密钥。
   相对静态目录按配置文件位置解析。更换任务必须明确更换配置；不会自动选“最新任务”。
3. 由本机秘密管理方式向进程注入 `HCUOPT_VIEWER_DATABASE_URL`，包含显式的
   host=127.0.0.1、port、user、password、dbname。不要把真实值写进Git、命令参数或聊天。
   CLI读取后从本进程环境删除该项，避免继续传给它创建的SSH子进程。
4. 运行：

```powershell
Set-Location C:\wa\bw20-target-onboarding
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m hcuopt framework-viewer serve config/deployments/bw20-framework-viewer.example.json
```

当前机器已有测试数据库凭据的读取方式仍由本机操作桥接脚本
`results/launch_managed_viewer.py`完成：只读inspect既有测试容器，凭据仅在进程内存中流转，
随后调用上述正式CLI。该桥接脚本不是仓库通用秘密管理器，也不随普通clone分发。
团队部署需通过自己的受控凭据注入方式配置环境变量，不能依赖本人的results目录。

如配置了 `ssh_target`，程序用严格host-key检查、免密非交互SSH，在127.0.0.1分配临时转发端口；
只转发到指定主机的127.0.0.1数据库端口。不创建/启动远端容器，不执行远端业务命令。
没有配置SSH时直接使用既有loopback数据库连接，不拥有也不关闭外部提供的隧道。

服务前台运行。启动完成后打印URL、实例目录、凭据文件路径，不打印凭据内容。
复制 `access-key.txt` 内容到页面的访问凭据框；`control-key.txt` 仅供本机管理命令使用，
不要当作页面密码。页面访问凭据不能停止服务，也不能签核。

## 查看状态和停止

用启动输出中的实例目录：

```powershell
python -m hcuopt framework-viewer status <实例目录>
python -m hcuopt framework-viewer stop <实例目录>
```

正常启动窗口也可以按Ctrl+C退出。stop返回stopping表示已请求停止，之后status的本地记录
应为stopped；不可把已发出停止请求当作进程已结束。已停止记录会注明source=local_record。
活动实例由控制凭据和UUID双重校验，并先核对真实任务ID，再发出停止请求。
端口被别的程序占用时拒绝启动，不自动释放该端口。

正常停止/启动失败会撤销本实例访问，删除两份临时凭据文件，保留不含凭据的instance.json，
并结束自己持有的SSH子进程。不会递归删除目录，也不会停止其他隧道、页面、CI或HCU进程。
重启生成新UUID和新凭据，不复用旧文件。Windows实例位于解析后的LocalAppData私有目录，
先设ACL再生成凭据；普通其他用户不应有权限，系统和管理员仍可能访问。不要把它当作密钥保险库。

## 边界与故障

- 服务只绑定127.0.0.1，不面向公网，无TLS/企业SSO/多用户授权。
- PostgreSQL强制loopback host/hostaddr、明确schema和只读事务；没有migrate、写任务、签核等调用。
  只读连接设置是程序约束；仍建议团队提供数据库侧只读账号作为额外保护。
- 首次读不到真实任务就启动失败，运行中数据库故障返回503，不回退Demo或缓存成功结果。
- 托管SSH断开会使服务退出并清理；未托管的外部数据库连接故障不会自动终止外部隧道。
- 强制杀死进程、系统断电不会执行Python finally，可能留下文件或隧道；status会报告控制不可达，
  不根据陈旧PID自行杀进程。需人工核对归属后处理。此版不是系统服务管理器。
- 没有新增人工签核按钮，也没有把只读页的控制凭据接到控制面写API。
- 前端源码、Sites打包、字体及hosting配置未修改；没有外部发布或浏览器视觉验收。

## 实际验证

- 生命周期/结果页/Agent CLI分组28passed；证据核验分组26passed、1项Windows符号链接权限skip。
  首次联合运行有一项MemoryError，随后两组单独复测通过；没有把它描述成一次无中断全绿。
- Ruff与diff检查通过。测试覆盖访问/控制凭据分离、实例身份不匹配、占用端口保护、
  只读DSN约束、数据库失败撤销凭据、自己创建的SSH失败清理及实际HTTP启动/停止。
- 真实数据库实例f144bd01-7ef3-4cda-b804-87399e8c2843完成启动、CLI状态查询、CLI停止，
  进程退出0；目录仅保留instance.json，两份临时凭据已删除。
- 重新启动实例bb4f4955-702d-4e54-8020-53756a1fbbd8，4194上保持运行；
  无凭据401、合法凭据200、静态页面200、四项检查true、awaiting_signoff、自动发布false。
  原始回读见 `results/managed-viewer-live-readback-20260910.json`。
- 当前实例凭据ACL实查包含当前用户、SYSTEM、Administrators和OWNER RIGHTS，没有普通Users/Everyone。
- 本轮没有占用HCU、修改数据库、签核、提交、推送或合并。

## 下一步

先评审并提交本轮累积的工程改动，补人工签核的显式身份/确认交互；之后继续独立的
BW20 Stage0与真实候选激活/恢复链路。已通过的No-op联验不需要为了页面改造重复跑。

## 自查修订与部署更新（2026-09-10）

以下状态覆盖前文旧实例的“保持运行”：bb4f4955实例已通过正式stop入口停止，
状态记录为stopped，两份凭据文件均已删除；本次仅清理自己的临时凭据，重新启动可生成新凭据。
当前4194实例为 `d6fbc2f5-76fb-487a-b3b6-dff3ca895aa1`，URL不变，访问凭据已轮换。

- 异常退出/中断先撤销页面访问，再等待自有SSH隧道停止。
- 服务线程未退出不能返回成功；两份凭据分别尝试删除，任一删除失败会记录failed并非零退出。
  失败记录不保证所有资源已消失；仍需核对归属，不能按旧PID批量清理。
- 可人工审核提示必须绑定当前任务、最新评测和最新证据，新增只读字段 `review_evidence_id`。
- 结果页/服务生命周期/历史证据回归：58passed、1项Windows符号链接权限skip、1项已有Starlette警告。
  新故障注入测试首次因替换全局Thread影响Windows subprocess而失败；改为模块局部替身后复测通过。
- 实际回读：无凭据401、有效凭据200、静态页面200；四项检查true，
  `review_evidence_id=a371a627-269d-5961-8dcf-06ffa4c968a2`，任务仍为awaiting_signoff。
  新证据：`results/managed-viewer-review-d6fbc2f5-76fb-487a-b3b6-dff3ca895aa1.json`。
- 原始验收证据通过独立CLI再次核验，未改写原证据；未运行新HCU任务、签核或发布。
- 人工签核下一切片详见 [签核接入边界](framework-viewer-signoff-integration.md)，尚未实现。

以上仅为结果页/部署模块自查与回归，不代表已审完工作树全部累积改动；尚未提交或推送。

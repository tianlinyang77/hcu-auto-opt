# Endpoint 结果页统一入口

本入口使用前端正式构建，不依赖 Vite 开发服务器。它管理自己的 SSH 隧道，显示指定
Campaign 的实时控制面结果，提供状态查询和受控停止。适用于单用户工作站；远端 API
和数据库应已由管理员部署完成。它不会在启动页面时启动 HCU 测量。

## 首次安装

使用 Python 3.10 或更新版本，在仓库根执行：

```text
python -m pip install -e .
cd web
npm ci
npm run build
cd ..
```

复制并调整 `config/deployments/bw20-endpoint-console.example.json`。配置中不包含密钥。
`static_root` 相对配置文件解析；`ssh_target` 需已有免密 SSH 和已验证的 known_hosts。
`ssh_remote_port` 是远端 loopback 控制面端口。已有外部隧道时可省略 `ssh_target`，
此时 `ssh_remote_port` 是本机既有 API 端口。示例 Campaign 为已签核的历史验收记录。

## 每次打开

```text
python -m hcuopt endpoint-console serve config/deployments/bw20-endpoint-console.example.json
```

启动器先检查真实 Campaign，再输出网页 URL 和 `instance_directory`。打开 URL 即可查看，
无需每次运行 npm 或手工建立隧道。服务前台运行，当前终端 Ctrl+C 可关闭本实例。

另一个终端可以使用启动输出的实例目录：

```text
python -m hcuopt endpoint-console status <instance_directory>
python -m hcuopt endpoint-console stop <instance_directory>
```

`status` 区分本地运行状态和 `upstream=available/unavailable`。`stop` 的 `stopping`
表示请求已接收；再次查询为 `stopped` 才表示服务完成清理。停止通过实例身份和独立本地
控制凭据验证，只关闭自身隧道，不停止远端 API、数据库或其他进程。

## 失败时怎么处理

- 启动失败：检查前端构建、配置中的 Campaign、VPN、SSH known_hosts 和远端 API。
- 端口被占用：改配置端口；启动器不会杀掉占用端口的程序。
- API 不可用：页面返回明确错误，不回退到 Demo；VPN 恢复后刷新。
- 自有 SSH 已退出：实例退出，恢复连接后重新运行 serve。
- 强制终止后遗留状态：控制命令不会依赖旧 PID 杀进程，应核对实际实例归属。

## 当前范围与下一切片

这是 #46 易用性建设的第一个收尾切片，只开放指定 Campaign 的读取以及本地实例管理。
签核和新建任务的写 API 不通过此入口转发。已签核结果可直接展示；等待签核的 Campaign
应继续使用已经部署的独立签核入口。控制凭据不能用于人工签核。

后续切片将现有 Plan Preview / Start Intent 页面与获准的写接口连接，补环境与工作负载
选择、预算确认、启动和错误引导。不得把只读页面部署成功当作任务启动功能已验收。

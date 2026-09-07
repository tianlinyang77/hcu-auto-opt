# Agent 结果页：单用户只读部署

第一版按项目所有者选择使用独立、按 Run 授权的访问凭据，不接企业 SSO。这里交付部署入口和
联验方法，不代表服务器已常驻部署，也不是 HCU/性能验收。凭据仅用于看结果，不可生成候选、
签核、晋级或发布，更不能与模型 API Key 混用。

## 服务边界

`hcuopt.api.inspection_server` 是独立 FastAPI 实例，不挂载完整控制面。只注册：

- `GET /healthz`：进程存活，不查询数据库或文件；**不是环境已就绪**。
- `GET /v1/operator/agent-generations/{run-id}/inspection`：登录后读取该 Run 的 D 复核结果。

没有创建任务、调度、模型执行、签核、迁移、静态文件目录、Swagger 或旧证据入口。即使进程
设置 `HCUOPT_AUTO_MIGRATE=true`，本服务也不迁移。环境工厂对数据库连接设置只读事务；部署
仍须使用专门的只读数据库账户，不能把这个选项当作数据库角色权限的替代。

## 准备凭据（Linux，CPU 即可）

运行用户须有既有 Evidence Store 的读取权限。下面的 `/private/...` 是示意路径，由部署方
选定，不是安装脚本自动创建的真实目录。先准备该用户独占、权限为 0700 的目录，不复用源码
库、网页目录或共享 `/tmp` 根目录。凭据目录不可挂给 Agent 或加入 Git。

```text
python -m hcuopt.api.inspection_server create-access \
  --access-file /private/viewer/access.json \
  --credential-file /private/owner/credential.txt \
  --run-id <已有且已准备inspection的Run UUID> \
  --ttl-seconds 3600
```

可重复 `--run-id` 指定最多 32 个明确 Run，不接受通配符。程序随机生成 256 位访问密码，
access.json 只保存用户名、密码 SHA256、允许 Run、到期时间；明文仅写入指定 credential.txt。
两份文件均以 0600、排他创建，已有文件一律拒绝覆盖，控制台不输出密码。默认 1 小时，生成
命令最多允许 24 小时。只向服务挂载 access.json；明文文件保留在操作者私人目录。

文件写入出错时可能保留一份已创建文件；失败不等于可用，不会覆盖或自动删除已有文件，
检查后使用全新的路径重新生成。不得靠保留部分文件宣称部署成功。

每次 GET 重新读取配置，并验证 owner-only 权限、原生无链接安全读取、凭据摘要、Run、
到期时间。删除配置会立即失败关闭（503）；到期/移除 Run/轮换密码后旧凭据返回 403。
轮换通过新凭据文件完成，由部署方原子替换服务器配置或重启引用新配置，不要求重新生成
Proposal/Receipt。修改配置必须继续维持 0600。已交付给读者的数据不能靠撤权追回。

## 启动专用入口

在 Linux Python 3.10 环境安装项目。将以下配置通过部署进程提供，不把数据库密码写入 Git、
命令参数或终端输出：

| 环境变量 | 用途 |
| --- | --- |
| `HCUOPT_INSPECTION_DATABASE_URL` | 专用只读 DB 账户的连接配置 |
| `HCUOPT_AGENT_INSPECTION_ROOT` | 原 Worker Store/审核前快照的受保护只读根目录 |
| `HCUOPT_INSPECTION_ACCESS_FILE` | 本用户私有的 access.json 绝对路径 |

**不要设置 `HCUOPT_MODEL_API_KEY`**；环境工厂发现该值会拒绝启动，模型密钥属于另一个 Worker。
API 用户可读证据但应通过文件系统只读挂载/ACL 防止写入。URI 根目录必须与原始证据一致，
不能随意搬迁后修改 Receipt 的路径或 Hash。

```text
python -m hcuopt.api.inspection_server serve --port 8091
```

该入口仅监听 `127.0.0.1`，默认关闭访问日志和代理头信任，不新建容器、不映射设备。
远程浏览可经获准的 SSH 转发，或由部署方提供 HTTPS 反向代理。非本机明文 HTTP 被拒绝；
`X-Forwarded-Proto` 本身不会获得信任。需要代理识别 HTTPS 时，必须按部署边界配置可信
代理，不能对任意地址启用代理头。服务没有全局 CORS 放行。

## 浏览器怎样看

独立服务采用 HTTP Basic 登录，由浏览器的原生登录提示输入 `operator` 和私有文件中的密码。
不把密码放在 URL、JavaScript、localStorage、截图或聊天中。Basic 编码**不是加密**，只能
在上述 HTTPS 或本机/SSH 通道中使用；浏览器可能缓存密码，但服务器不会缓存权限判定。

前端静态文件与 API 通过同一个受控源提供。先访问该源下的具体 inspection URL 完成登录，
再访问 `/?agentInspection=<run-id>`；沿用原页面，不启用 demo 回退。前端无需获得模型 Key。
代理仅转发这一条只读 API，不把整个 `/v1/` 指向完整控制面。当前仓库未自动安装/启用代理，
也未验收真实浏览器的登录提示行为，部署时需实测，不能用 TestClient 的 HTTPS 字符串替代
真实 TLS 或浏览器联验。

## 验收清单

- 无登录：401；错误凭据/跨 Run/过期：403；配置缺失、权限错误或损坏：503。
- 有效凭据 + 当前证据：200；证据 Hash/状态漂移：422；数据库或底层读失败：503。
- 成功与失败均 `Cache-Control: no-store`，禁止共享 HTTP 缓存复用敏感结果。
- 证据读取之前完成登录和 Run ACL；拒绝路径不触碰 A 数据和 Evidence。
- 修改/撤销配置后，下次读取立即拒绝；后台状态不出现 Review、Promotion 或新 Attempt。
- POST/PUT/PATCH/DELETE 和控制面路径不存在（404/405），无数据库迁移。
- 真实数据库测试使用随机隔离 schema，真实密码校验和原生 D Reader；模型为本地模拟服务。

本文件是开发部署方案，不是生产安全认证。多用户审计、速率限制、企业 SSO、密码管理平台、
全站安全策略和 HTTPS 代理运营不在本切片的完成范围内。实际部署前仍需确认主机、端口、
只读数据库账号、Run/Evidence 根目录和资源范围；不能为了页面可见更改既有系统门禁。

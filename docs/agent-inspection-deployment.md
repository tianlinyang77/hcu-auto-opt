# Agent 结果页：单用户只读部署

第一版按项目所有者选择使用独立、按 Run 授权的访问凭据，不接企业 SSO。这里交付部署入口和
联验方法，不代表服务器已常驻部署，也不是 HCU/性能验收。凭据仅用于看结果，不可生成候选、
签核、晋级或发布，更不能与模型 API Key 混用。

## 服务边界

`hcuopt.api.inspection_server` 是独立 FastAPI 实例，不挂载完整控制面。只注册：

- `GET /healthz`：进程存活，不查询数据库或文件；**不是环境已就绪**。
- `GET /v1/operator/agent-generations/{run-id}/inspection`：登录后读取该 Run 的 D 复核结果。
- `GET /v1/operator/agent-generations/{run-id}/evidence`：相同 Run ACL 下，独立重读已登记的 D 终态报告。

没有创建任务、调度、模型执行、签核、迁移、静态文件目录或 Swagger。即使进程
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
| `HCUOPT_AGENT_INSPECTION_ROOT` | 原 Worker Store、审核前快照及终态报告引用的受保护只读根目录 |
| `HCUOPT_INSPECTION_ACCESS_FILE` | 本用户私有的 access.json 绝对路径 |

**不要设置 `HCUOPT_MODEL_API_KEY` 或 `HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE`**；环境工厂
发现任一值都会拒绝启动，模型密钥属于另一个 Worker。
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

### 仓库自带的本机入口

前提：部署管理员已配置专用只读后端、有效 Run 凭据，以及指向后端的本机 SSH 隧道。
前端命令不自动连接主机、不创建/续期凭据、不操作数据库或 HCU。Node.js 使用项目 CI 的
22 或兼容版本。在 `web/` 下执行一次安装和构建：

```text
npm ci
npm run build
```

之后每次启动只需：

```text
npm run inspection:serve -- --port 4191 --api-port 8091
```

打开 `http://127.0.0.1:4191/?agentInspection=<获准的Run UUID>` 并登录。
终态报告使用 `http://127.0.0.1:4191/?agentEvidence=<获准的Run UUID>`；两个入口在登录页
提供明确切换链接，切换需重新登录，不把凭据放进 URL。不能同时指定两个入口。
无需复制 `results/` 下的临时脚本；该命令固定监听 127.0.0.1，并只连接本机 API 端口。
静态页面仅来自可信的 `dist/client` 构建产物，不能将 Evidence/私有凭据放入该目录，运行
期间不要由不可信主体更改构建文件。该工具是本地预览代理，不是生产网关或云端部署入口。

只代理精确 GET inspection/evidence，拒绝其他 API、写操作、跨源/跨站请求、非预期 Host、路径越界、
上游跳转和非 JSON 响应；不转发 Cookie、任意代理头或模型 Key。响应统一 no-store，单次
上游读取最多 30 秒、8 MiB，不自动重试。端口占用会报错退出，不终止已有服务。

| 故障 | 下一步 |
| --- | --- |
| 缺少构建产物 | 在 web 下执行 `npm run build` |
| Viewer port already in use | 核对现有进程，或显式选另一个本机端口 |
| 503 inspection_upstream_unavailable | 核对后端和获准 SSH 隧道，不自动重启它们 |
| 504 inspection_upstream_timeout | 排查后端读取/文件验证耗时，不以刷新页面扩大执行预算 |
| 401/403 | 重新登录或向管理员申请该 Run 的有效凭据，不自动续期 |
| 422 | 检查证据/身份/状态漂移，不修改 Hash 绕过验证 |

按 Ctrl+C 只停止本机代理，后端、SSH 隧道、证据和凭据不受影响。

### 登录与数据边界

终态入口复用 `AgentGenerationEvidenceReadService`，不是绕过权限调用完整控制面。
每次读取先核对 Run ACL，再核对实时 A 状态、Publication 与全部 D 引用。缺少终态报告、
状态/内容漂移或权限失效时拒绝读取，不回退到 inspection 或演示数据。`approved/promoted`
只描述已有审核/制品记录，不等于性能验收、Formal Signoff 或发布授权。

升级需同时更新专用后端、前端构建和本机代理进程。部署方须确认既有只读账户可读取
`agent_generation_evidence_read_models` 及其依赖的 A 记录，且报告所有 URI 位于已批准只读根内；
程序不自动增加数据库 GRANT、扩展证据目录或续期凭据。旧后端返回 404 不代表候选失败。

加载中、鉴权拒绝或读取失败时，统计显示“— / 用量未知”，不能把未取得证据解释为零次
尝试、零提案或零消耗。重新读取期间不展示或导出旧快照；只有本次读取成功才恢复证据
展示。服务返回的真实零值仍显示为 0，不会与未知状态混淆。

独立服务采用 HTTP Basic。前端访问 `/?agentInspection=<run-id>` 时先显示显式登录表单，
输入 `operator` 和私有文件中的密码；未提交表单不读取 inspection，避免内置浏览器的原生
认证提示不可见而一直等待。密码只作为当前页面内存中的 Authorization 使用，提交后清空
密码输入框；不写入源码、URL、localStorage、sessionStorage、截图或聊天。关闭证据页即
退出当前页面登录，刷新需重新输入；这不是服务端撤权，也不能追回已下载的证据。

Basic 编码**不是加密**，只能在上述 HTTPS 或本机/SSH 通道中使用。前端静态文件与 API 必须
通过同一个受控源提供；inspection 请求不使用 `VITE_HCUOPT_API_BASE`，只发往当前源的确切
只读路径，禁止跟随重定向、不附带 cookies、不缓存，并设置 30 秒超时。服务端仍逐次检查
密码、Run ACL 和有效期，前端的登录状态不能放宽服务端权限。前端无需获得模型 Key。

代理仅转发这一条只读 API，不把整个 `/v1/` 指向完整控制面。登录失败、到期和证据不可用
均不启用 demo 回退。部署时需要实际验证登录与成功后的证据内容，不能用 TestClient 的
HTTPS 字符串替代真实 TLS 或浏览器联验。

## 验收清单

### 2026-09-08 开发验收记录

已在获准 CPU 环境部署专用只读 API，经 SSH 隧道和同源静态页面接入既有 PostgreSQL。
使用独立 schema 与 SELECT 角色，真实本地 Runner 执行模拟模型夹具，生成 1 个待审核提案；
不是实际优化案例，也不产生 HCU 性能结论。

- HTTP 九项检查通过：存活、缺少/错误/有效凭据、跨 Run、非本机 Host、写请求、控制面路径、
  OpenAPI 路径；所有响应均 no-store。专用 DB 角色的 UPDATE 被拒绝。
- 初次一小时凭据到期后实际返回 403；获得新的限时授权并轮换后，旧密码 403、新密码 200。
- 实际浏览器已观察成功登录后的 Attempt、保留 Proposal、Patch、Receipt、清理与 HOLD。
- 页面发现 pending review 被误显示为 not_applicable；修复只按 D 的 review_status 显示
  待审核/不适用/证据未确认，不改变后台状态。修复后单测和构建通过，未冒称已再次完成浏览器登录。
- 当前前端 20 项 unit + 4 项 Sites tests、lint/build 通过；CI 结果须绑定对应提交单独记录。

这里只确认受控只读展示链路，不代表生产部署、安全认证、跨模块接受、真实模型质量或 Formal
验收。原始凭据、模型回复和现场部署目录不进 Git。后续仍需 CODEOWNER/接口上下游审核。

### 后续部署检查

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

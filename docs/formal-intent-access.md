# Formal 页面访问凭据的部署交接

关联 #167 / #168。此工具是部署管理进程接口，不是匿名 HTTP 签发端点。

## 输入与输出

`hcuopt.deployment.formal_intent_access.materialize_formal_intent_access()` 消费：

- 已有 `FormalStartIntentRequest`，包含部署身份服务签署的 actor assertion；
- 管理面独立确认的 `expected_actor_id`；
- 部署允许列表中的 `DeploymentFormalStartSignatureVerifier`；
- 带时区的当前时间、管理员选定的私有输出父目录。

工具先重新解析 Contract，检查 actor/action/请求摘要/有效期/signer 身份和签名，成功后才创建文件。
它不生成 actor 签名，也不签发项目窗口、B 或 D Authority。不可将测试 verifier 用于生产。

输出一个新建的 owner-only 目录（复用现有 private_instance 的 Windows ACL / POSIX 0700 保护）：

- `access-key.txt`：32 随机字节生成的独立访问凭据，仅交给获准操作者；
- `capabilities.json`：凭据 SHA256、原预签声明和冻结请求，无明文凭据；
- 函数返回三个路径，不输出口令，不覆盖已有部署目录。

配置在最后发布。文件写入异常不返回成功；可能留下新建私有目录中的部分文件，管理员只清理
该次返回前创建的独立目录，不能清空父目录。工具不自行重新签发授权或扩大窗口。

## 装配顺序

1. 部署管理进程获得已有已签名请求，并用可信 verifier 生成上述访问材料。
2. 通过 `FormalStartManagement.from_file(coordinator, deployment_root=目录, path=配置)` 装配。
3. 浏览器部署使用 `create_formal_intent_console(management=..., repository=..., static_root=..., browser_origin=...)`。
   它只注册 Formal 准备/提交与静态页面，不挂载通用 Task/Job/Lease/签核接口，不迁移数据库。
   `create_app(formal_start_management=...)` 仅供内部集成；不得为了开放页面而直接暴露整个通用 API。
4. 页面 `/?formalStart=1` 使用独立凭据读取冻结引用，确认后提交，所有 B/D 校验仍由 coordinator 执行。
5. 重启时复用原配置与 assertion；不要重新生成幂等键。移除配置绑定并重启后撤销访问。

运维须保护父目录，使用 TLS 或受控回环通道，并避免代理日志记录 Authorization。
凭据不写聊天、Git、URL 或浏览器存储。不要把模型 API Key 用在此处。
专用控制台拒绝 Host/Origin 不匹配和跨站请求；所有响应禁止缓存、嵌框与引用来源泄露。
绑定监听地址和 TLS 仍由部署管理进程负责，不能把允许回环 Origin 理解为服务自动只监听回环。

## 当前验收边界

### 可选的派发状态读取

派发读模型须显式装配：`dataclasses.replace(management, dispatch_reader=dispatcher)`，
其中 `dispatcher` 为绑定同一 coordinator/repository 的 `PostgresFormalDispatcher`。
该注入仅增加 `GET /v1/operator/formal-round-dispatch`，不开放 Web 派发写接口。
沿用独立操作凭据，复验 actor 声明及有效期；Intent ID 由凭据绑定请求推导，不接收任意 Run/Intent ID。
响应核对 Intent/Round/Plan/服务身份。未配置 reader 时路由不存在；读取失败不是“轮次不存在”。

页面重新输入凭据后只读查询，查询结束清空凭据。v1 仅支持 `not_created / queued / cancelled`，
`execution_consumer_enabled=false` 固定；queued 明确显示“已创建，待执行模块接入”，不是 HCU 运行。

单元测试覆盖配置生成→文件加载→HTTP 读取/提交→同请求重放，以及过期、错误 actor、拒绝签名、
验证器故障和无时区时间的失败前零文件写入。PostgreSQL 集成测试改为消费工具生成的材料，
验证真实持久化与恢复；其中 Authority 和签名器仍明确为测试夹具。

生产身份服务、真实 signer/verifier 配置及生产窗口证据尚未部署；不能据此宣布生产签名链已完成。
所有执行禁止字段保持 false。本工具不创建 Round/Job/Lease，不访问 HCU。

## 统一部署装配

部署代码使用 `hcuopt.deployment.formal_runtime.FormalDeploymentRuntime` 收拢接线：

```python
runtime = FormalDeploymentRuntime(repository, management, enabled=False)
app = runtime.console(static_root=frontend_dist, browser_origin=origin)
```

`management` 必须已经装配可信 coordinator 与已签访问配置，不能预先混入 reader。
同一 runtime 提供 `dispatcher`、`claims` 和控制台，统一使用同一仓库及 coordinator。
构造对象没有迁移、派发、资源领取或后台执行副作用；`enabled=True` 也不代替签名、
窗口或预算授权，且不新增 Web 执行写接口。

正确性执行后，可在装配控制台时传入 `correctness_journal=原日志对象`，
将只读恢复查询接上该次执行。日志必须沿同一个 `runtime.claims` 构造，否则拒绝；
不允许拿另一条执行链的日志给当前页面展示。重启须从原始持久化身份重新装配，
不新建领取或自动重试未知执行。

这只是统一装配入口，不是完整自动调度循环。生产签名、B/D 部署配置、真实资源执行、
阶段推进和最终签核仍沿既有组件集成，不能把创建 runtime 当作这些工作已验收。

2026-09-21 组合验证：接口/控制台/恢复读 API 本地 18 passed；Linux/Python 3.10/
PostgreSQL 32 passed（41.70 秒）。派发及真实 Git/Overlay 构建测试的共同入口现从
HTTP 创建 Intent，再经统一 runtime 的 dispatcher 执行。签名和业务源仍为明确测试夹具，
正确性日志测试中的结果也不代表 HCU 执行；本轮没有真实模型、性能或浏览器视觉验收。

## 标准签名适配与信任边界

`hcuopt.deployment.formal_signing` 提供 `FormalEd25519Signer` 与仅持公钥的
`FormalEd25519Verifier`，安装可选依赖 `.[formal]`。使用 cryptography 的标准 Ed25519，
不是自实现密码算法。部署可信配置提供密钥对象、signer_id、key_id 和固定角色
`actor / execution / evaluation`；请求不能选择密钥或角色。

- B/D 原有 issuer 使用 `sign_authority(content_hash=...)`；身份服务使用
  `sign_assertion(content_hash=...)`。错误角色调用拒绝。
- Coordinator 只注入 `signer.verifier()` 返回的公钥对象，不向 API 进程传入签名器。
- 协议名为 `hcuopt-formal-ed25519-v1`。签名消息是既有 `canonical_json_bytes` 编码的
  `{domain, role, signer, content_hash}` 对象；`signer` 为完整 FormalStartSignerRef。
  摘要限定小写 SHA256，签名为 64 字节 Ed25519 签名的规范标准 Base64。
- `signer_hash` 是原始 32 字节公钥的 SHA256。重用同一密钥但修改角色或名字不会
  变成独立密钥身份；Coordinator 原有角色分离检查继续生效。
- 适配器不生成或落盘生产密钥，不给操作者授予权限，不批准机器窗口，不签发 owner
  Authorization 或替代 Holdout/独立裁决证明。密钥保管、撤销、轮换和角色职责隔离
  仍需部署方配置；不同密钥不自动证明不同管理主体。

统一 runtime 的单元和 PostgreSQL 派发/构建联验已改用临时生成的独立 actor/B/D 密钥
进行真实密码学签名验证。输入 Authority、owner 验签、源工作负载及环境证据仍是测试夹具，
不能称为生产授权链部署完成。篡改 actor/B/D 签名均不能得到 ready 状态，actor 验签失败
不创建 Intent。该可选适配器仍随 Draft PR 评审，不更改 ADR 的 Proposed 状态。

本轮验证：Windows/Python 3.12/cryptography 50.0.0 本地组合 31 passed；
Linux/Python 3.10/cryptography 50.0.1 与 PostgreSQL 组合 50 passed（41.68 秒）；
Ruff 通过。远程加密依赖安装在本次 CPU 测试临时目录，未升级容器系统库。
没有生成生产密钥、升级 BW20 源库、启动 HCU 或签署生产授权。

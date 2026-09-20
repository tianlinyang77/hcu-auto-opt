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
3. 显式向 `create_app(formal_start_management=...)` 注入；普通默认部署继续不注册写入口。
4. 页面 `/?formalStart=1` 使用独立凭据读取冻结引用，确认后提交，所有 B/D 校验仍由 coordinator 执行。
5. 重启时复用原配置与 assertion；不要重新生成幂等键。移除配置绑定并重启后撤销访问。

运维须保护父目录，使用 TLS 或受控回环通道，并避免代理日志记录 Authorization。
凭据不写聊天、Git、URL 或浏览器存储。不要把模型 API Key 用在此处。

## 当前验收边界

单元测试覆盖配置生成→文件加载→HTTP 读取/提交→同请求重放，以及过期、错误 actor、拒绝签名、
验证器故障和无时区时间的失败前零文件写入。PostgreSQL 集成测试改为消费工具生成的材料，
验证真实持久化与恢复；其中 Authority 和签名器仍明确为测试夹具。

生产身份服务、真实 signer/verifier 配置及生产窗口证据尚未部署；不能据此宣布生产签名链已完成。
所有执行禁止字段保持 false。本工具不创建 Round/Job/Lease，不访问 HCU。

# Endpoint Campaign 短期人工签核

已完成签核后，日常查看结果可使用 [托管结果入口](endpoint-console.md)，通过
`hcuopt endpoint-console serve/status/stop` 管理正式前端构建和自有隧道。

Endpoint Campaign 的签核凭据是单 Campaign、单签署人、短期有效的独立能力。它不应复用页面读取凭据、控制凭据或模型 API Key，也不会改变 `automatic_release_allowed=false`。

## 部署输入

控制面启动前必须一次性同时设置以下变量；只设置其中一部分会使服务启动失败：

```text
HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_CAMPAIGN_ID
HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ACTOR
HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_TOKEN_SHA256
HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_EXPIRES_AT
HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ORIGIN
```

服务只接收签核 Token 的 SHA-256，不接收明文 Token。部署脚本应使用密码学安全随机数生成明文，写入仅当前操作者可读的临时文件，再把 Hash 交给控制面。`EXPIRES_AT` 使用带时区的 ISO 8601 时间；HTTP 页面只允许显式 loopback Origin，远程访问应通过 SSH 隧道。

## 操作边界

1. 页面读取当前 Campaign 和正式 D Result Hash。
2. 操作者输入配置中的签署人和独立签核凭据。
3. 页面冻结决定、理由、Result Hash 和幂等键后再提交。
4. API 同时校验 Campaign、有效期、Origin、Bearer Token 和 actor。
5. PostgreSQL 写入不可变签核；接受 `inconclusive` 只表示接受这次证据与裁决，不表示候选更快或允许发布。

签核完成、凭据过期或服务停止后，应删除明文凭据文件并清除五个环境变量。若没有配置上述变量，GET 页面仍可查看，POST 签核安全返回 503。

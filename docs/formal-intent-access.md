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

### owner 与公钥配置整体接线

后续增量提供 `FormalOwnerEd25519Signer/Verifier`，适配现有 owner 窗口授权协议，
只签署已经决策的 authorization_hash，不生成、延长或批准窗口。签名域仍为上述协议，
role 固定为 owner，消息内 signer 是由 verifier 身份同字段映射得到的 FormalStartSignerRef。
该适配器补充前文尚缺的 owner 密码学实现，但不代表生产 owner 授权已经签发。

`FormalPublicTrust.from_file` 加载管理员选定的公钥配置，最大 16 KiB，格式为：
schema_version=`formal-public-trust-v1`，owner/actor/execution/evaluation 四项各含
identity_id、key_id、public_key_base64（原始 32 字节 Ed25519 公钥的标准 Base64）。
四项必须使用不同身份和密钥；私钥字段、未知字段、错误编码、越界路径拒绝。
公钥配置本身是信任根，管理员必须保护根目录及父目录；不能把请求上传的文件当可信配置。
配置启动时加载，不热更新；轮换后需重启，旧签名不会自动匹配新密钥。

部署可直接用 `FormalDeploymentRuntime.from_deployment_files(...)` 同时加载公钥配置
和既有 capabilities 配置，传入已完成 Profile/readiness 准入的 compiler 与 object_store。
工厂重新解析并验证 owner 授权内容及签名，再装配 actor/B/D verifier、管理入口和派发器。
这是替代手工接线的入口，不替代 Profile 准入、当前资源窗口检查或生产部署评审；
默认 disabled，构造时不迁移、不派发、不触碰硬件，不生成私钥或业务授权。

测试签名链现包含四方独立临时密钥及公钥文件重载。测试 Profile/readiness、工作负载
和签署决策仍是明确夹具，不是生产事实；部署重建重用原 capabilities 与 Intent，
owner 签名篡改、内容篡改和不匹配的轮换密钥在建立 coordinator 前拒绝。

本轮最终验证：本地组合 42 passed（4.71 秒），Linux/Python 3.10/PostgreSQL
组合 61 passed（41.42 秒），Ruff 通过。重建验签器和 runtime 复用同一 Intent，
不代表重启了真实数据库或生产服务。没有生成生产密钥、签署生产窗口或访问 HCU。

### 预览与授权磁盘重载（2026-09-22）

`FileFormalStartPreviewStore` 补齐原先只有 Protocol 的部署预览存储。
管理员将 compiler 生成的预览传入 `publish_preview`，按 UUID 原子发布固定字节；
重复发布同内容幂等，同 ID 不同内容拒绝。读取重新解析 Contract 并核对完整计划 Hash，
缺失、路径重定向、内容/身份不匹配拒绝。复用现有不可覆盖文件发布机制，单预览限制
512 KiB，不提供 HTTP 上传入口或新的执行许可。

与 `DeploymentFormalStartAuthorityStore(preview_store=...)` 配合，服务重建后可以从
磁盘加载同一预览及 B/D 授权，而不依赖测试内存 Store。过期记录仍可用于审计读取，
是否可执行继续由 coordinator 的当前时间及权威重验决定。存储根和父目录须由管理员
保护；它不是抵御管理员恶意替换文件的签名存储系统。

PostgreSQL 整合测试已改为：四方签名 → 预览/B/D 授权落盘 → 公钥/能力配置加载 →
HTTP 创建 Intent → 重新实例化全部文件 Store、runtime 和 app → HTTP 重放同一 Intent →
后续派发及构建测试。没有重启真实数据库或 OS 进程，测试 compiler、源负载和授权决策
仍是夹具，不能将这条重载验证称为生产部署或 HCU 验收。

验证：本地存储测试 8 passed / 1 skipped（Windows 符号链接测试跳过）；
Linux/Python 3.10/PostgreSQL 整合 70 passed（44.74 秒），包括存储测试和上述
文件重载后的 HTTP/派发/构建链，Ruff 通过。源库和生产服务未修改。

### 从部署配置重建编译器（2026-09-22）

`FormalDeploymentRuntime.from_configuration(...)` 不再要求调用方预先构造内存
compiler。`compiler_path` 指向管理员保护的 JSON，最大 2 MiB，使用
`FormalCompilerConfiguration` 契约，包含：

- `schema_version`: `formal-compiler-configuration-v1`。
- `source_commit`、稳定的 `server_instance_id`。
- 三项 `profiles`，既有 owner `authorization`。
- `readiness_manifest`、`readiness_report`、`candidate_family`。

加载器复用 `build_formal_operator_profile_catalog` 的签名、窗口及 readiness 准入，
不直接构造私有 Catalog。源码 Commit 必须等于启动方独立提供的
`expected_source_commit`；配置自报的 Commit 不是运行代码身份的证明。
候选 Family Hash 必须匹配 owner 授权；清单按固定 JSON 保存，每次读取重新解析。
候选包实际字节仍由原有 verifier 在编译时重新核验。

这个入口与已有 `trust_path`、`capabilities_path` 一起完成 runtime 装配，默认 disabled，
不迁移数据库、不产生授权、不启动执行。调用方仍须提供真实 repository、object_store
和 candidate_family_verifier；它尚不是单凭配置文件启动完整生产服务的 CLI。

新增测试覆盖重复加载身份稳定、默认禁止派发、错误发布版本、过期授权、签名篡改、
readiness/Family 篡改，以及未知/私钥字段、超限文件、越界路径和缺失文件拒绝。
配置加载测试使用明确的合成 readiness 及临时签名密钥，只验证构造与准入，
不声称新的配置 compiler 已完成真实候选编译或 HCU 执行。
既有 PostgreSQL HTTP/派发/构建链仍使用原测试 compiler。

远程测试打包须包含 `config/m2/nmz36-formal-readiness-v1.yaml`：新增准入夹具依赖此文件，
仅打包 `config/targets` 会导致测试准备阶段失败，不能将此错误归因于运行时准入。

验证结果：本地编译器/信任/运行时/签名/Profile 准入组合 45 passed；
Linux/Python 3.10/PostgreSQL 组合 81 passed（44.84 秒）；Ruff 通过。
远程包包含本轮工作区增量，不是之前 HEAD 的纯净快照。
Windows 新进程导入须显式设置本仓库 `src` 为 PYTHONPATH，避免命中其他 checkout
的已安装包；在该设置下完成独立进程导入检查。

### 配置驱动的本机服务入口（2026-09-22）

`python -m hcuopt.deployment.formal_service` 自动装配真实
`CandidateSourcePackageStore` / `BusinessCandidateFamilyVerifier`、磁盘预览/B/D
授权存储和 PostgreSQL repository。不再要求操作者手写 Python 对象接线。
启动前读取候选包实际字节并校验 Family，随后进行只读数据库结构检查。
缺包、包内容篡改、Store 身份不符、过期授权或 schema 不匹配均拒绝启动。
编译阶段仍会重新校验包；启动前通过不是对未来磁盘内容的永久担保。

管理员准备 `service.json`（64 KiB 上限，未知字段拒绝）：

```json
{
  "schema_version": "formal-service-configuration-v1",
  "compiler_path": "compiler.json",
  "trust_path": "trust.json",
  "capabilities_path": "capabilities.json",
  "package_root": "packages",
  "preview_root": "previews",
  "authority_root": "authorities",
  "static_root": "web",
  "package_profile": "<approved-adapter-profile>",
  "store_id": "<approved-store-id>",
  "store_hash": "<approved-sha256-identity>",
  "allowed_overlay_roots": ["<approved-source-root>"],
  "approved_mount_targets": {"<approved-replacement-point>": "<approved-mount-target>"},
  "port": 4205
}
```

这是字段示例，不是可用的生产授权；尖括号内容必须取自已审定部署资料。
目录须预先存在，位于部署根目录内且不是链接；`web` 须包含构建后的 `index.html`
和 `assets/`。配置文件、存储根及父目录由管理员保护，不接受浏览器上传。
数据库连接只通过 `HCUOPT_DATABASE_URL` 环境配置注入，不写入这个 JSON 或命令行。
配置中也不包含模型 API Key 或签名私钥。

```bash
python -m hcuopt.deployment.formal_service \
  --deployment-root /srv/hcuopt/formal \
  --configuration service.json \
  --source-commit <independently-pinned-release-commit>
```

应使用与发布 Commit 对应的安装包/源码运行；CLI 参数匹配本身不证明安装代码未变。
入口固定监听 `127.0.0.1`，不提供外网监听、自动迁移、自动签署或启用派发的开关。
预检错误输出脱敏错误码，不输出 DSN 或原始配置异常。Ctrl+C 正常停止服务。

此入口提供 Formal Intent 管理 HTTP 服务，不启动 Agent、Build、Correctness 或
Performance worker，也不替代最终裁决/签核服务。已有签名 capabilities 继续控制
Intent 操作权限；空 capabilities 仅允许展示静态页，不能提交合法 Intent。
因此服务能启动不等于完整真实优化任务已跑通。

新增 PostgreSQL 联验使用合成授权、真实临时候选包和隔离 schema，实际启动
Uvicorn 临时本机端口，验证 HTTP 页面、跨站拦截、没有自动创建 Job/dispatch/claim，
最后停止服务。不将此测试称为生产页面部署、真实模型调用或 HCU 验收。

本轮验证：本地组合 52 passed；Linux/Python 3.10/PostgreSQL 组合 89 passed
（46.64 秒）；Ruff 通过。远程测试包包含本轮工作区增量。

### Worker 统一接线与数据库构建输入（2026-09-22）

统一 runtime 增加三项显式部署接口：

- `build_consumer(...)`：将既有 Builder 绑定到同一 runtime 的 claims、journal 和发布存储。
- `local_build_consumer(...)`：复用已准入 compiler 的候选包根目录、白名单和 Store 身份，
  自动装配 GitSourceManager、Overlay Builder、LocalArtifactStore 和 Build Cache。
  目标 Profile 与 Store 身份不一致拒绝；禁用 runtime 在创建目录前拒绝。
- `correctness_consumer(...)`：接入既有 D Worker adapter，但拒绝来自其他 runtime 的租约链。

构建调用方现在只须传既有 `reservation_id` 和部署输出目录给
`execute_current_once(...)`；候选、Round、冻结 Baseline、Hotspot 及其 Intake Hash
由 `PostgresFormalBuildMaterialReader` 根据 Intent/Job/预算关联读取，不能由 HTTP
请求临时拼装。原有 `execute_once` 也在真正调用 Builder 前复核 Hotspot 与数据库一致。

重复调用时读取原 journal 的 input Hash 和已落盘结果，只重做预算结算及发布，
不从已更新为 built 的候选状态伪造原始构建输入，也不再次运行 Builder。
该重放模式忽略新的输出目录：返回的是原制品，不复制或重建到新目录。
构建中的 KeyboardInterrupt 等中断标记为需要恢复；没有确定结果就拒绝自动重试。

真实 Git/Overlay/PostgreSQL 测试已改用 `runtime.local_build_consumer` 和数据库输入入口，
覆盖两份候选制品、只读内容、源码 Hash、Worktree 清理、一次结算、重复调用。
附加测试覆盖错误预算/领取者、热点篡改拒绝、构建中断不重跑以及跨 runtime 接线拒绝。
测试源代码、授权和设备证据仍是夹具，不构成真实 HCU 正确性或性能结论。

边界：这些工厂不注册 Worker、不领取任务、不申请或扩大预算、不启动后台循环。
服务 CLI 仍为管理入口，未隐式启用执行。生产驱动器还须在已批准资源窗口内串联
dispatch/claim/预算与各阶段；Correctness 物理 adapter、Performance、裁决和最终签核
不能因 CPU 构建联验成功而视为完成。

本轮最终回归：本地相关组合 36 passed，Linux/Python 3.10/PostgreSQL 组合
94 passed（52.87 秒），Ruff 通过；后者包含真实 Git/Overlay 构建、数据库材料加载、
结果重放、HTTP 启动及故障拒绝。远程源码包含本轮增量，不是之前 HEAD 的纯净快照。
旧故障夹具曾传空 Hotspot，在新增数据库复核下被正确阻止；现改用隔离库中的实际记录，
保留故障注入目的，没有放宽生产校验。

### 有界 CPU 构建驱动与命令入口（2026-09-22）

`FormalBuildDriver` 串联原有操作：显式 `start` 创建 dispatch 并一次性领取；
`build_family` 只遍历签名 Intent 内的候选，通过注册 Worker 检查、独立 Build Job、
预算预留、数据库输入加载、真实 Overlay 构建和结果发布。第一项失败就停止后续候选。
它不扩大 Family、资源窗口或总预算，也不自动冻结 Family 或进入正确性/性能阶段。

Worker 须事先按部署流程注册；驱动器不改写他人的 Worker 注册。
领取凭据须私密保管；`start` 的响应丢失不能仅凭 Worker 名字取回凭据或自动再领取。
同一 Job 的 reservation/ledger 使用确定性 ID 和原 Job 时间，重放不创建第二份预算。
改变已有预留金额会被原预算接口拒绝，不静默覆盖。
`wall_seconds_per_candidate` 是预算预留而不是进程终止计时器；当前 claim 最长 300 秒，
无续租机制，超过窗口不得自动再领或冒充完成。此驱动适用于有界 CPU Overlay 构建，
不是长时间重编译或 HCU 调度循环。

显式命令入口消费**已有领取凭据**，不在失败后重新 start：

```bash
python -m hcuopt.deployment.formal_build_command \
  --deployment-root /srv/hcuopt/formal \
  --configuration service.json \
  --invocation private-build-invocation.json \
  --source-commit <independently-pinned-release-commit>
```

沿用管理服务的配置加载、源码 Commit、签名与只读 schema 检查；此命令显式启用
构建 runtime，但不启动 HTTP 服务或 HCU Worker。`HCUOPT_DATABASE_URL` 由环境注入。
invocation JSON 契约为 `formal-build-invocation-v1`，字段是 `intent_id`、`worker_id`、
`claim_token`、`wall_seconds_per_candidate`、`artifact_root`、`cache_root`、`output_root`。
三个目录须已存在且处于管理员控制的部署根内，不允许越界或链接；文件上限 16 KiB。
领取 token 不放在命令行、报告或示例中。POSIX 文件要求仅 owner 可访问；Windows
须由部署方设置等效 ACL。管理员也须保护父目录，不能将网页上传内容作为此文件。

成功输出仅含 Intent、候选及 Artifact Hash，固定 `hcu_accessed=false` 和
`automatic_release_allowed=false`。失败返回非零和脱敏错误码，禁止自动重试；
已经完成的前序候选及预算保留在数据库，不能把一次失败理解成整批没有发生任何写入。

新增测试覆盖完整两候选真实 Git/Overlay 构建、重复调用、原预算金额不可变、停止、
错误领取凭据、未注册 Worker、预算不足和无效数字；命令调用真实 runtime 的重放
及脱敏输出也纳入测试。该命令联验对配置加载函数使用测试替换；配置加载准入另有
测试，尚不能称为生产配置到 HCU 的一体化验收。

最终验证：本地 71 passed / 1 skipped（Windows 不执行 POSIX 权限位检查）；
Linux/Python 3.10/PostgreSQL 115 passed（63.93 秒），Ruff 与独立 CLI 加载检查通过。
包含首个候选完成后停止的测试：保留一个成功 Job/Artifact，不创建第二个构建 Job。
远程测试包包含本轮工作区增量。没有生产库迁移、真实模型调用、HCU 执行或自动发布。

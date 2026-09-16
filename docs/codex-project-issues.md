# Codex 项目本地已知问题

## SGLang 多进程导入不能把动态 PID 差异当成 Overlay 身份漂移

- Scope: project-local；2026-09-16。
- Symptom: BW20 Endpoint Baseline 第 0 次采集的 SGLang scheduler 在初始化时退出，服务始终
  未 ready；每个子进程都报 `endpoint activation evidence already differs`。
- Evidence: Run `0dbedc7e-15b0-58c2-9f1c-f3e6bdcd9d8f` 使用 fence 53，在任何请求和
  性能样本产生前失败；失败证据、server.log 和 cleanup evidence 均已保留。清理后 HCU 7
  为 auto、busy 0、约 2.1 MiB、无 KFD 进程，资源账本为 available。
- Cause: SGLang 使用多个 Python 子进程；它们导入的是同一路径、同一 Hash、同一
  inode/mtime 的目标模块，但每个进程的 PID、父 PID 和采集时间不同。旧 `_publish()` 对
  整份 JSON 做字节相等比较，把合法的第二个导入者误判为篡改。
- Proven workaround: 首个真实导入者以原子 hard-link 发布不可变 `activation.json`；后续
  导入者只有在 schema、模块名、绝对路径、SHA256、device/inode/size/mtime 全部一致时才
  复用首份证据。PID 和单调时钟只保留首个导入者的值，不参与跨进程稳定身份比较；其他
  稳定字段、文件类型、link count 或 JSON inventory 有任何差异仍 fail closed。
- Validation: 两个独立 Python 进程对同一模块和同一证据路径均成功，且第二个进程不能
  改写首份字节；不同模块身份继续被拒绝。Endpoint/Runner 聚焦回归为 `33 passed`。
- Applies to: 通过 `sitecustomize.py` 在 SGLang 多进程启动期间证明 Python Overlay 实际加载。
- Do not repeat: 不要要求多进程激活 JSON 字节完全相同，也不要为了兼容多进程而删除
  module Hash、路径或 inode/mtime 的稳定身份校验。
- Last updated: 2026-09-16

## Registry Digest 运行时必须带完整仓库引用

- Scope: project-local；2026-09-16。
- Symptom: `docker images --digests` 能找到精确 Registry Digest，但
  `docker run sha256:<registry-digest>` 报 `No such image`，CPU 验收没有启动。
- Evidence: 本地清单同时给出仓库名、Registry Digest 和不同的本地 Image ID；失败后没有
  容器、数据库 schema 或 HCU 状态变化。
- Cause: Registry manifest Digest 不是 Docker 本地 Image ID，不能脱离仓库名作为镜像引用。
- Proven workaround: 使用 `<repository>@sha256:<registry-digest>` 启动，并保留 Digest
  校验；只有确需按本地内容对象寻址时才使用 `docker image inspect` 返回的 Image ID。
- Validation: 改用完整引用后，同一只读源码、无 HCU CPU 容器完成 PostgreSQL 端点控制面
  联验，`2 passed`，容器和随机隔离 schema 均已清理。
- Applies to: BW20 上按已冻结 Registry Digest 启动的 SGLang 临时验收容器。
- Do not repeat: 不要把 Registry Digest 裁成裸 `sha256:` 传给 `docker run`，也不要改用
  浮动 Tag 绕过失败。
- Last updated: 2026-09-16

## DeepSeek Messages 可成功返回但 unified diff 元数据不稳定

- Scope: project-local；2026-09-15。
- Symptom: 对冻结的 BW20 `PagedTokenToKVPoolAllocator.free` 源文件执行四个相互独立、
  有上限的真实 Messages 探针，HTTP/Runner 均成功，但 C 全部拒绝 Proposal；首个回复的
  hunk 行数不一致，后三个回复的 hunk 起始位置或上下文与 Baseline 不一致。
- Evidence: 每次均保留独立 input、Runner Receipt、原始回复和失败报告；累计没有创建
  Candidate、没有执行补丁、没有访问 HCU，也没有性能结论。具体证据保存在部署方仓库外
  `bw20-agent-live-results/source-probe/deepseek-20260915-v1..v4`，不提交模型原始内容。
- Cause: 已确认是模型生成的 unified diff 定位/计数不符合严格解析器；不是网络、模型名、
  token 用量、Runner 清理或密钥注入失败。语义建议是否值得优化尚未评测。
- Proven workaround: 保留 failed Batch/Receipt 语义，并支持结构化单片段编辑：模型返回唯一
  `old_text/new_text`，C 在冻结 Baseline 上唯一匹配后确定性生成 diff，再交给原严格 Patch
  解析器回放。不得直接修补模型 diff 后冒充原始 Proposal，也不得放宽路径、上下文或 hunk
  计数校验。
- Validation: v1-v4 Runner cleanup 均为 verified，原始回复全部 fail closed；v5 使用新格式
  形成 `applicable_unreviewed_patch`，Patch Hash 为
  `sha256:62d3719f3e8d45ff7b06036062ef49688397fc8aea6c7a00a2cfadf3eea092d9`。
  v5 仍因调用未定义 helper 而必须在人工代码审核处拒绝；没有 Candidate、HCU 访问或性能结论。
- Applies to: 当前 DeepSeek Anthropic-compatible `deepseek-flash` 单文件候选生成器。
- Do not repeat: 不要对同一输入无限重试，不要手改原始模型回复或禁用严格 Patch 门禁。
- Last updated: 2026-09-15

## Messages 与 D 衔接必须保留原生安全读取及失败 Batch 语义

- Scope: project-local；2026-09-07。
- D 的 HashedEvidenceReader 在 Windows 明确拒绝读取；不能通过替换正式 Reader 为
  PortableReader 来宣称整链验收。Windows 验证数据库调度，原生 D/API 在 Linux CPU 验证。
- API 测试需要 `with TestClient(...)` 启动 lifespan，否则 app.state.repository 不存在；
  只读验收设置 HCUOPT_AUTO_MIGRATE=false，避免混入启动迁移写操作。
- 无 .git 的只读归档必须设置 HCUOPT_SOURCE_COMMIT；它只标基线，实际修改用源码包 Hash 固定。
- A 接受“Runner 成功 / Batch 失败”的有据结算。D 仅在存在独立重读且 Hash 绑定的 failed Batch
  时接受该组合，核对错误码，展示为失败并保留用量；Generator barrier 以 A 的 Attempt 成功为准，
  不能用进程成功替代候选生成成功。
- 已新增失败 Batch、审核前只读检查、状态漂移和 API 篡改拒绝回归。

## Messages CPU 验收必须自包含依赖并回收孤儿进程（2026-09-07）

- Scope: project-local。
- nmz36 主机是 Python 3.6，不得用它验收要求 Python 3.10 的新生成器，也不要升级共享主机。
- 本次在批准的无 HCU、无网络、源码只读容器中验证。旧测试依赖目录不完整，先报
  `ModuleNotFoundError: typing_extensions`。从清华源准备完整 CPython 3.10 Linux wheels，
  容器内 `pip --no-index --no-deps --target /tmp/test-deps /wheels/*.whl` 安装，不改主机。
- `/tmp` 的 Docker tmpfs 若带默认 noexec，Pydantic 原生扩展报 `failed to map segment`。
  本次仅对容器临时目录指定 `rw,nosuid,exec`，根文件系统和源码仍只读、网络仍关闭、不映射设备。
- 未加 `--init` 时两个原有 Runner 子进程清理用例返回 `cleanup_failed`；相同代码加 `--init`
  后全部通过。必须让 PID 1 回收孤儿进程，不能放宽清理断言或忽略未回收的进程域。
- 真实模型探针曾在报告序列化 datetime 时中断。输入和 Receipt 已保存，因此使用
  `messages_hotspot_probe.py --recover --output <原目录>` 补报告，不重复付费调用模型。
- 后续同事先重读 Receipt，再处理报告；不要因为报告缺失直接重跑生成器，不要复用可变旧依赖目录。

## React lint 禁止在 Effect 中同步调用含 setState 的加载函数

- Scope: project-local
- Symptom: `npm run lint` 报 `react-hooks/set-state-in-effect`，指出组件的 `useEffect`
  同步调用了会立即 `setLoading(true)` 的 `load()` 函数。
- Evidence: Agent/Apex 证据工作台在 Windows 本地执行 ESLint 时，报错定位到
  `web/src/AgentProposalWorkspace.jsx` 的初始加载 Effect；后端 Ruff 与 26 个聚焦测试已通过，
  因此前端 lint 是唯一阻塞项。
- Cause: React hooks lint 将 Effect 内的同步状态更新视为潜在级联渲染；把读取逻辑封装为
  普通函数不会绕过规则。
- Proven workaround: 在 Effect 中定义异步读取协程，等待数据 Promise 后再更新状态，并用
  `cancelled` 防止卸载后的写入；手动重试从事件处理器设置 loading 并递增独立 reload nonce。
- Validation: 修改后重新运行 Web lint、unit、build 与 sites 测试。
- Applies to: 本仓库需要在组件挂载时读取 API 或 fixture 的 React 工作台。
- Do not repeat: 不要在 Effect 里直接调用会同步 setState 的 `load()`；不要为了通过 lint
  关闭规则或删除加载/错误状态。
- Last updated: 2026-09-02

## Windows PowerShell 转发 nmz36 复杂内联命令会丢失引号

- Scope: mixed
- Symptom: 通过本地 PowerShell 调用 `ssh github@10.17.1.2 "..."` 时，远端
  `python -c` 或带括号的 `grep -E` 表达式在 Bash 解析前已经失去预期引号，报
  `SyntaxError` 或 `syntax error near unexpected token '('`。
- Evidence: 2026-08-25 读取正式 Stage 0 JSON 的两次内联命令均在解析阶段失败；未启动
  容器、未写证据，也未改变 HCU 状态。
- Cause: 本地 PowerShell、OpenSSH 参数转发和远端 Bash 形成多层解析边界，复杂引号、
  反斜杠及括号无法依赖目测保证原样到达远端。
- Proven workaround: 简单检查只用无正则元字符的 `sha256sum`/`head`/`sed`；需要解析
  JSON 或包含复杂逻辑时，把脚本作为项目文件 `scp` 到隔离 Worktree，再用一条简单
  `ssh` 命令执行。
- Validation: 远端显式运行脚本并校验其 SHA256、退出码和产出文件 Hash。
- Applies to: Windows Codex → nmz36 SSH 的 M1 诊断、证据读取和集成脚本。
- Do not repeat: 不要继续叠加 PowerShell → SSH → Bash → Python 四层内联转义。
- Last updated: 2026-08-25

## nmz36 并发 `hy-smi --showpids` 可能异常退出

- Scope: project-local
- Symptom: M1 连续 HCU 进程归属监控中的 `hy-smi --showpids` 以 `Aborted (core dumped)`
  退出，探针因此 fail-closed，中止并清理受管容器。
- Evidence: Run `20260825-m1-allocator-invariant-v1` 在 2026-08-25 17:08:01+08:00
  写入 `could not continuously verify HCU 7 process ownership`；无不变量证据产生，清理后
  HCU 7 为 0 利用率、0 显存且无受管容器残留。
- Cause: 尚未确认；当时两个外部 `Runner.Worker` 正在运行，可能存在并发管理工具调用，
  但没有证据把异常归因于任一外部任务。
- Proven workaround: 把管理工具异常视为证据失败，不复用部分输出；对 `hy-smi` 非零退出
  最多执行三次有限重试，瞬态失败写入 Telemetry warning，连续失败把完整重试历史写入
  failure evidence 后继续 fail-closed。若 Job 已因这一精确基础设施故障耗尽预算，只能在
  Task/Candidate/Job、错误、前置 Job 和无 verdict/signoff 均匹配时人工授权一次新尝试，
  保留旧 attempts、last_error 和事件历史。
- Validation: Formal Performance 第 4 次使用新 Measurement ID 完整完成 40/40 acquisition、
  400 个原始样本且零 Telemetry warning；D 独立复算得到 `faster`，最终 HCU 7 为 0%/2 MiB、
  Resource available、无受管容器残留。第三次的 37/40 部分证据仍单独保留，未混入成功结果。
- Applies to: nmz36 上需要连续调用 `hy-smi` 的 M1 诊断和正式验收。
- Do not repeat: 不要把监控工具崩溃的部分运行标成可接受，不要清零重试历史，也不要只凭
  清理后的空闲状态补签或复用旧样本。
- Last updated: 2026-08-25

## M1 后续 Job ID 不能从幂等键自行推导

- Scope: project-local
- Symptom: Formal recovery 能找到固定 Task 和 Candidate，却找不到脚本用 `uuid5` 推导的
  Performance Job；真实数据库 Job ID 为另一 UUID。
- Evidence: 控制面数据库中的正式 Performance Job 为
  `7a95738b-b4cd-41bb-88ba-66fa44ab4a1f`，而部署脚本自行推导得到
  `1f86aca7-625f-5107-8ca9-8da22278ac23`；第一次 recovery 被 fail-closed 护栏拦截，数据库
  未发生写入。
- Cause: 当前 `enqueue_job()` 使用数据库入队时生成的 Job UUID；`idempotency_key` 用于唯一
  约束和重放，不是 Job ID 的公开生成公式。
- Proven workaround: 在一次性 Formal recovery 中绑定数据库已产生且人工核对的精确 Job
  ID；通用代码必须通过 Repository 按 Task、Job 类型和幂等键读取，不能重写内部 ID 公式。
- Validation: 部署常量和单测均绑定真实 Job ID；recovery 测试验证只恢复精确的 `3/3`
  telemetry 失败，并且重复执行不增加额度。
- Applies to: M1 Build 后动态排队的 Correctness、Performance 和 Adjudication Job。
- Do not repeat: 不要看到稳定 idempotency key 就假设 Job ID 也是同一字符串的 UUIDv5。
- Last updated: 2026-08-25

## Codex PowerShell 工具会话可能不继承本机 Python PATH

- Scope: mixed
- Symptom: 同一任务前序命令可运行 `python`，后续新工具会话却报告命令不存在；解释器
  文件仍然存在。
- Evidence: `Get-Command python` 与 `where.exe python` 曾无结果，但已确认的 Python 3.12
  安装路径仍为普通文件；后续工具会话恢复了该绝对路径的执行权限。M2a Formal Authority
  切片所在 checkout 还没有独立 `.venv`，且本机会话没有 Docker CLI 或
  `HCUOPT_DATABASE_URL`，因此不能在 Windows 本地伪装 PostgreSQL 实跑。
- Cause: 当前 Codex 工具会话的 PATH 发生变化；具体注入原因未确认。
- Proven workaround: 先用 `Get-Command`/`where.exe` 验证，再直接使用已经确认的解释器绝对
  路径；若某次工具会话仍拒绝执行，保留原命令并在新会话复验，不重装 Python，也不改项目
  依赖。checkout 暂无独立虚拟环境时，先确认 `python` 的真实绝对路径并显式设置
  `PYTHONPATH=src`；PostgreSQL 集成测试仍交给带 PostgreSQL 17 Service 的 Linux CI。
- Validation: 权限恢复后，同一解释器完成 Ruff 全量检查及 M1 聚焦回归，结果为
  `46 passed, 6 skipped`。M2a Formal Authority 定向验证为 `12 passed, 11 skipped`，其中
  11 项只因本机未配置 PostgreSQL 而跳过，不能算作数据库通过。M2b 最终 business
  generation E2E 在 2026-09-01 本地同样只能完成收集与跳过态检查；正式数据库结论必须来自
  PR 的 PostgreSQL 17 CI job。
- Applies to: 本项目的 Windows 本地验证。
- Do not repeat: 不要把 `python` 命令不可见当成测试失败，也不要因此改仓库配置。
- Last updated: 2026-08-29

## Measurement 包顶层重导出 M1 Harness 会形成导入环

- Scope: project-local
- Symptom: 从 `hcuopt.measurement.__init__` 重导出 `M1WorkloadFactory` 后，pytest 在收集
  API 测试时报告 `EvidenceReadError` 来自 partially initialized module。
- Evidence: 导入链为 `evaluation.evidence_reader → measurement.__init__ → m1_harness →
  evaluation.stage0_verifier → evaluation.evidence_reader`，测试尚未执行即失败。
- Cause: `m1_harness` 依赖 Evaluation 的 Stage 0 Reader，而 Evaluation Reader 本身依赖
  `measurement.evidence`；包顶层的 eager import 把原本分离的子模块连成环。
- Proven workaround: `M1WorkloadFactory` 只从
  `hcuopt.measurement.m1_harness` 导入，不在 `hcuopt.measurement` 包顶层重导出。
- Validation: 移除顶层重导出后重新运行 M1 控制面和 D Verifier 测试。
- Applies to: M1 Worker/部署代码新增 Measurement Harness 接线。
- Do not repeat: 不要为了缩短 import 路径，把依赖 Evaluation 的 Harness 类加入
  `measurement/__init__.py` 的 eager exports。
- Last updated: 2026-08-25

## Windows 普通账户无法运行 F1-C 文件语义测试

- Scope: project-local
- Symptom: Windows 全量单测中的 F1-C Source/Builder/Artifact Store 用例报
  `WinError 1314`（无权创建符号链接）或 `WinError 5`（无法删除已设只读的临时制品）。
- Evidence: PR #38 修复验证中曾得到 `288 passed, 15 skipped, 14 failed`。M2 Contract
  第一切片在 2026-08-26 的普通权限 Windows 全量结果为
  `326 passed, 16 skipped, 18 failed`；18 个失败仍全部来自上述文件语义，涉及
  `test_f1c_pipeline.py`、`test_git_source_manager.py`、`test_local_artifact_store.py`、
  `test_m1_candidate_builder.py` 和 `test_noop_builder.py`。OX-1 收尾在 2026-08-28 的同类
  环境结果为 `456 passed, 69 skipped, 19 failed`，新增清单中的
  `test_m1_c_scripted.py` 也失败在 Artifact Store 的 Windows 只读删除语义；干净的
  `main@b338a4c` 已复现两类代表错误。
- Cause: 测试依赖 Linux 文件语义；当前 Windows 会话没有创建符号链接权限，且只读位的
  删除语义与 Linux 不同。
- Proven workaround: F1-C 正式验证使用 Linux CI；Windows 上只运行明确支持 Windows 的
  测试集合。不要用缩短 `--basetemp` 掩盖权限问题，短路径只能解决路径长度。
- Validation: M2 新增 `tests/unit/test_m2_contracts.py` 为 `4 passed`，全仓 Ruff 通过；完整
  失败清单没有 M2 Contract 或其他新增失败。M2a Formal readiness 在 2026-08-29 的普通权限
  Windows 全量结果为 `458 passed, 21 skipped, 18 failed`：16 项仍为符号链接权限，2 项仍为
  只读临时制品删除，新增 readiness 定向测试 `10 passed`。Linux CI 继续作为这些文件语义的
  正式门禁。M2a Formal Authority 切片复验为 `464 passed, 21 skipped, 18 failed`；失败集合
  未增加，仍是同样的 16 项符号链接权限和 2 项只读临时制品删除。M2b Agent Contract
  #117 联审修复在 2026-08-31 的全量结果为 `516 passed, 92 skipped, 19 failed`；19 项仍全部
  属于相同的符号链接或只读临时制品删除限制，新增 Contract 定向回归为 `33 passed, 1 skipped`。
  M2b A/B/C/D Receipt 集成在 2026-09-01 的普通权限 Windows 全量结果为
  `594 passed, 22 skipped, 18 failed`；18 项仍是同一组 16 个 symlink 权限失败和 2 个只读临时
  制品删除失败，Agent/M2 聚焦回归为 `133 passed`。M2a Formal Start Authority Store 在
  2026-09-04 的同类环境结果为 `695 passed, 32 skipped, 18 failed`；失败文件和根因仍完全相同，
  新增及相邻 Formal Start 聚焦回归为 `36 passed, 1 skipped`。
- Applies to: 普通权限 Windows Python 3.10+ 的仓库全量单测。
- Do not repeat: 不要把这些既有 F1-C 权限失败归因于当前 PR；也不要为跑绿而删除
  symlink/只读语义测试。
- Last updated: 2026-09-04

## Windows 可编辑安装可能指向同项目的旧 checkout

- Scope: project-local
- Symptom: 当前工作树已新增 CLI 命令，`python -m hcuopt.cli --help` 却仍显示旧命令集；
  pytest 从当前 `src/` 运行时正常。
- Evidence: Python 实际导入路径指向同一父目录下的旧 `dcu-auto-opt/src/hcuopt`，而当前工作树
  为 `work/hcu-auto-opt-m1-real`；显式设置 `PYTHONPATH=src` 后立即显示 `profile` 和 `round`。
- Cause: 用户环境中的 editable install 仍绑定旧 checkout。项目的 pytest `pythonpath`
  只约束测试收集，不会改变普通 `python -m` 的 site-packages 链接。
- Proven workaround: 开发验证先打印 `hcuopt.__file__`；优先使用项目独立虚拟环境。一次性源码
  验证可显式设置 `PYTHONPATH=src`。如果确认旧 `dcu-auto-opt` editable 项目已废弃，再单独卸载
  它；仅重新安装当前 `hcu-auto-opt` 不会自动删除另一个发行名留下的 `.pth`。
- Validation: `PYTHONPATH=src python -m hcuopt.cli round run --help` 正确显示 OX-1 参数。
- Applies to: 同一 Windows 用户环境中并存多个 hcu-auto-opt checkout 的本地 CLI 验证。
- Do not repeat: 不要把旧 checkout 的帮助输出判成当前分支代码未生效，也不要在未核对
  `__file__` 前修改 parser。
- Last updated: 2026-08-28
## Messages Worker 的租约必须服从 A 冻结超时

- Scope: project-local
- Symptom: 独立 Worker 单测通过，但真实 PostgreSQL Claim 拒绝 lease 大于 generator timeout。
- Cause: Worker 原先在冻结 timeout 外追加清理余量，与 A 的不可扩预算规则矛盾。
- Proven workaround: Claim 默认等于冻结 timeout，Runner 使用 timeout 减 5 秒，HTTP 再留余量；过期结算仍拒绝。
- Validation: 新增真实 PostgreSQL 调度/恢复回归，不用 SQLite 或模拟 Claim 替代。
- Do not repeat: 不修改 A 的预算上限来迎合 Worker；不对共享数据库运行会 TRUNCATE 的旧测试。
- Last updated: 2026-09-07
# 2026-09-07：Windows 全仓补跑未完成

- Scope: project-local。
- Symptom: 全仓单测停留在既有 F1-C 临时仓库夹具，子进程为 `git config user.email`。
- Cause: 尚未确认；不能归因于 Messages 生成器或数据库，也不能计为测试通过。
- Validation: 定向 Windows/Linux CPU 和 Linux PostgreSQL 联验已另有记录；全仓交由 PR CI。
- Do not repeat: 不把中止的补跑报成全量通过，不为此更改共享 Git 配置或真实源码基线。

# Frozen readiness evidence must remain target-isolated

- Scope: project-local
- Symptom: extending a shared adapter module for BW20 changed one file hash frozen by the historical nmz36 M2a Formal Readiness manifest, reducing verified evidence from 32 to 31.
- Evidence: `formal_measurement_adapter` reported `hash_mismatch` for `src/hcuopt/adapters/real_profile.py` in PR #154 CI.
- Cause: BW20 composition logic was added directly to a module used as immutable evidence by a prior target-specific audit.
- Proven workaround: restore the audited nmz36 module byte-for-byte and place BW20 composition in `src/hcuopt/deployment/bw20_m1_profile.py`.
- Validation: the repository readiness test must continue to report 32 verified evidence items without changing the old manifest or its expected count.
- Applies to: target-specific onboarding that reuses code named by an existing Formal Readiness manifest.
- Do not repeat: do not update a historical audit hash merely to accommodate a new target; isolate the new target behind a wrapper or versioned entrypoint.
- Last updated: 2026-09-15

# BW20 deployment checkout has no noninteractive GitHub HTTPS credential

- Scope: project-local
- Symptom: `git clone https://github.com/tianlinyang77/hcu-auto-opt.git` on
  `github-bw20` invoked `gnome-ssh-askpass` without a display and failed before checkout.
- Evidence: the clone destination contained no files; the existing PostgreSQL and unrelated
  containers were unchanged.
- Cause: the remote `github` account has no usable noninteractive credential for this HTTPS
  remote. This is a deployment transport limitation, not a repository or branch failure.
- Proven workaround: generate `git archive` from the locally verified commit, retain its SHA256,
  upload it to a new target-specific directory, and verify the same digest before extraction.
- Validation: commit `c7f81ac9258f1e031666074faeba85fea6c737fe` was exported with archive
  SHA256 `1fa1bd2dcd1cacd0f4ac810d392a1484a930b107b91f26138bd41e28574b0881`;
  the remote digest matched before extraction.
- Applies to: branch deployment on `github-bw20` while no deploy key or GitHub token is installed.
- Do not repeat: do not retry interactive HTTPS clone from automation and do not put credentials
  in the command line; use a verified archive or separately provision an approved deploy key.
- Last updated: 2026-09-15

# Remote CPU container UID must match the BW20 deployment owner

- Scope: project-local
- Symptom: a dependency-only container using UID/GID `1000:1000` could download packages but
  failed to publish them into the host-mounted directory with `PermissionError`.
- Cause: the BW20 `github` deployment owner is UID/GID `1002:1002`.
- Proven workaround: verify `id -u` and `id -g` before container creation and run the bounded
  dependency container as `1002:1002`; do not widen directory permissions.
- Validation: the second run installed `psycopg 3.3.5`, and a network-disabled read-only check
  imported both `psycopg` and `hcuopt` from the pinned Python 3.10 image/source deployment.
- Applies to: host-write mounts under `/home/github/hcu-auto-opt-runtime` on `github-bw20`.
- Do not repeat: do not assume the first non-root user is UID 1000 and do not solve ownership
  mismatches with world-writable directories.
- Last updated: 2026-09-15

# BW20 controller identity assets must keep LF bytes across Windows deployment

- Scope: project-local
- Symptom: the BW20 M1 Performance Worker failed before Job claim with
  `BW20 controller identity assets differ from the approved content`.
- Evidence: Git blobs for `bw20-passwd` and `bw20-group` contained the approved LF bytes, while
  the Windows-expanded deployment archive and both remote controller directories contained CRLF.
  The worker stopped before HCU use and produced no measurement sample.
- Cause: the two byte-pinned identity assets had no explicit checkout EOL rule.
- Proven workaround: declare both paths as `text eol=lf` in `.gitattributes`; deploy from a
  verified `git archive` or another transport that preserves Git blob bytes. Do not normalize
  inside `freeze_controller` or weaken the byte-for-byte safety check.
- Validation: repository-root `freeze_controller` succeeds with the LF working-tree files and
  its focused staging/performance tests pass.
- Applies to: Windows checkout to Linux BW20 controller deployments.
- Do not repeat: do not copy an unconstrained Windows working tree as the formal controller.
- Last updated: 2026-09-16

# BW20 M1 total session budget is not a transport timeout

- Scope: project-local
- Symptom: a formal Performance attempt failed before Docker creation with
  `transport timeout must be within 90 seconds`.
- Evidence: `BW20M1ProcessSession._guard()` returned up to 180 seconds from the 480-second
  total session budget, while every Docker/JSON transport call rejects values above 90 seconds.
  No sample was produced and the failed attempt remains recorded.
- Cause: the long-lived process budget and per-call transport timeout were conflated.
- Proven workaround: retain the 480-second total deadline but cap each guarded transport/stdio
  call at 90 seconds; verify create/start receive only bounded timeouts.
- Validation: the M1 session unit test now records create/start timeouts and rejects regressions
  above the transport ceiling.
- Applies to: BW20 M1 Performance process creation and stdio requests.
- Do not repeat: do not raise the transport ceiling to match a whole-session budget.
- Last updated: 2026-09-16

# BW20 M1 container PID namespace link is hidden across host UIDs

- Scope: project-local
- Symptom: the M1 worker reached its real HCU ready event, then host binding failed with
  `host namespace unavailable` before any formal performance sample.
- Evidence: the host `github` account can read `/proc/self/ns/pid` but receives permission denied
  for namespace links owned by another UID; M1 containers deliberately run as `65534:65534`.
  The failed attempt was cleaned, HCU 7 returned idle in `auto`, and no sample was accepted.
- Cause: Stage 0 originally runs as UID 1002, so its host reader assumption did not cover the
  stricter M1 non-owner container UID.
- Proven workaround: keep the non-owner container UID and no privilege escalation. The frozen
  PID1 controller attests that its namespace and the measured child namespace are identical;
  the host still independently verifies exact CID/labels, cgroup membership, `NSpid`, parent PID,
  start token, restart identity, and that the attested namespace differs from the host namespace.
- Validation: 66 focused runtime/session/factory/worker/reference tests pass, including strict
  namespace syntax, mismatch rejection, and simulated different-UID host denial.
- Applies to: BW20 M1 performance process binding on hosts that restrict `/proc/*/ns/*` by UID.
- Do not repeat: do not run the M1 container as the host deployment UID and do not add sudo or
  privileged namespace readers merely to bypass procfs permissions.
- Last updated: 2026-09-16

# BW20 local controller must not SCP artifacts or evidence through its own SSH endpoint

- Scope: project-local
- Symptom: after real container activation and process binding, M1 first failed while mirroring
  evidence from `github@10.17.1.20:<source>` and later failed while staging the Candidate Artifact
  to that same endpoint; neither path had accepted a complete performance series.
- Evidence: source evidence existed as a regular read-only file with the Worker-reported Hash;
  controller and HCU execution both ran on `github-bw20`, so the SSH loopback added an unrelated
  credential/host-key dependency. Cleanup succeeded and HCU 7 returned idle in `auto`.
- Cause: `BW20M1EvidenceMirror` reused the remote-host transport even when passed the explicit
  `BW20LocalCommandRunner`.
- Proven workaround: for the exact local-runner type, copy through a no-follow file descriptor
  and exclusive destination. Evidence is bound to the prior device/inode/size/mtime receipt;
  Artifact staging is bound to the frozen content Hash and size. Keep SCP for genuine remote
  runners, then retain the existing post-copy Hash and source re-read checks.
- Validation: focused evidence/staging/session/factory tests cover receipt drift, Artifact Hash
  drift and exclusive destinations; the formal retry must still prove the live local path.
- Applies to: BW20 M1 Workers deployed directly on the target host.
- Do not repeat: do not add SSH credentials or weaken `StrictHostKeyChecking` to make host-local
  evidence copying work.
- Last updated: 2026-09-16

# BW20 calibration timer lifetime must not span the full M1 ABBA run

- Scope: project-local
- Symptom: the formal Performance attempt completed acquisitions `a0` through `a15`, then failed
  before `a16` with `timing session budget exhausted`; no complete `performance.json` existed.
- Evidence: the Stage 0 timer session was created once before the 40-acquisition plan and remained
  registered as live for the whole run. Its deliberate 480-second process limit expired even
  though every individual Baseline/Candidate acquisition completed well within its own limit.
- Cause: the Job-bound device timer is needed only to calibrate event resolution at the start of
  M1, but the Harness deferred its close until after all acquisitions.
- Proven workaround: close and reap the owned calibration timer immediately after calibration,
  before starting the first acquisition. Keep the overall Job wall budget and every acquisition's
  independent session budget unchanged.
- Validation: a Harness regression requires the owned timer to be closed exactly once before any
  workload factory call. The failed partial evidence remains immutable and cannot be adjudicated.
- Applies to: long M1 ABBA plans using a Job-bound calibration timer factory.
- Do not repeat: do not raise the 480-second per-session safety ceiling or accept a partial ABBA
  series merely to make the formal run finish.
- Last updated: 2026-09-16

# Standalone BW20 operators must inherit the API PostgreSQL credential context

- Scope: project-local
- Symptom: the one-shot Performance recovery command reached PostgreSQL but failed with
  `fe_sendauth: no password supplied`; no database row was changed.
- Evidence: the running API process had both `HCUOPT_DATABASE_URL` and `PGPASSWORD`, while the
  first standalone command exported only the URL. Variable names were inspected without printing
  credential values.
- Cause: the URL deliberately omits the password and relies on the process-local PostgreSQL
  credential environment.
- Proven workaround: when a trusted same-host operator must share the API database authority,
  copy both variables directly from the live API process environment into the one command without
  logging either value. Keep credentials out of source, command output, evidence, and Git.
- Validation: the recovery transaction subsequently matched the exact terminal snapshot and
  authorized attempt 7/7; the command output exposed only non-secret audit fields.
- Applies to: one-shot BW20 recovery or reconciliation commands that intentionally target the
  same PostgreSQL database as the running API.
- Do not repeat: do not add a password to the repository, CLI arguments, DSN output, or shell
  history merely to make a standalone operator connect.
- Last updated: 2026-09-16

# SGLang image import queries HCU identity during Triton import

- Scope: project-local
- Symptom: a CPU-only capability probe can load the SGLang distribution metadata but
  `import sglang` fails with `RuntimeError: No HIP GPUs are available` after Triton HCU tuner
  initialization. Without `/opt/hyhal`, the earlier failure is instead a missing
  `librocm_smi64.so.2`.
- Evidence: the pinned SGLang 0.5.12 image contains `bench_serving.py`, its main entrypoint,
  `--output-file`, `--max-concurrency`, `--warmup-requests`, and TTFT/TPOT/stream support when
  inspected without importing the package.
- Cause: this image's Triton package queries `torch.cuda.get_device_name()` at module import time.
- Proven workaround: for a no-HCU capability probe, inspect `importlib.metadata` and parse the
  installed source without importing `sglang`. Reserve a leased HCU only for an execution probe.
- Validation: a network-disabled, no-HCU container returned the exact SGLang distribution version
  and benchmark CLI capability inventory; the temporary probe and container were removed.
- Applies to: capability discovery in the pinned BW20 SGLang image.
- Do not repeat: do not classify CPU import failure as missing SGLang, and do not map an HCU merely
  to enumerate installed benchmark files.
- Last updated: 2026-09-16

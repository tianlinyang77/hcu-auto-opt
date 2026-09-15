# BW20 时钟Backend静态审核绑定

> 2026-09-14：当前正式验收采用宿主`auto`只读策略，本Backend不参与运行，也不再是
> Stage0前置条件。本文保留的是未来确需主动控制频率时的安全设计。

状态：**已实现本地只读校验；没有Backend实现、加载、执行或时钟授权。**

## 解决的问题

真实时钟Backend不能只靠一个Python对象或函数名接入。部署前必须先固定它服务的Target、
制品字节、持久日志、权限提供方、恢复策略和可写范围，防止更换机器、制品或日志后仍沿用
旧审核结论。

`BW20ClockBackendReviewManifest`固定以下内容：

- Target ID、Target Fingerprint和唯一BW20资源ID。
- `confirmed_default_auto_v1`策略、1500MHz核心频率目标。
- 显存策略固定为`observe_only_no_write`。
- Backend制品绝对路径及独立SHA256；检查时只读文件，不导入、不执行。
- 与时钟会话及Worker共用的持久`ClockJournal`绝对路径。
- 不含密钥的权限提供方标识，以及`manual_fenced_takeover_v1`恢复策略。
- 可选硬件验收证据路径及SHA256；路径和Hash必须成对出现。

未知字段被拒绝，因此密码、Token或临时命令不能塞进这个清单。绑定文件本身也必须由调用方
提供独立SHA256。

## 只读输出边界

`inspect_clock_backend_review()`校验清单、Backend制品Hash、Target绑定、日志对象与可选验收
证据Hash。日志状态使用SQLite `mode=ro + query_only`读取；测试确认检查前后数据库字节Hash
不变。

只读CLI只允许打开已经初始化的日志，不会为缺失路径新建空SQLite文件：

```bash
python -m hcuopt.deployment.bw20_clock_backend_review \
  /absolute/path/clock-backend-review.json \
  --sha256 sha256:<manifest-hash> \
  --target-id bw20-sglang-0.5.12 \
  --target-fingerprint sha256:<target-hash>
```

CLI成功表示静态输入与Hash可复核，退出0不代表Backend可执行、硬件验收通过或获得改频权限。
不存在、重定向、不是普通文件或schema不完整的日志会直接拒绝；不会调用普通构造器补表。

即使所有静态字节一致，输出仍固定：

```text
backend_imported=false
backend_executed=false
clock_mutation_performed=false
hardware_acceptance_semantics_verified=false
clock_control_bound=false
execution_allowed=false
stage0_accepted=false
automatic_release_allowed=false
```

提供一个验收JSON只能证明“这份文件与Hash一致”，不能证明其内容真实、测试设计正确或已经
人工裁决。因此检查器会保留`hardware_acceptance_semantics_not_adjudicated`；没有证据时保留
`hardware_acceptance_evidence_missing`。日志存在未恢复记录时，再增加
`clock_journal_requires_reconciliation`。

## 当前验证

静态审核、恢复与bootstrap新增聚焦：**49 passed / 1既有Starlette警告**；当前代码版本
扩大BW20回归：**669 passed / 7平台条件skip / 1既有Starlette警告**。覆盖制品/清单/Target/日志/
证据篡改、相对路径、半套证据、秘密字段、未恢复日志，以及静态检查不执行非法制品。

所有文件与日志均为本地测试夹具；没有连接远端、没有HCU容器、没有真实Backend、没有修改
频率或Target。后续只有经过独立硬件验收和人工裁决的绑定，才有资格进入正式部署评审。

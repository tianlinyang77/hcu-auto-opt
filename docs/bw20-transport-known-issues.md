# BW20 transport 实机兼容记录

## 2026-09-11 真实 C 准备暴露的地址、UID与采集参数问题

- 范围：项目本地。SourceManager旧逻辑把同仓库HTTPS/SSH克隆地址判为不同；新增仅限
  GitHub标准地址的身份归一，不放宽host/owner/repo、HEAD、干净状态与源码Hash校验。
- C容器需要固定1002:1002并在create后核验User，避免root私有证据阻塞控制端读取。
  不用chmod扩大输出目录权限，不改宿主设备权限；错误UID有拒绝测试。
- Profiler配置的10/5默认采样次数不代表SGLang实际执行次数；根据固定源码设为1次预热、
  1次采集。使用真实bench_one_batch路径，不伪造kernel trace；依旧只由原D判级。
- Windows profiler输出mount拼写应复用原Overlay路径helper，并从明确输出URI核对其
  父目录；不能把`/C:/...`直接当Windows Path用于relative_to。已补真实配置重绑定测试。
- 新专项40通过，Linux扩大343通过/1条件跳过；原SourceManager6项Windows符号链接
  权限失败保留，Linux均通过。部署配置、证据、仍未实测的范围见C运行准备记录。

## 2026-09-11 同机控制端：不要依赖 loopback SSH 或仅打包 Python

- 范围：项目本地。BW20 SSH登录自己因没有已知host key失败；未禁用StrictHostKeyChecking、
  未复制私钥。新增显式非root BW20LocalCommandRunner，同机命令不经过SSH；源码staging的
  本地归档复制只允许新UUID目录中的controller.tar，后续仍执行原独立manifest检查。
- 实际导入API先暴露缺少`storage/sql/0001_walking_skeleton.sql`，再暴露无Git元数据时
  未配置`HCUOPT_SOURCE_COMMIT`。打包/解包白名单补齐精确SQL目录；部署环境显式提供base
  commit并另记未提交snapshot manifest，不能把base commit声称为当前实际代码版本。
- 验证：BW20 Python3.10下394通过/1非POSIX条件跳过；同机stage_controller真实复制并
  双次核验通过。均无HCU、无容器启动、无数据库联验。证据见CPU控制端部署记录。
- 不要重复：单模块导入/AST成功不代表API能运行；缺省checkout路径、SQL和启动身份都须
  在实际部署环境核对。包内资源新增时同时更新冻结和解包白名单，不整体上传本地results。

## 2026-09-11 七探针接线：阶段健康检查与源码包协议

- 范围：项目本地 BW20 Stage0 部署。最新实现状态以接入文档文首为准，以下旧切片保留。
- 原因/症状：Overlay 在 baseline 与 candidate 之间也调用 health；若复用会永久关闭
  executor 的清理动作，baseline 后下一阶段会被自身停止闸门拒绝。
- 修复：C 阶段健康检查只核对本任务容器已不存在和主机状态；真正 fence/cancel 才关闭
  executor。失租仍停止后续启动并清理完整 CID，不因阶段检查放松租约。
- 验证：原 Overlay 三阶段证据夹具、阶段间 health、不明创建、运行中失租和超时清理
  均有测试。没有运行真实 BW20 C 容器，不能把夹具生命周期报告为实机恢复证明。
- 不要重复：源码包只包含 `.py` 会遗漏 `evaluation/protocols/*.yaml`，导致注册协议
  在镜像中加载失败。freeze 与解包白名单已同步补齐该精确目录，拒绝携带无关 YAML。
  新源码必须重新冻结 Hash，旧197文件快照不能用于新 Profile。
- 路径细节：跨 Windows 夹具与 Linux 的证据 mount 拼写使用原 Overlay mount helper；
  真实 C Controller 要在 BW20 同机运行，不靠转发 Windows 路径启动远端 Docker。
- 缓存边界：新 `s0-g0-bw20-v1` 只登记分配器清理，原 v1/v2 canonical Hash 不变。
  不填写虚假的硬件 flushed 状态；原时钟/manual/环境闸门没有解除。

## 2026-09-11 部分探针不能注册为完整 Profile

- 范围：项目本地部署接线。B 的五探针已有 adapter，但原 Stage0 Router 明确要求七种探针。
  C 的 Profiler/Hotpatch 和统一清理路由缺失时，不能仅凭 stage0_probe 能力名注册完整配置。
- 修复/处理：提供 B 的显式部署组合入口，默认 Catalog 不变；原 Target blockers 不豁免。
  每次上传和创建都受原 Worker 租约检查，失败 staging receipts 留在原 diagnostics。
- 相关协议缺口：v2 YAML 没有定义 cache 清理范围，D 的 cleared 判定不能由 allocator
  empty_cache 自动解释为硬件 flush。下一步方案在接入文档单列，未改旧协议或放行。
- 验证：本地专项21项通过，不是新 Linux/HCU 验收。不再以“构造B adapter成功”报告完整
  Profile 或七探针成功，也不为了消除 unknown 填写未经证实的 cache 状态。

## 2026-09-11 D 回执接线与 calibration 会话正常退出

- 范围：项目本地 BW20 Stage0。旧 health 缺少 resource_id，原 Formal reference 会拒收；
  calibration session 若只 force_close，没有正常 waitpid，不能证明有效计时进程正常收尾。
- 修复：健康回报显式绑定资源；factory.finish 先正常 close/reap 再逐个精确 CID 清理，
  单个会话异常不阻断其余清理。失租时不再执行测量，未取得 reap 证据仍由 D 拒收。
- 独立 D 既检查初始也检查最终宿主原始 status/cgroup/stat，不比较整个动态 procfs 文本。
  仅身份字段要求稳定，防止正常运行时 CPU 时间等变化被误当成 PID 重用。
- 验证：原七探针入口缺少 BW20 旁证时拒收、重算 Hash 后的18类内容篡改拒收、四探针
  正向逐样本绑定、正常关闭/失租/退出异常/多会话清理均有单测；范围和数量见接入文档。
- 不要重复：未清理不是正常退出，清理成功也不是正常退出；只比较摘要或 Hash 不够。
  测试 Formal reference 应传枚举/UUID 或走 JSON 校验，不为了 wire 字符串放宽 strict。

## 2026-09-11 新接线的 Windows 证据路径过长

- 范围：项目本地。新adapter把job UUID目录叠加在原run/probe/measurement UUID目录上，
  Windows创建原子写入临时文件时出现FileNotFoundError；不是测量结果错误或远端故障。
- 修复：原始测量证据继续使用原output_dir布局，job目录只保存diagnostics。
  不修改原证据hash/原子写入逻辑，不修改系统长路径设置。
- 验证：原Worker成功/失败/失租及扩大回归206项通过。后续不要重复嵌套相同用途的UUID。
- 相关边界：缓存回执的真实操作是allocator释放，不是L2/HBM flush；默认nmz36输出不变，
  BW20新session要求新回执，不能继续使用缺少cache_receipts扩展的旧controller快照。

## 2026-09-11 设备采集与控制端时钟

- 范围：项目本地 BW20 部署，不改通用 nmz36 协议。
- 实机 SMI 有 KFD PID 但 HCU Index 为空。旧逻辑会跳过；新逻辑对照 sysfs 全局清单，
  归属未知时保留潜在干扰，不能据此证明卡7空闲，也不能声称所有 PID 都在卡7。
- 首轮采集遇到他人 `/proc/<pid>/exe` PermissionError。保留未知exe和警告即可；
  进程starttoken/清单缺失仍然失败关闭，不sudo、不删进程。失败stdout/stderr限长留存。
- 正式 Harness 时间戳不能混用 Windows CLOCK_MONOTONIC 与远端 Linux 子进程纪元。
  新主机时钟通过Python3.6 ctypes取得整数timespec，并与telemetry共享boot_id。
  这不代表SSH传输延迟已符合校准阈值，须交原D实际复核。
- 最新只读实机采集成功，卡7约40.9 GiB显存被占用，未启动HCU测量。证据和边界见
  `bw20-stage0-integration.md` 9月11日最新章节。不要因瞬时busy=0就启动。
- 本地新用例发现的TARGET夹具导入名错误已修；未为迁就测试改生产接口。

## 2026-09-10 原 Worker 接线测试边界

- 范围：项目本地；新增租约检查API和SDK能力测试。
- 新测试先后暴露夹具问题：把数据库UUID直接用于HTTP JSON、未开启FastAPI lifespan
  导致`app.state.repository`未初始化。已改为真实wire字符串及TestClient上下文管理，
  显式模拟repository迁移，未改生产API来迁就夹具。
- 不要重复：SDK目录是`src/hcuopt/workers/`，先用`rg --files`定位测试，不猜不存在的
  `worker_sdk/`目录；PostgreSQL专项测试必须提供独立测试连接，不能用未配置跳过冒充通过。
- 验证：新API回归与原Worker completion/heartbeat回归通过，真实数据库联验尚未运行。

## 2026-09-10 CPU-only 演练

- 范围：项目本地；固定 BW20 Docker27.2.1 与现有 OpenSSHCommandRunner。
- 症状：创建失败但没有原始原因；修复诊断后又遇到 bytes 无法写入JSON；正常退出后清理误判。
- 原因：传输层误以为 CommandResult 为文本；`--pid=private` 不是有效 Docker 参数；
  原不存在回执白名单未包含本机daemon文案。
- 修复：按原bytes契约在传输边界UTF-8解码；保留创建失败的plan/result供对账；
  省略PID模式参数并核验实际独立namespace；增加精确完整CID的daemon不存在文案。
- 验证：真实CPU正常/超时两条链路通过，完整收据见 `bw20-stage0-integration.md`。
  bytes契约、创建失败、异常CID、默认拒绝CPU测量协议均有测试覆盖。
- 不要重复：不要用只返回str的测试runner代替真实bytes契约；不要因namespace权限错误
  改成hostPID/sudo；SSH失败不等于容器消失；没有CID的创建不确认必须对账，不能按名称盲删。
- 保留失败：`4c6aa877...`/`4aeedd96...`/`6e5e9bf3...`为创建失败尝试，第二份诊断JSON
  因bytes序列化中断而不完整；`2263488c...`为正常退出后清理误判。均保留在操作任务目录，
  不篡改为成功；最终完整成功记录为`37756ec6...`。
- 适用边界：仅CPU传输、进程身份和容器清理；不能作为Stage0/HCU测量或优化收益依据。

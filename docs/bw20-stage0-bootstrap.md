# BW20 Stage0：统一部署组合入口与时钟事务边界

> 2026-09-14 当前执行口径：正式BW20协议采用`host_auto_observe_only_v1`；不需要时钟
> Backend或ClockJournal，不写频率。旧的manual/backend章节作为设计历史保留，不是当前验收前置。

2026-09-11。**已完成CPU工程验证，不是后台服务上线或HCU实测。**

## 已实现的入口

`hcuopt.deployment.bw20_stage0_bootstrap` 将原有B五探针、C两探针、输入复核、
Target/Profile Catalog及原Worker组合成同一个部署对象；没有新队列、新判决器或fake回退。

部署配置只包含显式绝对路径和独立Hash：准备目录、控制端快照及tar包、准备记录Hash、
Python运行时证据及Hash、协议Hash、证据根目录。配置不接收数据库密码或模型密钥。
配置文件本身也必须由调用方独立固定Hash。

```bash
python -m hcuopt.deployment.bw20_stage0_bootstrap <deployment.json> --sha256 <config-sha256>
```

这个CLI只检查、不监听端口、不写数据库、不领取任务。输入一致但Target仍有Stage0
blocker时输出`inputs_verified=true / target_admitted=false`并退出2；这不是检查器故障，
也不是放行。API和Worker工厂同样拒绝这种状态。

```python
deployment = load_deployment(
    config_path,
    config_sha256=config_pin,
    runner=bw20_local_runner,
    clock_session_factory=reviewed_clock_session_factory,
)
app = deployment.make_application(repository=explicit_repository)
worker = deployment.make_worker(
    api_url="http://127.0.0.1:8820", worker_id="bw20-stage0",
    clock_journal=reviewed_clock_session_factory.journal,
)
```

- API使用只有当前冻结Target的Catalog，以及只有`bw20-stage0-v1`的适配器Catalog。
  构造前重新检查部署输入和原Target准入。
- Repository必须显式提供。通过新增可选`create_app(auto_migrate=False)`参数禁止隐式迁移，
  即使进程环境`HCUOPT_AUTO_MIGRATE=true`也不迁移；未传该参数的原应用行为不变。
- Worker使用原GPU Worker、原JobHandlers和七路Registry，resource固定为BW20 HCU7。
  `clock_journal`须由部署方显式提供，并与时钟事务使用同一个持久日志。
  实现上要求复用工厂持有的同一个`ClockJournal`对象，不能临时创建同路径或不同路径的
  空日志替代。
  仅接受显式端口的loopback HTTP API URL；构造时不注册、不claim、不启动轮询。
  调用方仍负责原服务生命周期、数据库schema/权限、最终D证据读取器和有界启动。
- `clock_session_factory`未绑定时，部署对象只能执行只读`check()`；报告显式给出
  `clock_control_bound=false`，API和Worker工厂均拒绝启动。这里没有no-op时钟后端，
  不能让正式服务在缺少已审核控制实现时表现为可运行。
- 本入口没有引入HTTP认证或生产部署能力。不能把原控制面裸露到公网。
- 上传快照不包含`.git`。启动原API前须注入真实控制端**基准提交号**
  `HCUOPT_SOURCE_COMMIT`；它不是完整工作树身份，实际部署字节仍由Controller manifest绑定。
  不允许因为缺少Git而填假提交号或删除原API检查。

## 同机验证结果

最终CPU审核目录：
`/home/github/hcu-auto-opt-runtime/bw20-stage0/fec4c5ab-46ad-449f-99a9-e3df4da3853d`。
控制端233文件，manifest
`sha256:1eadb47e72364f0eb225766d9ff0ded3927a382905bc1981e995c5570e9c90b4`。

审核配置`deployment.json` Hash：
`sha256:404a0db8d256ebfc8685d6fd147a1f0809a8c1bea5e5707c4e47f9aa1329d272`。
它引用原4548d046准备目录和已核验的运行时证据，只用于本次组合核对。旧准备目录及候选未改。
最后正式部署时仍需重新冻结与已审核Target匹配的输入；不能用更改blocker后的Target搭配旧指纹。

- BW20 Python3.10：**73 passed / 1既有Starlette警告**。包含真实API工厂/Worker构造、
  七路组合、缺失/变更输入拒绝、时钟事务故障注入；单测中的设备/DB/ClockBackend是替身。
- 同机真实输入检查：源码、模型、候选、制品及运行时证据一致，七路齐全；保留5个Stage0
  blocker并退出2。没有创建真实Job或伪造lease，没有启动API/Worker或HCU容器。
- Windows扩大回归：**124 passed / 2符号链接权限skip / 1既有Starlette警告**，覆盖部署、
  时钟事务、C探针、输入/运行时核验、原Worker活租约及B部署。与Linux不是同一测试集。
- 首次远端检查ef1da36d中60passed/1failed：操作脚本漏了`HCUOPT_SOURCE_COMMIT`，
  API原检查拒绝无Git快照。补启动参数后4097e559通过；最终fec4c5ab还包含时钟事务测试。
  保留所有历史证据，不把失败重写为通过。

最终报告：`results/bw20-bootstrap-check-fec4c5ab-46ad-449f-99a9-e3df4da3853d/report.json`，
SHA256 `587c2ce18b90a8e6159af3f6c57697f5f2cd3641e4cfd1a0180b473e64d5539f`。

## 时钟部分：已实现事务协调，尚未实现驱动写入

`BW20ClockSession`只协调：捕获完整原策略 → 确认控制权 → 请求manual1500/1800 →
回读策略 → 工作负载 → 恢复原策略 → 回读确认。它没有默认Backend、sudo命令或sysfs写入。
`authorization_id`只记录批准引用，不验证批准真伪；真实授权必须由部署方持有的
`assert_control`过程内回调实施，不能从Job JSON取一个字符串就当作授权。

- 原策略必须包含模式与全部启用频率档位；瞬时600MHz读数不是完整可恢复配置。
- 应用设置时部分成功后报错，仍尝试在有效控制权下恢复。
- 正常退出、工作负载异常和KeyboardInterrupt均走恢复；命令返回成功不等于恢复成功，
  必须回读完整原策略。auto模式不按某次瞬时频率假定恢复正确。
- 失去控制权后不再写频率，标记`reconciliation_required / quarantined`，不自动重试或释放。
- `ClockJournal`现在是时钟事务的必填依赖。改频前在本机SQLite日志中提交原策略与
  `mutation_possible`意图；只有恢复后读回一致且控制权仍有效，才提交`restored`。
  使用独立连接、`synchronous=FULL`、事务和未结束资源唯一索引，保留操作与状态事件。
- 新会话遇到未结束记录直接拒绝。失租、恢复失败、日志更新失败都保留待核对状态；
  不自动重试、不因重启清空。突然退出后的记录保留已有CPU测试，**不等于断电或真实硬件恢复验收**。
- 日志不是租约服务或第二套任务队列，不能授予控制权。部署必须指定同一个受保护的
  本地持久路径，不得用临时目录、新数据库或删除日志绕过未结束操作。
  SQLite耐久性依赖底层存储正确实现flush。
- Worker接线见下节：当前已强制将日志检查接到BW20 Worker领取/执行/完成及清理上报，
  但尚无自动恢复接管、真实Backend或特权写入代理，**不是硬件恢复或全局隔离的实机验收**。
  实机接入前仍需驱动语义验证、受控恢复路径和当期真实数据库/Worker联验。

本轮未改频率、未占用HCU、未修改Target准入、未提交或推送；前端没有新性能数据。
下一切片是确认驱动完整策略的读取/写入/恢复接口，并将时钟事务与原租约、清理证据接线，
之后再在获准的独占窗口执行Stage0。不能绕过这部分直接调优化收益。

## 2026-09-11：驱动接口只读核查

实际检查BW20物理HCU7（PCI `0000:b1:00.0` / `renderD135`）：

- 当前模式auto、瞬时sclk600MHz；支持列表包含1500MHz（档位10）。
- mclk只有1800MHz，sysfs明确标记`DPM disabled`。不能据此直接执行mclk写入。
- 三个sysfs控制文件均root拥有、0644，github账号不可直接写入。未提权或改权限。
- 本机`amdsmi_frequencies_t`定义只有`num_supported/current/frequency[]`，没有已启用mask。
  `amdsmi_set_clk_freq`说明设定mask会切入manual、切回auto回默认状态；这不证明能
  无损捕获与恢复任意现存策略，也不证明当前驱动执行语义已通过测试。
- `hy-smi --showclkfrq --json`会混入文本，不是有效JSON；单纯`--showclocks
  --showperflevel --json`也出现重复空键。不将这类输出反序列化后当完整恢复凭据。
- 本轮没有执行`--set*`、`--load`、`--resetclocks`；后者还影响OverDrive，不能用作兜底恢复。

下一步需要确认完整策略读回接口，或评审一个明确只接管已知auto基线的更窄恢复协议；
不能用支持频率表假冒启用mask，也不能仅凭瞬时频率相等放行。

本切片验证：Windows相关回归**87 passed / 1既有Starlette警告**；BW20原隔离
Python3.10环境**18 passed**（设备Backend为替身）；四个新增/更新Python文件Ruff通过。
覆盖原策略持久化先于写入、部分写失败、失租、日志失败、并发唯一性、旧状态拒绝、
子进程`os._exit`后日志保留。没有做真实SIGKILL改频、断电或硬件恢复试验。

最终证据：
`results/bw20-clock-journal-2da9a386-3925-4ba9-8590-dbd2a2ba04fd/report.json`，
SHA256 `2884963c0f93c52fc433c7c0c5430bc86b35d8b2db5a72fb86205183a2009e99`。
同目录保留只读driver observation；远端对应目录保留234文件控制端快照，manifest
`sha256:2a185ad9762705789e7b2270bac046f355c837019d6b01bdbde9c3cdf4adb543`。
本切片没有提交/推送，没有更新前端性能结果。

## 2026-09-11：Worker资源安全检查接线

这部分复用原Worker和PostgreSQL资源状态机；不新建调度器或改数据库表结构。

- `Worker(resource_guard=...)`接受部署方过程内检查回调，不读取任务JSON中的同名字段。
  普通Worker默认行为保持不变；BW20的`make_worker()`强制提供`ClockJournal`并限定资源。
- 注册/领取前、领取后执行前、最后续租后完成前检查日志。未结束意图或读取失败均拒绝；
  领取后的拒绝按原失败流程处理，而不是将成功结果提交。
- 清理上报、失败上报和失租后的补报路径检查当前日志。未恢复或读盘失败会保留进程fence
  信息，但强制`health.healthy=false / quarantined=true`。处理器给出的“健康”不能覆盖它。
  日志为空也不能把原本失败的容器清理提升为健康。
- 原Repository据此将资源结算为`quarantined`，而非`available`；本切片没有修改Repository。
- 日志运行中被删除时拒绝，不隐式创建新的空库。显式初始化仍会创建日志，因此部署时
  必须使用同一个受保护的持久路径；不能通过重新初始化绕过历史操作。

仍未完成：恢复接管的授权/执行、真实硬件Backend与时钟事务实际运行、真实PostgreSQL
并发及异常恢复联验。没有把日志里的字符串当作有效lease，也不在失去租约后改频率。
检查结果为空不是改频授权，更不是Stage0通过证据。

验证：Windows **108 passed / 1既有警告**；BW20原Python3.10 CPU环境
**108 passed / 1既有警告**（测试集不同）。Ruff通过；实际输入复核仍为
`inputs_verified=true / target_admitted=false`，七路齐全、5个Stage0 blocker保留，退出2。
Worker控制面Client及Repository连接为测试替身；没有把状态选择单测算成PostgreSQL联验。

最终报告：`results/bw20-bootstrap-check-ec40d44c-5ccb-4540-880e-08a530848a27/report.json`，
SHA256 `391ea837d7b09a827903b51985742d5b20ce29e3b739a2ce54e936d54099ab53`。
控制端manifest：`sha256:20789e6561444b344b994fdf313615ba6469696c3045db4d6fc5e28bcbbaf5b5`。
审核仍引用旧4548d046准备目录，正式部署需重冻并绑定当前控制端，不代表整套服务已上线。
本切片未改频率、未启动HCU工作负载、未修改Target、未提交或推送。

## 2026-09-11：真实PostgreSQL资源隔离验证与时钟预检

在nmz36已有测试PostgreSQL中以新建随机`hcuopt_lease_check_*` schema运行，
**10 passed / 1既有警告**，测试创建的schema均已删除、无新增遗留。没有迁移或清空
共享表，没有运行HCU；这不是BW20同机数据库部署验收。

这次是实际数据库，而非连接替身，覆盖：

- 原活租约API检查不续租；过期、隔离、旧fencing和lease错配拒绝，真实锁等待后复核过期。
- Worker将遗留时钟意图或日志丢失转换为不健康清理；原Repository在数据库中记录quarantined。
- Worker经过原HTTP补报入口重新检查日志，不能用缓存“健康”释放资源；已模拟确认恢复的
  日志允许原API恢复available。**日志恢复状态是测试模拟，不是驱动恢复验收。**
- 旧fencing token的清理报告返回409，不释放新一轮资源。

完整任务claim→执行→complete/fail、恢复接管、真实驱动写入与并发故障仍未全部联验；
新增测试只扩展了临时schema列和用例，没有改生产Repository/状态机。
报告：`results/live-lease-postgres-5edcd0c3-dfc4-4707-a780-0783383a2a24/report.json`。

另新增只读时钟预检与[默认auto收口方案](bw20-clock-auto-scope-proposal.md)。
本地时钟/Worker相关回归**51 passed**；BW20 Python3.10时钟相关CPU回归**37 passed**，
两者不是同一测试集。远端CPU报告：
`results/bw20-clock-journal-00a0669a-dd98-483c-be43-5f40ad4f9397/report.json`。
上述预检当时方案尚未采纳、原ClockPolicy完整恢复契约未改、Target及冻结协议未改。

## 默认auto策略：设计已确认，实机操作暂停

用户随后确认首版采用“已确认默认auto基线”的范围，并明确要求先不动机器、不改频率。
新增`BW20AutoClockSession`及`DefaultAutoBaseline`，复用原事务协调器、日志和Worker；
不修改完整`ClockPolicy`语义，不提供真实设备写入Backend，不自动注册或改变Target准入。
详见[默认auto方案与实现](bw20-clock-auto-scope-proposal.md)。

异常后的独立恢复接管约束见[默认auto恢复接管契约](bw20-clock-recovery.md)。恢复不属于
Agent/Apex或普通Worker权限，当前仅有本地协调器和模拟测试，没有真实Backend或调用入口。
真实Backend进入部署前还必须通过[静态审核绑定](bw20-clock-backend-review.md)，固定Target、
制品Hash、日志、权限提供方与恢复策略；静态通过仍不会设置`clock_control_bound=true`。

本轮仅本地验证：**106 passed / 1既有警告**，Ruff通过。设备与控制面连接均为模拟，
没有连接远端、没有重跑PostgreSQL、没有实机测量或恢复验收。代码未提交/推送。

## 2026-09-14：Stage0测量生命周期接线（仅本地模拟）

五个B类探针中，`fingerprint`保持只读，不创建时钟会话；`timer`、`noise`、
`known_signal`和`null_signal`统一执行以下顺序：

```text
进入部署方时钟事务 → 执行测量 → 清理本任务容器 → 恢复默认auto
→ 读取恢复回执 → Worker持久日志检查 → 控制面资源结算
```

容器清理被放在恢复auto之前，避免恢复完成后仍遗留测量负载。工作负载异常同样先清理
本任务容器，再退出时钟事务。恢复失败、回执缺失/非对象、`restored`不为true，或
`quarantined`不为false时，即便容器清理曾报告健康，也会覆写为不健康并要求隔离。
后续Worker还会通过同一个持久`ClockJournal`再次检查，处理器不能用缓存回执越过日志。

本地聚焦回归：**146 passed / 1既有Starlette警告**，Ruff通过。测试中的时钟、设备、
容器和控制面均为替身；没有连接远端、改频、启动HCU容器或执行Stage0实机验收。
扩大到全部BW20相关单测与Worker资源守卫后：**615 passed / 7平台条件skip /
1既有Starlette警告**，全库Ruff通过。
全量Windows回归在本接线前记录为1404 passed / 39 skipped / 19 failed：18项是Windows
符号链接/只读临时目录权限差异，1项是冻结Formal证据对源码改动的预期告警；没有通过
重算Hash或放宽断言掩盖。待当前累积改动形成审核边界后再运行对应Linux/CI回归。

## 2026-09-14：可选Backend静态审核展示与恢复错误保留

部署配置可选携带成对的`clock_backend_review`绝对路径与
`clock_backend_review_sha256`。加载时只读固定审核清单、Backend字节和既有持久日志，
并将结果展示在`check()`的`clock_backend_static_review`字段中。缺失日志不会被创建，
静态审核日志与运行时工厂日志不一致时拒绝组合。
每次`check()`都会重新只读检查清单、制品和日志，不能缓存加载时的`journal_clear=true`
来掩盖随后出现的未恢复记录。

这个展示字段永远不能设置顶层`clock_control_bound=true`。即使静态Hash全部一致，未注入
正式`BW20AutoClockSessionFactory`时，API和Worker仍拒绝构造；静态报告自身也固定
`execution_allowed=false / stage0_accepted=false / automatic_release_allowed=false`。

恢复协调器现在还会同时保留“原恢复失败”和“失败状态落盘失败”，避免吞掉第二个异常；
若恢复确认已经完成但成功回执落盘失败，也会保守回到待核对状态。新增三模块聚焦回归
**49 passed / 1既有Starlette警告**；当前代码版本扩大BW20回归
**669 passed / 7平台条件skip / 1既有Starlette警告**。两次检查均未连接远端、未启动
HCU容器、未改频率、未改变Target blocker，也没有形成Stage0硬件验收。

## 2026-09-14：切换为宿主auto只读验收

新增`BW20AutoObservationSessionFactory`。它只把非fingerprint测量绑定到当前exclusive
lease，不含Backend、时钟日志、sysfs写入或`hy-smi --set*`。原telemetry在每轮保留
`hy-smi`/sysfs频率和模式，D要求全程为`auto`，并按首次观测检查sclk/mclk在协议容差内
稳定；测量噪声、MDE、已知信号和空信号仍按原阈值裁决。

bootstrap现在分别报告`measurement_clock_policy_bound`和`clock_control_bound`。当前正式
绑定前者为true、策略为`host_auto_observe_only_v1`，后者必须为false。向auto协议注入
manual写频Factory会被拒绝。历史Backend/恢复代码不参与当前运行，也不再阻塞实机验收。

# BW20 正常 API 联验记录（2026-09-09）

## 最新结果：修复后的正常 API 联验通过（2026-09-09 心跳续跑）

此前阻塞已解除：只读复查时旧 vLLM CI 容器退出，HCU7 busy=0、VRAM=2207744 bytes、
KFD为空；没有停止或修改其他容器。沿用原 schema 和资源台账，保留旧 rejected 任务，
通过正常 API 创建关联任务 `b90b2fdb-24da-4e70-98ca-1a64d38bd0af`。

**简版：修复版 API → Build Worker → Coordinator → HCU Worker → 原 D → 数据库闭环通过，
新任务已到 awaiting_signoff。输出一致、资源清理通过、独立证据核验通过；尚未人工签核，
不代表候选已激活、Stage0已通过或模型取得性能收益。**

### 本次实证

- 结果目录：`results/bw20-api-acceptance-37e51bbb-6171-4c27-a8da-e4d66c7a97aa/`。
- API source_prepare、noop_build、framework_smoke 均由原 Worker 完成，执行退出码0。
- 本次加载修复后的 Worker SDK，SHA256 `eec79b496b66454b23be4494a54817025174933bdce1a8b228b8e84e7b893cf8`。
- Baseline／No-op 输出均为 ` Paris. It is the largest city in`，prompt5、completion8。
- 原 D passed=true，数据库资源 available；收尾 HCU7 busy0、VRAM2207744、KFD为空。
- 两个独立容器的 live HostConfig、精确镜像、单render映射、CPU/NUMA、非特权/只读/无网络
  范围、Docker die0/destroy记录均已由独立核验脚本检查。
- `results/verify_bw20_api_acceptance.py` 实际运行退出0，核对原始文件Hash、D结果、
  API Summary与数据库回读；收据 `independent-readback.json`，不是仅编写了验证代码。
- API Summary SHA256：`4d3c037fca0a030424fd380c66cd46b5c65a413864fd185666f700b88ee5a64c`。
- EvidenceBundle SHA256：`33aa41157475767adcb51f04583b936cd5ac83813c3a9e7c93d68d66c84b0ba5`。
- 本轮相关本地回归107passed、1项既有Starlette弃用警告；相关Ruff通过。
  这些是本机Python3.12回归，远端推理使用固定镜像Python3.10，不混称全栈3.10验证。

新只读结果页：`http://127.0.0.1:4193/?frameworkSmoke=b90b2fdb-24da-4e70-98ca-1a64d38bd0af`。
真实数据库自检：无凭据401、合法凭据200、四项检查全true、ready_for_human_review=true。
旧4192失败页和4191 Agent页保留。未进行浏览器点击/视觉验收。

心跳 `hcu7-bw20` 已按其完成条件暂停，避免重复跑已通过的联验。
未提交、推送、合并、代签或发布。下一步是人工审核本次证据；BW20 Stage0、候选激活与
恢复、性能收益和可复用部署入口仍单列待办。

## 历史阻塞复核（下文为首轮失败记录，已被上述新任务结果更新）

连续三个目标推进轮次均遇到共享CI设备占用。最近一次实查：
`vllm_hcu_34349710198_1_accuracy-gfx936` 容器仍运行且KFD有PID4009004；
等待该精确容器时它已退出，但再次查询发现新的
`vllm_hcu_34349799970_1_contract-hcu-gfx936-p1of2` 已启动。
这表明空闲瞬时读数不构成可靠窗口；没有停止任何CI或绕过设备检查。

本地结果页已交付，见 `docs/bw20-framework-viewer.md`，本机4192入口实查HTTP200。
原任务仍为rejected。验收目标暂标阻塞，不是完成：需协调HCU7不受CI占用的窗口，
或另行授权并锁定其他空闲设备后，继续修复版正常API实机复验。
修复后正向结果、人工签核、BW20 Stage0及优化证据仍未完成；不会以CPU测试替代。

## 简版

正常 HTTP API → Build Worker → Coordinator → HCU Worker → 原 D 判定器 →
数据库／Summary API 的链路已实际执行，不再只是手工向 Repository 塞任务。
Baseline 和 No-op 均执行成功，输出完全一致；但 No-op 收尾出现其他 KFD 进程，
清理健康检查失败，任务正确落到 `rejected`、资源进入 `quarantined`。
**这次验收未通过，不能称整个系统完成。**

后续通过当前清理证据及正式资源恢复 API，资源恢复 `available`；这不改变旧任务的失败。
再次准备执行时捕获另一 vLLM CI 容器占用设备，因此暂停 HCU 重跑，没有停止其他任务。
下一步在无冲突窗口，以修复后的 Worker 创建关联任务，沿用同一数据库／资源记录重新验收。

## 实际走通的内容

任务：`8b853469-df3b-4aba-8a30-cad83b2b349f`。
隔离 PostgreSQL schema：`bw20_api_3204bed89e0a4381a51367c97026deb3`。

1. 补齐完整 Python 源码／wheel／安装目录清单，并重新计算已知差异。
2. 将运行时证据、工作负载字节及 Target 指纹绑定到部署侧准入对象。
3. 真正向 `/v1/framework-smoke/tasks` 发起 HTTP 请求，返回201。
4. 原 Worker 使用 HTTP claim／heartbeat／complete，运行远程 source_prepare 和 noop_build。
5. 原 Coordinator 自动记录 Baseline、Candidate、Artifact，并产生 GPU Job。
6. 原 Worker 使用 PostgreSQL 租约、BW20 有界执行器、原 D 和清理器完成成对执行。
7. 原 D 检出输出一致但资源不健康；API Summary 返回 `rejected`。
8. 独立核对原始证据文件 Hash、D 结果与数据库记录一致；本次两个 GPU 容器均不存在。

两次输出均为 ` Paris. It is the largest city in`，prompt 5 tokens、completion 8 tokens。
D 指标：`baseline_execution_succeeded=true`、`noop_execution_succeeded=true`、
`output_equivalent=true`、`difference_count=0`、`cleanup_healthy=false`、`passed=false`。

## 环境与准入边界

- 主机 github-bw20／10.17.1.20；物理 HCU7／PCI0000:b1:00.0／gfx936；CPU64–79／NUMA4。
- 镜像固定 digest `a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c`。
- CPU 构建使用已验证的只读控制器清单 `61d3ea684367aafbff7788c3a679d1244b86049dd04fb42b4957331bc99e98ec`。
- Source Hash `215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b`。
- Artifact Hash `4051b175ef8da8401a70834a704a23898ddcd40bed78a5e7bef2ee659926129a`。
- 单次准入 Target 指纹 `1f0332262b3951a9ab5de5cc8782ce5570e75408dc0a5e1c901c176fc5a2477b`。

准入配置保存在本次 results 下，默认 Target 与默认 Catalog 未修改。三个 F1 准入项
依据运行时清单、冻结工作负载及真实组件接线证据记录解决，不接收 API 调用方的豁免列表。
native 编译来源和候选激活仍单列未验证项；时钟、独占窗口和 Stage0 闸门保持未通过。
Python绑定只证明“固定源码＋已知导入修复＋打包差异”，不是源码与安装目录逐字相同。
完整清单包含1974个源文件、1973个wheel文件及1973个安装文件，后两者 Hash 集合一致。

No-op tar 只是挂载，不安装、不激活。这是框架流程验收，不是候选优化成功。
本机 API 为 Python3.12，远程固定镜像为 Python3.10；不能声称全栈已在3.10验证。

## 失败、修复与恢复

### 环境干扰

首轮 No-op 清理检查发现 PID3975696、3976278，原始归属未及时捕获，不能事后臆测。
后续再次准备时，PID3988215 的 cgroup 明确属于容器
`ab657526a9379d9ef0fdfddd4a1a23ec4d4045ae9be8ddd1465471fbb204a09f`，
名称 `vllm_hcu_34349710198_1_contract-hcu-gfx936-p1of2`，映射 `/dev/kfd` 和整个 `/dev/dri`。
它不是本次管理的容器，没有被停止或修改。后续归属不能冒充首轮 PID 的归属证据。

资源恢复使用 `/v1/resources/{resource_id}/cleanup`，当前 fencing token为1，
原清理器提供 healthy=true、无本次残留容器、KFD为空的证据，返回200。
未直接修改数据库状态。之后占用又出现，未启动新 HCU 验收。

### Worker 完成与心跳竞态

实际日志出现：完成接口已提交任务终态但 Coordinator 尚未返回时，后台心跳收到409，
误触发失租清理。修复为清理结束后停止并等待后台心跳，再同步检查／续租，最后提交完成。
真正的失租仍拒绝完成并清理。

专项测试及相关 Worker 回归已通过；首轮进程在修改前启动，仍加载旧SDK，
因此**没有修复后 HCU 正向验收证据**。该竞态与另一进程占用是两件不同的问题。

### 复测终态限制

现有 `/retest` 只接收 awaiting_signoff，拒绝 rejected，返回409。
不放宽既有状态机、不把失败任务改成成功。操作脚本已支持在同一schema／资源记录下，
保留旧失败任务并建立关联的新任务；这个路径尚未实机完成。

## 证据位置

- 首轮：`results/bw20-api-acceptance-3204bed8-9e0a-4381-a513-67c97026deb3/`。
  `checkpoint-review.json` 为独立失败回读；包含 API／数据库／EvidenceBundle 的 Hash。
- API Summary SHA256：`8fc0476464feae343a9d4b059cd7a52fc62c5f9d3881ed3be3f7dc93a3d7475a`。
- EvidenceBundle SHA256：`064072836e7a60f577b5c65cc1ab0ce4f64005cf7c8001256f2b2e022b57313c`。
- 运行时绑定：`results/bw20-runtime-binding-54a0ce98-d0b3-42f1-9f77-c8734d847fd5/`；
  stdout SHA256 `729248c611aedc8d11ed56a4a265f6c7c36924f6581f9cbf4174496f9da22aaf`。
- 成功资源恢复、retest409：`results/bw20-api-acceptance-8ecd16cc-0d26-47d8-9718-c81d0d3f925c/`。
- 仅启动前检查失败：`results/bw20-api-acceptance-b69a1aee-40c6-43e9-9715-027e2a52a30b/`。

本地最终相关回归126passed／1skipped，Ruff通过，包含非对象证据拒绝与心跳竞态用例。
Windows首次CAS发布跳过不代替Linux验证。代码尚未提交／推送。
临时API、SSH隧道和本次GPU容器已结束；旧4191页面不是本次任务的结果页。

## 剩余验收项

- 无冲突设备窗口内，修复后的正常API链路到 awaiting_signoff。
- 新任务结果页接入和人工签核；不代签。
- BW20自己的Stage0、候选实际激活／恢复及优化证据；不继承nmz36结论。
- 把本轮操作脚本固化为可复用的部署入口，减少人工串接；当前仍是有界验收工具。

本轮适合报告“正常API与实机流程已执行、失败保护生效、完成竞态已修复”，
不适合报告“全系统验收完成”或“获得性能提升”。

# BW20 时钟恢复收口方案：仅接管已确认的默认 auto 基线

> 2026-09-14：本方案已被宿主`auto`只读验收策略取代。当前Stage0不写频率，因此无需
> 进入manual或执行恢复；本文仅作为未采用的设计记录保留。

状态：**用户已确认首版设计范围；明确暂不操作机器或修改频率。**
本轮仅本地代码与模拟验证，不修改现行完整策略 `ClockPolicy` 语义或 Stage0 冻结协议。

## 为什么提出这个范围

现有 ClockSession 要求捕获并恢复全部启用频率档位。BW20 已安装接口提供支持档位表与
瞬时当前档位，但未确认能读回启用 mask。二者不能互相冒充。

本机 `rocm_smi_v2.h` 的 `rsmi_dev_gpu_clk_freq_set` 注释明确说明：设置允许频率集合会
切入manual，返回AUTO可回默认状态。这支持提出更窄的方案，但头文件说明不是该驱动
实机执行和恢复通过的证据；不得直接转为正式 Backend。

## 已确认首版的边界

仅接管**独占窗口内，经独立确认的默认 auto 基线**：

1. 固定主机、物理PCI、render设备和NUMA；检查有效租约与独立的时钟控制授权。
2. 初始必须auto，OverDrive/boost为0，显存只存在1800MHz且DPM disabled；有未结束
   时钟日志、观察缺失、人工manual设置或未知历史策略则拒绝接管。
3. 从当期支持表定位1500MHz档位，不把“档位10”写成跨设备常量。
4. 仅设置核心频率；显存只观察、不写入，不调整风扇、功耗上限、OverDrive或系统库。
5. 结束后恢复已约定的默认auto；回读模式、支持档位表、固定显存及未触及设置。
   不要求瞬时核心频率回到采样时的600MHz，也不宣称恢复任意历史mask。
6. 失权后原执行者不再改频；保留日志及资源隔离状态。恢复接管必须有新的、真实的
   恢复权限及fencing校验，不能拿旧任务JSON或日志授权字符串直接写硬件。

**当前auto读数不能独立证明“已确认的默认基线”。** 基线确认、受控试验与恢复证据仍需
单独完成。如果无法保证这个前提，本方案同样不得执行。

## 对现有实现的影响

- 保留原 `ClockPolicy(mode, sclk_levels, mclk_levels)` 完整策略语义，不能用支持表填充。
- 已新增明确区分“默认auto恢复”和“完整策略恢复”的策略类与快照类型，共用原日志、
  租约和Worker；不能自动把任意ClockBackend当成已审核驱动。
- 实机写入仍须受控权限。当前github账号不能直接写sysfs；未尝试提权、改权限或部署helper。
- 不直接编辑Target blocker，也不改测量阈值。若需调整冻结协议/配置，走原版本化和重新
  绑定流程，不能沿用旧Hash作为新策略的凭据。
- 不需要为了验收首版支持所有驱动、任意manual历史策略、修改显存频率或断电自动恢复。
  但无法确认恢复时必须可靠隔离，且具备明确的人工恢复路径。

## 新增只读预检

`hcuopt.deployment.bw20_clock_preflight` 仅读取固定设备的sysfs及接口头文件Hash。
在配置好已审核控制端Python路径的BW20主机运行：

```bash
python -m hcuopt.deployment.bw20_clock_preflight
```

输出包括原始观测、支持档位、当前档位、显存DPM状态、候选范围匹配情况和缺项。
退出0仅代表当前观测匹配候选方案前提，**不是获准执行或通过Stage0**；
`execution_allowed / restoration_verified / enabled_mask_verified`始终为false。
非auto、OverDrive/boost不明、目标频率缺失、显存可变、设备不符或表格式歧义均拒绝。
检查是非原子的只读观测，不能取代实际写入前的重验、有效租约和排他控制。

当期实机只读记录：
`results/bw20-clock-preflight-5f75e522-88e8-4776-98bc-5de67cc0fb2e/report.json`，
SHA256 `319e21e4948cca8d85a13c73bfa3aa1da42182e812c4f19160b4b69be188189c`。
观测前提匹配、建议sclk档位10；没有执行改频、HCU工作负载或资源恢复。

## 本地实现：默认auto策略，不含真实驱动写入

`BW20AutoClockSession`复用原`BW20ClockSession`事务协调器；完整策略路径只抽取内部
策略方法，原`ClockPolicy`校验和恢复保证不变。新策略使用独立的`DefaultAutoBaseline`，
明确记录支持频率表而非启用mask，不把瞬时核心频率纳入auto恢复相等判断。

- 必须显式注入`AutoClockBackend`、原控制权回调和独立的默认基线确认回调。
  没有默认Backend，没有SSH/sysfs写入代码，没有注册到运行中的Worker。
- 初始auto观测不是默认基线确认。确认回调必须由部署方提供真实依据；用户本次确认的
  是设计范围，不是某台机器的基线证明或实机执行许可。
- 改频意图落盘后再次检查观测与控制权；频率档位由索引表查找，不依赖文本行顺序。
- 仅请求sclk档位，没有mclk、风扇、功耗、OverDrive写入接口。
- 回读manual状态和目标频率；正常/异常退出后恢复auto并回读。auto从600降到300MHz
  不构成失败。boot、PCI、接口头文件Hash、支持表、固定显存或零OD/boost改变则隔离。
- 接口头文件Hash只是额外身份锚点，不等于驱动二进制来源证明；真实部署还需原Target绑定。
- 失去控制权后不再调用恢复写入；复用原持久日志及Worker的不健康清理路径。
- 回执明确`restoration_scope=confirmed_default_auto_v1`，并保持
  `enabled_mask_verified=false / arbitrary_prior_policy_restored=false /
  hardware_restore_verified=false / stage0_accepted=false`。

后续仍需独立实现并审核受控驱动Backend、恢复接管以及实机验收；当前模拟测试不替代这些。
恢复接管的本地状态机和故障边界见[默认auto恢复接管契约](bw20-clock-recovery.md)；它没有
真实写频实现或调用入口，也不能把本节的设计确认升级为执行授权。

## 本轮验证及暂停范围

本地Python3.12回归：**106 passed / 1既有Starlette警告**，Ruff通过。
测试包括默认auto策略、原完整策略、持久日志、只读预检、Worker资源检查、活租约回调、
心跳完成顺序及部署入口。所有设备与控制面连接均为本地模拟；没有新的PostgreSQL或
Python3.10实机测试，也没有SSH、Docker、设备采集、时钟写入或远端部署。

测试明确覆盖正常/异常/KeyboardInterrupt退出、auto瞬时频率变化、非默认配置拒绝、
缺失独立基线确认、档位行乱序、部分写入失败、恢复回执不等于恢复成功、失租拒绝写入、
boot/PCI/接口Hash/档位表/显存/OD变化，以及原Worker的成功/失败/隔离上报。

当前代码未提交或推送。用户明确要求先不动机器；不能将设计确认解释成改频或测量窗口授权。

## 与Stage0适配器的当前接线

默认auto策略现在只能由部署方显式注入到Stage0组合入口；未绑定时
`clock_control_bound=false`，部署对象只允许只读检查，不能创建API或Worker。不存在
自动选用的no-op或真实驱动Backend。bootstrap只接受`BW20AutoClockSessionFactory`，
不再把任意callable视为已审核控制；工厂把过程内活租约、独立时钟权限、基线确认、
授权引用和持久日志组合在一起。Worker必须复用该工厂持有的同一个日志对象，不能换一份
新建空日志绕过未恢复记录。

`fingerprint`不进入时钟事务；四个计时探针在事务内运行，并保证本任务容器清理先于
恢复auto。恢复回执必须明确`restored=true / quarantined=false`，否则适配器证据强制
标为不健康，随后Worker再以持久日志做独立的释放前检查。该接线只通过本地替身验证，
不构成真实硬件恢复、时钟授权、Stage0通过或性能结论。

// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import { useRef, useState } from "react";
import { FORMAL_CANDIDATE_STATES, FORMAL_RECOVERY_STATES, loadFormalDispatch, loadFormalRecovery, loadFormalSubmission, submitFormalIntent } from "./formal-start.js";

const STATES = { awaiting_authority: "等待授权材料", ready_for_round_creation: "意图授权核对已完成（轮次进度见下方）", failed: "授权核对失败", cancelled: "意图已取消" };
const DISPATCH = { not_created: "尚未创建正式轮次", queued: "正式轮次已创建，待执行模块接入", cancelled: "正式轮次已取消", claimed: "控制面已领取（不代表实机已开始）", recovery_required: "领取已超时，需恢复核验；禁止自动重试", stop_requested: "已请求停止（尚未证明执行已停止或资源已释放）" };

export function FormalStartEntry() {
  const [token, setToken] = useState("");
  const [plan, setPlan] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dispatch, setDispatch] = useState(null);
  const [dispatchError, setDispatchError] = useState("");
  const [recovery, setRecovery] = useState(null);
  const [recoveryError, setRecoveryError] = useState("");
  const lock = useRef(false);
  async function operate(submit) {
    if (lock.current || (submit && !confirmed)) return;
    lock.current = true; setBusy(true); setError("");
    try {
      if (submit) setReceipt(await submitFormalIntent(plan, token));
      else setPlan(await loadFormalSubmission(token));
    } catch (failure) { setError(failure.message); }
    finally { if (submit) setToken(""); lock.current = false; setBusy(false); }
  }
  async function refreshDispatch() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setDispatchError(""); setDispatch(null);
    try { setDispatch(await loadFormalDispatch(plan, receipt, token)); }
    catch (failure) { setDispatchError(failure.message); }
    finally { setToken(""); lock.current = false; setBusy(false); }
  }
  async function refreshRecovery() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setRecovery(null); setRecoveryError("");
    try { setRecovery(await loadFormalRecovery(plan, receipt, token)); }
    catch (failure) { setRecoveryError(failure.message); }
    finally { setToken(""); lock.current = false; setBusy(false); }
  }
  return <main className="workspace-panel" style={{ maxWidth: 960, margin: "40px auto", padding: 24 }}>
    <a href="/">返回工作台</a>
    <h1>正式启动 · 意图受理</h1>
    <p>本入口只核对冻结计划和授权，不创建执行轮次、不访问 HCU、不自动发布。</p>
    <section className="wizard-card full-width" style={{ padding: 24 }}>
      <h2>1. 读取部署方登记的计划</h2>
      <p>输入独立操作凭据，不是模型 API Key。凭据仅保留在当前页面内存，提交后清空。</p>
      <label className="plan-field">独立操作凭据
        <input type="password" autoComplete="off" value={token} disabled={busy}
          onChange={(event) => setToken(event.target.value)} />
      </label>
      {!plan && <button className="primary-button" disabled={busy || !token} onClick={() => operate(false)}>读取冻结计划</button>}
      {plan && <>
        <h2>2. 核对请求</h2>
        <p>以下是不可编辑的部署引用；读取成功不代表授权闸门已通过。</p>
        <dl>{[["计划预览", plan.preview_id], ["请求编号（重试不变）", plan.idempotency_key],
          ["计划 Hash", plan.resolved_plan_hash], ["窗口授权 Hash", plan.formal_authorization_hash], ["执行授权 Hash", plan.execution_authority_hash],
          ["评测授权 Hash", plan.evaluation_authority_hash], ["服务实例", plan.expected_service_identity.server_instance_id]]
          .map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="mono" style={{ overflowWrap: "anywhere" }}>{value}</dd></div>)}</dl>
        <label className="plan-field"><span><input type="checkbox" checked={confirmed} disabled={busy || !!receipt}
          onChange={(event) => setConfirmed(event.target.checked)} /> 我确认仅提交启动意图，此操作不会启动 HCU</span></label>
        <button className="primary-button" disabled={busy || !confirmed || !token || !!receipt} onClick={() => operate(true)}>
          {busy ? "正在处理…" : "提交原请求 / 安全重试"}</button>
        <p>失败后重新输入同一凭据，重试仍使用上述请求。刷新后须用同一凭据重新读取；过期时请交由管理面核对，勿另建计划。</p>
      </>}
      {error && <p role="alert">{error}</p>}
      {receipt && <section role="status"><h2>3. 服务端回执</h2>
        <p>{STATES[receipt.state]}</p><p className="mono">意图：{receipt.intent_id}</p>
        <p>意图回执不代表实时执行进度，也不是正确性、性能或签核结论。</p>
        {receipt.blocker_codes?.map((code) => <p className="mono" key={code}>{code}</p>)}
        <h2>4. 查询轮次进度（只读）</h2>
        <p>重新输入同一操作凭据后查询。查询不会创建或启动任务；未配置读取接口时保留“未知”，不推断已运行。</p>
        <button className="primary-button" disabled={busy || !token} onClick={refreshDispatch}>查询持久化派发状态</button>
        {dispatch && <><p>{DISPATCH[dispatch.state]}</p><p className="mono">预定轮次：{dispatch.round_id}</p>
          <p>当前部署尚未接入自动执行消费者；下方是已持久化的候选事实，不表示 HCU 已运行或性能验证已通过。</p>
          <h3>候选进度</h3>
          {!dispatch.candidates?.length && <p>暂无候选明细，不能据此判断已完成。</p>}
          {dispatch.candidates?.map((candidate) => <section key={candidate.candidate_id} className="wizard-card" style={{ padding: 16, marginTop: 12 }}>
            <p className="mono" style={{ overflowWrap: "anywhere" }}>候选：{candidate.candidate_id}</p>
            <p>{FORMAL_CANDIDATE_STATES[candidate.state]}</p>
            {candidate.artifact_id && <>
              <p className="mono" style={{ overflowWrap: "anywhere" }}>制品：{candidate.artifact_id}</p>
              <p className="mono" style={{ overflowWrap: "anywhere" }}>{candidate.artifact_hash}</p>
            </>}
          </section>)}</>}
        {dispatchError && <p role="alert">轮次进度未知：{dispatchError} 原意图回执不受影响。</p>}
        <h2>5. 正确性执行核查（只读）</h2>
        <p>查询部署方绑定的执行任务，不会重新执行、补账或释放资源。需要重新输入同一操作凭据。</p>
        <button className="primary-button" disabled={busy || !token} onClick={refreshRecovery}>查看执行核查</button>
        {recovery && <section className="wizard-card" style={{ padding: 16, marginTop: 12 }}>
          <h3>{FORMAL_RECOVERY_STATES[recovery.report.status]}</h3>
          <p className="mono" style={{ overflowWrap: "anywhere" }}>任务：{recovery.report.job_id}</p>
          <p>资源：{recovery.report.resource_id} / {recovery.report.resource_state}</p>
          <p>本次任务仍持有资源记录：{recovery.report.resource_owned_by_attempt ? "是" : "否（不代表物理空闲）"}</p>
          <p>预算状态：{recovery.report.budget_state}；结果已留存：{recovery.report.recorded_result ? "是" : "否"}；释放记录：{recovery.report.release_recorded ? "有" : "无"}</p>
          <p>{recovery.report.reconciliation_allowed ? "可由可信部署入口核对后收尾；本页不提供执行权限。" : "禁止补账放行和自动重试，请先完成核查。"}</p>
          {!!recovery.report.required_manual_checks.length && <p>需确认原执行器已被阻止继续启动，核查本任务容器与进程，留存新鲜清理和健康证据，并核算实际用量。</p>}
          <p className="mono" style={{ overflowWrap: "anywhere" }}>核查快照：{recovery.report.snapshot_hash}</p>
          <p>这是控制面记录，不是实机空闲证明或性能提升结论。</p>
        </section>}
        {recoveryError && <p role="alert">执行核查未知：{recoveryError} 不会转用演练数据。</p>}
      </section>}
    </section>
  </main>;
}

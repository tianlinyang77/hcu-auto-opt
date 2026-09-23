// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useEffect, useMemo, useState } from "react";
import { adjudicationResult, agentOrigin, credibleThresholdStatus, formatEvidencePercent, freezeManualSignoff, loadManualCandidateSummary, measurementFacts, signoffMatches, submitManualSignoff } from "./manual-candidate.js";
import "./framework-smoke.css";

const stateLabel = (value) => ({ awaiting_signoff: "等待人工签核", completed: "已批准", rejected: "已拒绝", accepted: "已接受" }[value] || value);

function identityRows(summary, result) {
  return [
    ["Task", summary.task.task_id], ["Baseline Epoch", summary.baseline.baseline_epoch_id],
    ["Hotspot", summary.hotspots[0]?.hotspot_id], ["Candidate", summary.candidate.candidate_id],
    ["Artifact", summary.artifacts[0]?.artifact_id], ["Measurement", result.evaluation.measurement.measurement_id],
    ["EvaluationRun", result.evaluation.evaluation_run_id], ["EvidenceBundle", result.evidence.evidence_id],
  ];
}

function Signoff({ summary, onComplete }) {
  const [decision, setDecision] = useState("approved");
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("接受该冻结 M1 EvidenceBundle 作为项目证据；不授权自动发布或生产发布。");
  const [intent, setIntent] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (summary.signoff) return <div className="smoke-signoff" role="status">
    <h3>人工签核已写入控制面</h3>
    <p>{summary.signoff.decision === "approved" ? "已批准" : "已拒绝"} · {summary.signoff.actor}</p>
    <p>{summary.signoff.reason}</p>
    <p className="smoke-id">签核 {summary.signoff.signoff_id}</p>
    <p>自动发布仍为 false；本决定不等于生产发布。</p>
  </div>;

  return <div className="smoke-signoff">
    <h3>人工签核 · 冻结 M1 证据</h3>
    {error && <p className="smoke-error" role="alert">{error}</p>}
    {!intent ? <form onSubmit={(event) => {
      event.preventDefault(); setError("");
      try {
        const key = `m1-${summary.task.task_id.slice(0, 8)}-${decision}-${crypto.randomUUID()}`;
        setIntent(freezeManualSignoff(summary, { decision, actor, reason }, key));
      } catch (failure) { setError(failure.message); }
    }}>
      <label htmlFor="m1-actor">签署人</label>
      <input id="m1-actor" required maxLength={200} value={actor} onChange={(event) => setActor(event.target.value)} />
      <label htmlFor="m1-decision">决定</label>
      <select id="m1-decision" value={decision} onChange={(event) => setDecision(event.target.value)}>
        <option value="approved">接受冻结证据</option><option value="rejected">拒绝冻结证据</option>
      </select>
      <label htmlFor="m1-reason">签核理由</label>
      <textarea id="m1-reason" required maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} />
      <button disabled={!actor.trim() || !reason.trim()}>核对本次签核</button>
    </form> : <div>
      <dl><dt>Task</dt><dd className="smoke-id">{intent.taskId}</dd>
        <dt>Candidate</dt><dd className="smoke-id">{intent.candidateId}</dd>
        <dt>冻结 EvidenceBundle</dt><dd className="smoke-id">{intent.payload.evidence_bundle_id}</dd>
        <dt>签署人 / 决定</dt><dd>{intent.payload.actor} / {intent.payload.decision}</dd>
        <dt>理由</dt><dd>{intent.payload.reason}</dd>
        <dt>幂等请求</dt><dd className="smoke-id">{intent.payload.idempotency_key}</dd></dl>
      <p><strong>边界：</strong>只接受这份冻结证据；不授权自动发布、生产发布，也不声明 SGLang 端到端提升。</p>
      <button disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try {
          const receipt = await submitManualSignoff(intent);
          if (!signoffMatches(intent, receipt)) throw new Error("数据库回执与冻结签核请求不一致");
          await onComplete();
        } catch (failure) { setError(failure.message); } finally { setBusy(false); }
      }}>{busy ? "正在写入…" : "我已核对，确认提交"}</button>
      <button disabled={busy} onClick={() => setIntent(null)}>返回修改</button>
    </div>}
  </div>;
}

export function ManualCandidateInspection({ taskId }) {
  const [summary, setSummary] = useState(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  async function refresh() {
    setBusy(true); setError("");
    try { setSummary(await loadManualCandidateSummary(taskId)); }
    catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    let ignored = false;
    loadManualCandidateSummary(taskId).then((value) => {
      if (!ignored) setSummary(value);
    }).catch((failure) => {
      if (!ignored) setError(failure.message);
    }).finally(() => {
      if (!ignored) setBusy(false);
    });
    return () => { ignored = true; };
  }, [taskId]);
  const result = useMemo(() => {
    if (!summary) return null;
    try { return adjudicationResult(summary); } catch { return null; }
  }, [summary]);
  const origin = useMemo(() => agentOrigin(summary), [summary]);

  return <main className="smoke-inspection m1-inspection">
    <header><div><p className="smoke-eyebrow">HCU AUTO OPT · BW20 M1</p>
      <h1>单候选 M1 正式验收</h1><p className="smoke-id">{taskId}</p></div>
      <button disabled={busy} onClick={refresh}>{busy ? "正在刷新…" : "刷新控制面结果"}</button></header>
    <p className="smoke-boundary">本页展示冻结 allocator 微基准的正确性和性能裁决。它不是 SGLang 端点加速结论，也不允许自动发布。</p>
    {error && <p className="smoke-error" role="alert">{error}</p>}
    {!summary || !result ? <p>{busy ? "正在读取正式证据…" : "尚未取得可签核的正式证据。"}</p> : <>
      <section className="smoke-verdict"><div><p>Task / Candidate</p><h2>{stateLabel(summary.task.state)} / {stateLabel(summary.candidate.state)}</h2></div>
        <p>D 独立裁决：<strong>{result.evaluation.metrics.verdict}</strong>；自动发布：<strong>false</strong></p></section>
      <section className="m1-metrics" aria-label="正式评测指标">
        <article><small>正确性判决</small><strong>{result.evaluation.metrics.correctness_verdict || "未提供"}</strong><span>以 D 判决证据为准</span></article>
        <article><small>微基准效果比</small><strong>{formatEvidencePercent(result.evaluation.metrics.effect_ratio)}</strong><span>冻结 allocator 微基准</span></article>
        <article><small>95% 置信区间</small><strong>{Array.isArray(result.evaluation.metrics.confidence_interval)
          ? `${formatEvidencePercent(result.evaluation.metrics.confidence_interval[0])} – ${formatEvidencePercent(result.evaluation.metrics.confidence_interval[1])}`
          : "未提供"}</strong><span>{credibleThresholdStatus(result.evaluation.metrics)}</span></article>
        <article><small>工作负载 MDE</small><strong>{formatEvidencePercent(result.evaluation.metrics.workload_mde_ratio)}</strong><span>最小可检测效果</span></article>
      </section>
      <div className="smoke-columns"><section className="smoke-panel"><h2>候选改动摘要</h2>
        <p>{summary.candidate.optimization_intent || "未提供优化意图"}</p>
        <p className="smoke-boundary">当前 M1 摘要接口没有返回候选源码 diff；此处不把优化意图或其他案例的代码当作本候选改动。</p>
        <dl><dt>替换点</dt><dd className="smoke-id">{summary.candidate.replacement_point}</dd>
          <dt>源码 Hash</dt><dd className="smoke-id">{summary.candidate.source_hash}</dd>
          <dt>Artifact Hash</dt><dd className="smoke-id">{summary.artifacts[0]?.content_hash}</dd></dl>
      </section><section className="smoke-panel"><h2>证据身份链</h2><dl>{identityRows(summary, result).map(([label, value]) =>
        <div key={label} className="m1-id-row"><dt>{label}</dt><dd className="smoke-id">{value || "—"}</dd></div>)}</dl></section></div>
      {origin && <section className="smoke-panel"><h2>Agent 来源与审核链</h2>
        {!origin.valid && <p className="smoke-error" role="alert">{origin.reason}；BW20 M1 不允许批准。</p>}
        {origin.valid && <dl>
          <div className="m1-id-row"><dt>Generation Run</dt><dd className="smoke-id">{origin.generation_run_id}</dd></div>
          <div className="m1-id-row"><dt>Proposal / Review</dt><dd className="smoke-id">{origin.proposal_id} / {origin.review_id}</dd></div>
          <div className="m1-id-row"><dt>Review Record Hash</dt><dd className="smoke-id">{origin.review_record_hash}</dd></div>
          <div className="m1-id-row"><dt>Proposal Review Evidence</dt><dd className="smoke-id">{origin.proposal_review_evidence_hash}</dd></div>
          <div className="m1-id-row"><dt>Patch Hash</dt><dd className="smoke-id">{origin.patch_hash}</dd></div>
          <div className="m1-id-row"><dt>Source Package / Manifest</dt><dd className="smoke-id">{origin.source_package_hash} / {origin.manifest_hash}</dd></div>
          <div className="m1-id-row"><dt>Candidate Source Hash</dt><dd className="smoke-id">{origin.candidate_source_hash}</dd></div>
          <div className="m1-id-row"><dt>Generation Review Evidence</dt><dd className="smoke-id">{origin.generation_review_evidence_hash}</dd></div>
        </dl>}
      </section>}
      <section className="smoke-panel"><h2>测量证据</h2>
        <dl>{measurementFacts(result).map(([label, value]) => <div key={label} className="m1-id-row">
          <dt>{label}</dt><dd className="smoke-id">{value ?? "未提供"}</dd></div>)}</dl>
        <p className="smoke-id">原始样本 SHA256 {result.evaluation.measurement.raw_samples_hash || "未提供"}</p>
        <p>结论只适用于冻结的 allocator 微基准；BW20 上的端点占比尚未测量。</p></section>
      <section className="smoke-panel"><h2>人工决定</h2><Signoff summary={summary} onComplete={refresh} /></section>
      <footer>实时读取 BW20 控制面 · synthetic=false · automatic_release_allowed=false</footer>
    </>}
  </main>;
}

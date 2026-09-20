// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useEffect, useState } from "react";
import {
  endpointCampaignSignoffMatches,
  freezeEndpointCampaignSignoff,
  loadEndpointCampaignSummary,
  submitEndpointCampaignSignoff,
  validateEndpointCampaignSummary,
} from "./endpoint-campaign.js";
import "./framework-smoke.css";

const stateLabels = {
  awaiting_adjudication: "等待 D 裁决",
  adjudicating: "D 正在重读证据",
  adjudication_failed: "D Worker 执行失败",
  awaiting_signoff: "等待人工签核",
  completed: "已接受",
  rejected: "已拒绝",
  invalid: "证据无效",
};
const verdictLabels = {
  faster: "候选更快",
  slower: "候选更慢",
  inconclusive: "证据不足，无法判定",
  invalid: "证据无效",
};
const milliseconds = (value) => value == null ? "—" : `${(Number(value) / 1e6).toFixed(3)} ms`;
const percent = (value) => value == null ? "—" : `${Number(value).toFixed(4)}%`;

function CampaignSignoff({ summary, onComplete }) {
  const [decision, setDecision] = useState("accepted");
  const [actor, setActor] = useState("");
  const [credential, setCredential] = useState("");
  const [reason, setReason] = useState("接受本次正式 D 裁决作为项目证据；不授权自动发布或生产发布。");
  const [intent, setIntent] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (summary.signoff) return <div className="smoke-signoff" role="status">
    <h3>人工签核已写入控制面</h3>
    <p>{summary.signoff.decision === "accepted" ? "已接受" : "已拒绝"} · {summary.signoff.actor}</p>
    <p>{summary.signoff.reason}</p>
    <p className="smoke-id">签核 {summary.signoff.signoff_id}</p>
    <p>自动发布仍为 false；本决定不等于生产发布。</p>
  </div>;
  if (summary.campaign.state !== "awaiting_signoff") return <p>
    当前状态不允许人工签核。只有正式 D 形成有效 verdict 后才能签署。
  </p>;
  return <div className="smoke-signoff">
    <h3>人工签核 · 绑定当前 D Result Hash</h3>
    {error && <p className="smoke-error" role="alert">{error}</p>}
    {!intent ? <form onSubmit={(event) => {
      event.preventDefault(); setError("");
      try {
        setIntent(freezeEndpointCampaignSignoff(
          summary,
          { decision, actor, reason },
          `endpoint-${summary.campaign.campaign_id.slice(0, 8)}-${crypto.randomUUID()}`,
        ));
      } catch (failure) { setError(failure.message); }
    }}>
      <label htmlFor="endpoint-actor">签署人</label>
      <input id="endpoint-actor" required maxLength={200} value={actor} onChange={(event) => setActor(event.target.value)} />
      <label htmlFor="endpoint-credential">独立签核凭据</label>
      <input id="endpoint-credential" type="password" autoComplete="off" required
        minLength={32} maxLength={256} value={credential}
        onChange={(event) => setCredential(event.target.value)} />
      <p>凭据只保留在当前页面内存中，不是模型 API Key。</p>
      <label htmlFor="endpoint-decision">决定</label>
      <select id="endpoint-decision" value={decision} onChange={(event) => setDecision(event.target.value)}>
        <option value="accepted">接受本次裁决</option><option value="rejected">拒绝本次裁决</option>
      </select>
      <label htmlFor="endpoint-reason">签核理由</label>
      <textarea id="endpoint-reason" required maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} />
      <button disabled={!actor.trim() || !reason.trim() || credential.length < 32}>核对本次签核</button>
    </form> : <div>
      <dl><dt>Campaign</dt><dd className="smoke-id">{intent.campaignId}</dd>
        <dt>D verdict</dt><dd>{verdictLabels[intent.verdict] || intent.verdict}</dd>
        <dt>Result Hash</dt><dd className="smoke-id">{intent.payload.adjudication_result_sha256}</dd>
        <dt>签署人 / 决定</dt><dd>{intent.payload.actor} / {intent.payload.decision}</dd>
        <dt>理由</dt><dd>{intent.payload.reason}</dd></dl>
      <p><strong>边界：</strong>只接受或拒绝这次冻结裁决；不授权自动发布或生产灰度。</p>
      <button disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try {
          const receipt = await submitEndpointCampaignSignoff(intent, credential);
          if (!endpointCampaignSignoffMatches(intent, receipt)) {
            throw new Error("数据库回执与冻结签核请求不一致");
          }
          setCredential("");
          await onComplete();
        } catch (failure) { setError(failure.message); } finally { setBusy(false); }
      }}>{busy ? "正在写入…" : "我已核对，确认提交"}</button>
      <button disabled={busy} onClick={() => setIntent(null)}>返回修改</button>
    </div>}
  </div>;
}

export function EndpointCampaignInspection({ campaignId }) {
  const [summary, setSummary] = useState(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  async function refresh() {
    setBusy(true); setError("");
    try {
      setSummary(validateEndpointCampaignSummary(
        await loadEndpointCampaignSummary(campaignId), campaignId,
      ));
    } catch (failure) { setError(failure.message); } finally { setBusy(false); }
  }
  useEffect(() => {
    let ignored = false;
    loadEndpointCampaignSummary(campaignId).then((value) => {
      if (!ignored) setSummary(validateEndpointCampaignSummary(value, campaignId));
    }).catch((failure) => { if (!ignored) setError(failure.message); })
      .finally(() => { if (!ignored) setBusy(false); });
    return () => { ignored = true; };
  }, [campaignId]);
  const campaign = summary?.campaign;
  const result = campaign?.adjudication_result;
  const groups = campaign?.adjudication_request?.groups || [];
  const ci = result?.confidence_interval_percent;
  return <main className="smoke-inspection endpoint-inspection">
    <header><div><p className="smoke-eyebrow">HCU AUTO OPT · ENDPOINT CAMPAIGN</p>
      <h1>服务级正式裁决</h1><p className="smoke-id">{campaignId}</p></div>
      <button disabled={busy} onClick={refresh}>{busy ? "正在刷新…" : "刷新控制面结果"}</button></header>
    <p className="smoke-boundary">本页展示八组 B-C-C-B 的服务级正式 D 裁决。它不把请求数冒充独立样本，也不允许自动发布。</p>
    {error && <p className="smoke-error" role="alert">{error}</p>}
    {!campaign ? <p>{busy ? "正在读取 Campaign…" : "尚未取得 Campaign 权威数据。"}</p> : <>
      <section className="smoke-verdict"><div><p>Campaign 状态</p>
        <h2>{stateLabels[campaign.state] || campaign.state}</h2></div>
        <p>D 正式裁决：<strong>{result ? verdictLabels[result.verdict] || result.verdict : "尚未形成"}</strong><br />
          自动发布：<strong>false</strong></p></section>
      <section className="m1-metrics" aria-label="服务级评测指标">
        <article><small>Baseline 平均延迟</small><strong>{milliseconds(result?.baseline_mean_ns)}</strong><span>八个 ABBA 组</span></article>
        <article><small>Candidate 平均延迟</small><strong>{milliseconds(result?.candidate_mean_ns)}</strong><span>独立服务进程与缓存</span></article>
        <article><small>配对延迟下降</small><strong>{percent(result?.paired_latency_reduction_percent)}</strong><span>正值才表示候选更快</span></article>
        <article><small>95% 置信区间</small><strong>{ci ? `${percent(ci[0])} – ${percent(ci[1])}` : "—"}</strong><span>跨 0 时结论为 inconclusive</span></article>
      </section>
      <div className="smoke-columns"><section className="smoke-panel"><h2>证据与协议</h2><dl>
        <dt>正式 D</dt><dd>{summary.formal_d_adjudication ? "true" : "false"}</dd>
        <dt>ABBA 组</dt><dd>{groups.length} / 8</dd>
        <dt>Acquisitions</dt><dd>{groups.reduce((total, group) => total + group.acquisitions.length, 0)} / 32</dd>
        <dt>Measured requests</dt><dd>{result?.measured_requests ?? "—"}</dd>
        <dt>验证文件</dt><dd>{result?.verified_file_count ?? "—"}</dd>
        <dt>Manifest Hash</dt><dd className="smoke-id">{result?.manifest_sha256 || campaign.adjudication_request.raw_evidence_manifest_sha256}</dd>
        <dt>D Result Hash</dt><dd className="smoke-id">{summary.adjudication_result_sha256 || "—"}</dd>
      </dl></section><section className="smoke-panel"><h2>冻结身份</h2><dl>
        <dt>Signed M1 Task</dt><dd className="smoke-id">{campaign.signed_m1_task_id}</dd>
        <dt>Target Snapshot</dt><dd className="smoke-id">{campaign.target_snapshot_id}</dd>
        <dt>Environment</dt><dd className="smoke-id">{campaign.environment_fingerprint}</dd>
        <dt>Execution Adapter</dt><dd className="smoke-id">{campaign.adapter_profile}</dd>
        <dt>D Job</dt><dd className="smoke-id">{campaign.adjudication_job_id}</dd>
        <dt>D Job state</dt><dd>{summary.adjudication_job?.state || "—"}</dd>
      </dl></section></div>
      <section className="smoke-panel"><h2>八组运行状态</h2>
        <div className="endpoint-groups">{summary.endpoint_runs.map((run, index) => <article key={run.endpoint_run_id}>
          <small>GROUP {String(index + 1).padStart(2, "0")}</small><strong>{run.state}</strong>
          <span className="smoke-id">{run.endpoint_run_id}</span>
        </article>)}</div></section>
      <section className="smoke-panel"><h2>人工决定</h2>
        <CampaignSignoff summary={summary} onComplete={refresh} /></section>
      <footer>实时读取 Campaign 控制面 · formal_d_adjudication={String(summary.formal_d_adjudication)} · automatic_release_allowed=false</footer>
    </>}
  </main>;
}

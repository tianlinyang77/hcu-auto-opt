import { useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  Check,
  CheckCircle,
  CircleNotch,
  ClockCounterClockwise,
  DownloadSimple,
  Eye,
  FileLock,
  Fingerprint,
  Hash,
  Info,
  LockKey,
  Package,
  ShieldCheck,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

import { loadOperatorStartIntent } from "./api.js";
import { loadDemoStartIntent } from "./demo-data.js";
import {
  deriveStartAuditSteps,
  summarizeStartIntent,
} from "./start-intent.js";

function shortValue(value, length = 12) {
  if (!value) return "—";
  return String(value).replace("sha256:", "").slice(0, length);
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function downloadIntent(intent) {
  const blob = new Blob([`${JSON.stringify(intent, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `operator-start-intent-${shortValue(intent.intent_id, 8)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function AuditRail({ intent }) {
  const steps = deriveStartAuditSteps(intent);
  return (
    <ol className="start-audit-rail" aria-label="durable Start 审计步骤">
      {steps.map((step) => (
        <li className={step.status} key={step.key}>
          <span className="start-audit-node">
            {step.status === "complete" ? (
              <Check size={17} weight="bold" />
            ) : step.status === "failed" ? (
              <WarningCircle size={18} weight="fill" />
            ) : (
              <LockKey size={17} />
            )}
          </span>
          <div>
            <small>{step.eyebrow}</small>
            <strong>{step.label}</strong>
          </div>
        </li>
      ))}
    </ol>
  );
}

function Fact({ label, value, mono = true, title }) {
  return (
    <div className="start-fact">
      <dt>{label}</dt>
      <dd className={mono ? "mono" : ""} title={title || value}>{value || "—"}</dd>
    </div>
  );
}

function CandidateMember({ member }) {
  const bound = member.state === "round_member_bound";
  return (
    <article className={`start-member ${bound ? "bound" : member.state}`}>
      <div className="start-member-head">
        <span className="start-member-ordinal mono">M2-{String(member.ordinal + 1).padStart(2, "0")}</span>
        <span className={`start-member-state ${bound ? "bound" : "pending"}`}>
          {bound ? <CheckCircle size={16} weight="fill" /> : <WarningCircle size={16} />}
          {bound ? "已绑定 Round" : member.state}
        </span>
      </div>
      <strong>{member.optimization_intent}</strong>
      <span className="start-member-path">{member.replacement_point}</span>
      <dl className="start-member-facts">
        <Fact label="Candidate" value={shortValue(member.candidate_id)} title={member.candidate_id} />
        <Fact label="Round Member" value={shortValue(member.round_candidate_id)} title={member.round_candidate_id} />
        <Fact label="Package" value={shortValue(member.source_package_ref.source_package_hash)} title={member.source_package_ref.source_package_hash} />
        <Fact label="Input Digest" value={shortValue(member.candidate_input_digest)} title={member.candidate_input_digest} />
      </dl>
    </article>
  );
}

function LoadingState() {
  return (
    <section className="start-audit-empty">
      <CircleNotch className="spin" size={34} />
      <h2>正在读取 durable Start…</h2>
      <p>页面只等待权威 GET 响应，不推演中间状态。</p>
    </section>
  );
}

function ErrorState({ error, onRetry }) {
  return (
    <section className="start-audit-empty danger">
      <WarningCircle size={40} />
      <span>Operator API error</span>
      <h2>启动审计暂不可用</h2>
      <p>{error}</p>
      <button className="primary-button" type="button" onClick={onRetry}>
        <ArrowClockwise size={18} />重新读取
      </button>
    </section>
  );
}

export function StartIntentWorkspace({ round, demoMode, onClose }) {
  const [intent, setIntent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const intentId = round?.intent_id;

  useEffect(() => {
    document.body.classList.add("workspace-open");
    return () => document.body.classList.remove("workspace-open");
  }, []);

  const load = async () => {
    if (!intentId) {
      setIntent(null);
      setLoading(false);
      setError("missing_intent_id: 当前 Round 没有权威 intent_id");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const value = demoMode
        ? await loadDemoStartIntent(intentId)
        : await loadOperatorStartIntent(intentId);
      setIntent(value);
    } catch (loadError) {
      setIntent(null);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    const loader = demoMode
      ? loadDemoStartIntent(intentId)
      : loadOperatorStartIntent(intentId);
    loader
      .then((value) => {
        if (active) setIntent(value);
      })
      .catch((loadError) => {
        if (!active) return;
        setIntent(null);
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [demoMode, intentId]);

  const summary = useMemo(
    () => (intent ? summarizeStartIntent(intent) : null),
    [intent],
  );

  return (
    <div className="plan-workspace-backdrop start-workspace-backdrop" role="presentation">
      <section className="plan-workspace start-workspace" role="dialog" aria-modal="true" aria-labelledby="start-audit-title">
        <header className="plan-workspace-header">
          <div className="plan-workspace-title">
            <span className="plan-icon"><ClockCounterClockwise size={23} /></span>
            <div>
              <span>UI-2 · READ-ONLY START AUDIT</span>
              <h1 id="start-audit-title">durable Start 启动审计</h1>
            </div>
          </div>
          <div className="plan-workspace-meta">
            <span className="synthetic-badge">Synthetic</span>
            <span className="read-only-tag"><Eye size={15} />只读</span>
            <button className="icon-button" type="button" aria-label="关闭启动审计" title="关闭启动审计" onClick={onClose}>
              <X size={20} />
            </button>
          </div>
        </header>

        {intent ? <AuditRail intent={intent} /> : <div className="start-audit-rail-placeholder" />}

        <div className="plan-workspace-body start-workspace-body">
          {loading ? (
            <LoadingState />
          ) : error ? (
            <ErrorState error={error} onRetry={load} />
          ) : intent && summary ? (
            <div className="start-audit-content">
              <section className={`start-status-card ${summary.failed ? "failed" : "finalized"}`}>
                {summary.failed ? <WarningCircle size={33} /> : <ShieldCheck size={33} weight="fill" />}
                <div>
                  <span>后端状态 · {intent.state}</span>
                  <h2>{summary.label}</h2>
                  <p>{summary.detail}</p>
                </div>
                <dl>
                  <div><dt>候选绑定</dt><dd>{summary.boundCount} / {summary.memberCount}</dd></div>
                  <div><dt>Intent 版本</dt><dd>v{intent.version}</dd></div>
                  <div><dt>耗时</dt><dd>{Math.max(0, (new Date(intent.updated_at) - new Date(intent.created_at)) / 1000).toFixed(1)}s</dd></div>
                </dl>
              </section>

              {summary.failed && (
                <section className="start-error-card">
                  <WarningCircle size={23} />
                  <div><span className="mono">{intent.error_code}</span><strong>{intent.error_message}</strong></div>
                  <small>这里只显示后端保存的安全错误，不猜测恢复结论。</small>
                </section>
              )}

              <div className="start-audit-grid">
                <section className="wizard-card start-authority-card">
                  <div className="wizard-card-head compact">
                    <div><span>不可变引用</span><h2>Start 与 Plan Authority</h2></div>
                    <FileLock size={23} />
                  </div>
                  <div className="start-authority-section">
                    <h3><Fingerprint size={17} />启动身份</h3>
                    <dl className="start-fact-grid">
                      <Fact label="Intent ID" value={shortValue(intent.intent_id)} title={intent.intent_id} />
                      <Fact label="Preview ID" value={shortValue(intent.preview_id)} title={intent.preview_id} />
                      <Fact label="Task ID" value={shortValue(intent.task_id)} title={intent.task_id} />
                      <Fact label="Round ID" value={shortValue(intent.round_id)} title={intent.round_id} />
                      <Fact label="Actor" value={intent.actor} mono={false} />
                      <Fact label="Idempotency" value={intent.idempotency_key} />
                      <Fact label="Created" value={formatDateTime(intent.created_at)} />
                      <Fact label="Finalized" value={formatDateTime(intent.finalized_at)} />
                    </dl>
                  </div>
                  <div className="start-authority-section">
                    <h3><Hash size={17} />冻结承诺</h3>
                    <dl className="start-fact-grid">
                      <Fact label="Resolved Plan" value={shortValue(intent.resolved_plan_hash)} title={intent.resolved_plan_hash} />
                      <Fact label="Request Digest" value={shortValue(intent.request_digest)} title={intent.request_digest} />
                      <Fact label="Search Plan" value={shortValue(intent.search_plan_hash)} title={intent.search_plan_hash} />
                      <Fact label="Holdout Commitment" value={shortValue(intent.holdout_plan_commitment)} title={intent.holdout_plan_commitment} />
                      <Fact label="Holdout Authority" value={intent.holdout_plan_authority_id} />
                      <Fact label="Holdout Authority Hash" value={shortValue(intent.holdout_plan_authority_hash)} title={intent.holdout_plan_authority_hash} />
                      <Fact label="Candidate Family" value={shortValue(intent.candidate_family_hash)} title={intent.candidate_family_hash} />
                      <Fact label="FWER family alpha" value={intent.family_alpha?.toFixed(2)} />
                      <Fact label="Commitment Scheme" value={intent.holdout_commitment_scheme} />
                    </dl>
                  </div>
                  <div className="start-authority-section service-identity-strip">
                    <ShieldCheck size={20} />
                    <div><span>服务身份</span><strong className="mono">commit {shortValue(intent.service_identity.source_commit, 9)}</strong></div>
                    <small className="mono">{shortValue(intent.service_identity.profile_catalog_hash)}</small>
                  </div>
                </section>

                <section className="wizard-card start-members-card">
                  <div className="wizard-card-head compact">
                    <div><span>Candidate binding</span><h2>本轮候选成员</h2></div>
                    <Package size={23} />
                  </div>
                  <p className="wizard-card-intro">
                    每个成员同时保留输入摘要、Package、Candidate 与 Round Member 的绑定关系。
                  </p>
                  <div className="start-members-list">
                    {intent.candidate_members.map((member) => <CandidateMember member={member} key={member.round_candidate_id} />)}
                  </div>
                </section>
              </div>

              <section className="start-boundary-card">
                <LockKey size={22} />
                <div>
                  <strong>审计可见，不等于开放执行</strong>
                  <p>UI-2 不调用 Start、Reconcile、Cancel 或 Signoff；不接入真实 HCU，也不产生性能与发布结论。</p>
                </div>
                <span className="mono">automatic_release_allowed=false</span>
              </section>
            </div>
          ) : null}
        </div>

        <footer className="plan-workspace-footer">
          <span><Info size={17} />Authority 来自 GET /v1/operator/start-intents/{"{intent_id}"}</span>
          <span className="mono">{intent ? `updated ${formatDateTime(intent.updated_at)}` : "read-only"}</span>
          <button className="outline-button start-export-button" type="button" disabled={!intent || loading} onClick={() => downloadIntent(intent)}>
            <DownloadSimple size={18} />导出审计 JSON
          </button>
        </footer>
      </section>
    </div>
  );
}

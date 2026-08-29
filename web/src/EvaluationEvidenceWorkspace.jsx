import { useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  ChartBar,
  CheckCircle,
  CircleNotch,
  DownloadSimple,
  Eye,
  FileLock,
  FileText,
  Flask,
  Info,
  LockKey,
  MagnifyingGlass,
  Scales,
  ShieldCheck,
  Stack,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

import { loadOperatorEvaluationEvidence } from "./api.js";
import { loadDemoEvaluationEvidence } from "./demo-data.js";
import {
  evaluationStageCopy,
  evaluationStageState,
  evaluationStatusCopy,
  summarizeEvaluationEvidence,
  verdictTone,
} from "./evaluation-evidence.js";

const stageIcons = {
  search: MagnifyingGlass,
  holdout: Flask,
  fwer: Scales,
  evidence: FileText,
};

const verdictCopy = {
  faster: "FASTER",
  slower: "SLOWER",
  inconclusive: "INCONCLUSIVE",
  invalid: "INVALID",
};

const memberStateCopy = {
  search_measured: "Search 已测量",
  correctness_failed: "正确性失败",
  build_failed: "Build 失败",
  search_failed: "Search 失败",
  invalid: "无效",
  holdout_measured: "Holdout 已测量",
  holdout_failed: "Holdout 失败",
};

function shortValue(value, length = 12) {
  if (!value) return "—";
  return String(value).replace("sha256:", "").slice(0, length);
}

function ratio(value) {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
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

function downloadWorkspace(workspace) {
  const blob = new Blob([`${JSON.stringify(workspace, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `operator-evaluation-evidence-${shortValue(workspace.round_id, 8)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function Fact({ label, value, title = value, tone = "" }) {
  return (
    <div className={`evaluation-fact ${tone}`}>
      <dt>{label}</dt>
      <dd className="mono" title={title || ""}>{value ?? "—"}</dd>
    </div>
  );
}

function StageRail({ workspace, activeStage, onStageChange }) {
  return (
    <ol className="evaluation-stage-rail" aria-label="批级评测证据阶段">
      {Object.entries(evaluationStageCopy).map(([key, copy]) => {
        const Icon = stageIcons[key];
        const state = evaluationStageState(workspace, key);
        const status = workspace?.[`${key}_status`] || "pending";
        return (
          <li className={`${state} ${activeStage === key ? "active" : ""}`} key={key}>
            <button
              type="button"
              aria-pressed={activeStage === key}
              onClick={() => onStageChange(key)}
            >
              <span className="evaluation-stage-node">
                {state === "complete" ? (
                  <CheckCircle size={19} weight="fill" />
                ) : state === "current" ? (
                  <CircleNotch size={19} />
                ) : (
                  <LockKey size={18} />
                )}
              </span>
              <Icon size={20} />
              <span>
                <small>{copy.eyebrow}</small>
                <strong>{copy.label}</strong>
                <em>{evaluationStatusCopy[status]}</em>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function AuthorityHead({ eyebrow, title, icon: Icon, status = "available" }) {
  return (
    <div className="evaluation-authority-head">
      <span className="evaluation-authority-icon"><Icon size={25} /></span>
      <div><span>{eyebrow}</span><h2>{title}</h2></div>
      <span className={`evaluation-status ${status}`}>
        {status === "available" ? <CheckCircle size={16} weight="fill" /> : <CircleNotch size={16} />}
        {evaluationStatusCopy[status]}
      </span>
    </div>
  );
}

function LockedStage({ stage, status }) {
  const copy = evaluationStageCopy[stage];
  const Icon = stageIcons[stage];
  return (
    <section className="evaluation-authority-card evaluation-locked-card">
      <Icon size={34} />
      <span>{copy.eyebrow}</span>
      <h2>{evaluationStatusCopy[status] || "权威证据未形成"}</h2>
      <p>{copy.detail}。页面不会从候选状态或其他阶段拼出一个不存在的结论。</p>
    </section>
  );
}

function SearchStage({ workspace }) {
  const search = workspace.search;
  if (!search) return <LockedStage stage="search" status={workspace.search_status} />;
  return (
    <div className="evaluation-stage-content">
      <section className="evaluation-authority-card">
        <AuthorityHead eyebrow="BATCH AUTHORITY" title="Search Barrier" icon={ChartBar} />
        <div className="evaluation-callout search">
          <Stack size={25} />
          <div><span>Frozen family</span><strong>{search.expected_member_count} 个成员全部到齐</strong></div>
          <span className="mono">promoted {search.promoted_candidate_ids.length}</span>
        </div>
        <dl className="evaluation-facts four">
          <Fact label="Barrier ID" value={shortValue(search.barrier_id)} title={search.barrier_id} />
          <Fact label="Outcome" value={search.outcome} />
          <Fact label="Rule" value={search.rule_version} />
          <Fact label="Input Summary" value={shortValue(search.input_summary_hash)} title={search.input_summary_hash} />
        </dl>
      </section>
      <section className="evaluation-member-grid">
        {search.members.map((member) => (
          <article className={`evaluation-member-card ${member.promoted ? "promoted" : ""}`} key={member.round_candidate_id}>
            <header>
              <div><span className="mono">M2-{String(member.ordinal + 1).padStart(2, "0")}</span><small className="mono">{shortValue(member.candidate_id)}</small></div>
              <span className={member.promoted ? "success" : member.state.includes("failed") ? "danger" : "neutral"}>
                {member.promoted ? "晋级 Holdout" : memberStateCopy[member.state] || member.state}
              </span>
            </header>
            {member.restart_effects.length ? (
              <div className="restart-strip">
                <span>Frozen restart effects</span>
                <div>{member.restart_effects.map((value, index) => <b className="mono" key={`${member.candidate_id}-${index}`}>{ratio(value)}</b>)}</div>
                <small>仅展示 Search Barrier 中的 Synthetic 输入，不形成真实性能结论。</small>
              </div>
            ) : (
              <div className="evaluation-member-failure"><WarningCircle size={20} /><span>{memberStateCopy[member.state] || member.state}</span></div>
            )}
            <dl className="evaluation-facts compact">
              <Fact label="Raw Evidence" value={shortValue(member.raw_evidence_hash)} title={member.raw_evidence_hash} />
              <Fact label="Stage 0 MDE" value={ratio(member.stage0_mde_ratio)} />
              <Fact label="Budget Evidence" value={shortValue(member.budget_usage_evidence_hash)} title={member.budget_usage_evidence_hash} />
              <Fact label="Cleanup" value={shortValue(member.cleanup_evidence_hash)} title={member.cleanup_evidence_hash} />
            </dl>
          </article>
        ))}
      </section>
    </div>
  );
}

function HoldoutStage({ workspace }) {
  if (workspace.holdout_status === "not_applicable") {
    return <LockedStage stage="holdout" status="not_applicable" />;
  }
  if (!workspace.holdout_reveal) {
    return <LockedStage stage="holdout" status={workspace.holdout_status} />;
  }
  const reveal = workspace.holdout_reveal;
  const holdout = workspace.holdout;
  return (
    <div className="evaluation-stage-content holdout-layout">
      <section className="evaluation-authority-card reveal">
        <AuthorityHead eyebrow="D-OWNED AUTHORITY" title="Holdout Plan Reveal" icon={FileLock} status={holdout ? "available" : "revealed"} />
        <div className="evaluation-callout holdout">
          <ShieldCheck size={25} />
          <div><span>Commit → Reveal</span><strong>冻结计划已在独立 Lease 下揭示</strong></div>
          <span className="mono">fence {reveal.fencing_token}</span>
        </div>
        <dl className="evaluation-facts two">
          <Fact label="Reveal Lease" value={shortValue(reveal.reveal_lease_id)} title={reveal.reveal_lease_id} />
          <Fact label="Resource" value={reveal.resource_id} />
          <Fact label="Plan Hash" value={shortValue(reveal.plan_hash)} title={reveal.plan_hash} />
          <Fact label="Reveal Evidence" value={shortValue(reveal.reveal_evidence_hash)} title={reveal.reveal_evidence_hash} />
          <Fact label="Authority" value={reveal.authority_id} />
          <Fact label="Revealed At" value={formatDateTime(reveal.revealed_at)} />
        </dl>
      </section>
      {holdout ? (
        <section className="evaluation-authority-card">
          <AuthorityHead eyebrow="FROZEN PROMOTED FAMILY" title="Holdout Barrier" icon={Flask} />
          <div className="holdout-member-list">
            {holdout.members.map((member) => (
              <article key={member.round_candidate_id}>
                <span className="mono">M2-{String(member.ordinal + 1).padStart(2, "0")}</span>
                <div><strong>{memberStateCopy[member.state] || member.state}</strong><small className="mono">candidate {shortValue(member.candidate_id)}</small></div>
                <CheckCircle size={22} weight="fill" />
              </article>
            ))}
          </div>
          <dl className="evaluation-facts two">
            <Fact label="Barrier ID" value={shortValue(holdout.barrier_id)} title={holdout.barrier_id} />
            <Fact label="Family Hash" value={shortValue(holdout.input_family_hash)} title={holdout.input_family_hash} />
            <Fact label="Rule" value={holdout.rule_version} />
            <Fact label="Closed At" value={formatDateTime(holdout.closed_at)} />
          </dl>
        </section>
      ) : <LockedStage stage="holdout" status="revealed" />}
    </div>
  );
}

function FwerStage({ workspace }) {
  const fwer = workspace.fwer;
  if (!fwer) return <LockedStage stage="fwer" status={workspace.fwer_status} />;
  return (
    <div className="evaluation-stage-content fwer-layout">
      <section className="evaluation-authority-card fwer-protocol-card">
        <AuthorityHead eyebrow="D-OWNED BATCH VERDICT" title="Bonferroni FWER" icon={Scales} />
        <div className="fwer-equation mono">
          <span>family α</span><strong>{fwer.family_alpha.toFixed(3)}</strong><i>÷</i><span>m</span><strong>{fwer.m}</strong><i>=</i><span>candidate α</span><strong>{fwer.alpha_candidate.toFixed(3)}</strong>
        </div>
        <dl className="evaluation-facts two">
          <Fact label="Protocol" value={fwer.protocol_version} />
          <Fact label="Protocol Hash" value={shortValue(fwer.protocol_hash)} title={fwer.protocol_hash} />
          <Fact label="Result Hash" value={shortValue(fwer.result_hash)} title={fwer.result_hash} />
          <Fact label="Created At" value={formatDateTime(fwer.created_at)} />
        </dl>
      </section>
      <section className="fwer-result-list">
        {fwer.candidates.map((candidate) => {
          const tone = verdictTone(candidate.verdict);
          const recommended = candidate.candidate_id === fwer.recommended_candidate_id;
          return (
            <article className={`fwer-result-card ${tone}`} key={candidate.candidate_id}>
              <header>
                <div><span className="mono">M2-{String(candidate.ordinal + 1).padStart(2, "0")}</span><small className="mono">{shortValue(candidate.candidate_id)}</small></div>
                <span className={`verdict-badge ${tone}`}>{verdictCopy[candidate.verdict]}</span>
              </header>
              {recommended && <div className="recommendation-banner"><ShieldCheck size={20} weight="fill" />Synthetic fixture recommendation</div>}
              <div className="ci-grid">
                <div><span>Adjusted CI lower</span><strong className="mono">{ratio(candidate.adjusted_ci_lower)}</strong></div>
                <div><span>Adjusted CI upper</span><strong className="mono">{ratio(candidate.adjusted_ci_upper)}</strong></div>
                <div><span>Credible threshold</span><strong className="mono">{ratio(candidate.credible_threshold)}</strong></div>
              </div>
              <div className="mde-row">
                <span>Stage 0 MDE <b className="mono">{ratio(candidate.stage0_mde_ratio)}</b></span>
                <span>Workload MDE <b className="mono">{ratio(candidate.workload_mde_ratio)}</b></span>
              </div>
              <p><Info size={16} />Verdict 来自后端 Multiple Comparison Authority，页面没有重新计算。</p>
            </article>
          );
        })}
      </section>
    </div>
  );
}

function EvidenceStage({ workspace }) {
  const bundle = workspace.evidence_bundle;
  if (!bundle) return <LockedStage stage="evidence" status={workspace.evidence_status} />;
  return (
    <div className="evaluation-stage-content evidence-layout">
      <section className="evidence-chain" aria-label="Evidence authority chain">
        <div><ChartBar size={22} /><span>Search Barrier</span><strong className="mono">{shortValue(bundle.search_barrier_id)}</strong></div>
        <i>→</i>
        <div><Flask size={22} /><span>Holdout Barrier</span><strong className="mono">{shortValue(bundle.holdout_barrier_id)}</strong></div>
        <i>→</i>
        <div><Scales size={22} /><span>FWER</span><strong className="mono">{shortValue(bundle.multiple_comparison_id)}</strong></div>
        <i>→</i>
        <div className="active"><FileText size={22} /><span>EvidenceBundle</span><strong className="mono">{shortValue(bundle.round_evidence_bundle_id)}</strong></div>
      </section>
      <section className="evaluation-authority-card evidence-bundle-card">
        <AuthorityHead eyebrow="IMMUTABLE TERMINAL AUTHORITY" title="Round EvidenceBundle" icon={FileText} />
        <div className="evidence-boundary-hero">
          <LockKey size={30} />
          <div><span>结论边界</span><strong>{bundle.summary.performance_conclusion}</strong><small>{bundle.summary.evidence_authority}</small></div>
          <span className="mono">automatic_release=false</span>
        </div>
        <dl className="evaluation-facts four">
          <Fact label="Terminal Reason" value={bundle.terminal_reason} />
          <Fact label="Candidate Family" value={shortValue(bundle.candidate_family_hash)} title={bundle.candidate_family_hash} />
          <Fact label="Artifact Family" value={shortValue(bundle.artifact_family_hash)} title={bundle.artifact_family_hash} />
          <Fact label="Budget Ledger" value={shortValue(bundle.budget_ledger_hash)} title={bundle.budget_ledger_hash} />
          <Fact label="Evidence Index" value={shortValue(bundle.evidence_index_hash)} title={bundle.evidence_index_hash} />
          <Fact label="Index URI" value={bundle.evidence_index_uri} title={bundle.evidence_index_uri} />
          <Fact label="Candidate Count" value={bundle.summary.candidate_count} />
          <Fact label="Created At" value={formatDateTime(bundle.created_at)} />
        </dl>
      </section>
    </div>
  );
}

function LoadingState() {
  return (
    <section className="evaluation-empty">
      <CircleNotch className="spin" size={36} />
      <h2>正在读取批级 Evaluation Authority…</h2>
      <p>等待 Search、Holdout、FWER 与 EvidenceBundle 的同一只读快照。</p>
    </section>
  );
}

function ErrorState({ error, onRetry }) {
  return (
    <section className="evaluation-empty danger">
      <WarningCircle size={42} />
      <span>Operator API error</span>
      <h2>评测证据暂不可用</h2>
      <p>{error}</p>
      <button className="primary-button" type="button" onClick={onRetry}><ArrowClockwise size={18} />重新读取</button>
    </section>
  );
}

export function EvaluationEvidenceWorkspace({
  round,
  demoMode,
  initialStage = "search",
  onClose,
}) {
  const [workspace, setWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeStage, setActiveStage] = useState(initialStage);
  const roundId = round?.round_id;

  useEffect(() => {
    document.body.classList.add("workspace-open");
    return () => document.body.classList.remove("workspace-open");
  }, []);

  const load = async () => {
    if (!roundId) {
      setWorkspace(null);
      setLoading(false);
      setError("missing_round_id: 当前页面没有权威 round_id");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const value = demoMode
        ? await loadDemoEvaluationEvidence(roundId)
        : await loadOperatorEvaluationEvidence(roundId);
      setWorkspace(value);
    } catch (loadError) {
      setWorkspace(null);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    const loader = demoMode
      ? loadDemoEvaluationEvidence(roundId)
      : loadOperatorEvaluationEvidence(roundId);
    loader
      .then((value) => {
        if (!active) return;
        setWorkspace(value);
      })
      .catch((loadError) => {
        if (!active) return;
        setWorkspace(null);
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [demoMode, roundId]);

  const summary = useMemo(() => summarizeEvaluationEvidence(workspace), [workspace]);

  return (
    <div className="plan-workspace-backdrop evaluation-backdrop" role="presentation">
      <section className="plan-workspace evaluation-workspace" role="dialog" aria-modal="true" aria-labelledby="evaluation-title">
        <header className="plan-workspace-header">
          <div className="plan-workspace-title">
            <span className="plan-icon"><Scales size={23} /></span>
            <div><span>UI-4 · READ-ONLY BATCH EVIDENCE</span><h1 id="evaluation-title">Search → Holdout → FWER → Evidence</h1></div>
          </div>
          <div className="plan-workspace-meta">
            <span className="synthetic-badge">Synthetic fixture</span>
            <span className="read-only-tag"><Eye size={15} />只读</span>
            <button className="icon-button" type="button" aria-label="关闭评测证据" title="关闭评测证据" onClick={onClose}><X size={20} /></button>
          </div>
        </header>

        <div className="evaluation-summary">
          <div><span>Search Family</span><strong>{summary.searchMemberCount}</strong><small>{summary.promotedCount} 个晋级</small></div>
          <div><span>Holdout Family</span><strong>{summary.holdoutMemberCount}</strong><small>独立冻结成员</small></div>
          <div><span>FWER Results</span><strong>{summary.fwerCandidateCount}</strong><small>{summary.recommendationId ? "有 Synthetic 推荐" : "无推荐"}</small></div>
          <div className="release-locked"><LockKey size={21} /><span>真实结论 / 发布</span><strong>false</strong></div>
        </div>

        <div className="plan-workspace-body evaluation-body">
          {loading ? (
            <LoadingState />
          ) : error ? (
            <ErrorState error={error} onRetry={load} />
          ) : !workspace ? (
            <section className="evaluation-empty"><FileText size={42} /><h2>当前 Round 没有评测证据</h2></section>
          ) : (
            <div className="evaluation-content">
              <StageRail workspace={workspace} activeStage={activeStage} onStageChange={setActiveStage} />
              <div className="evaluation-detail">
                {activeStage === "search" && <SearchStage workspace={workspace} />}
                {activeStage === "holdout" && <HoldoutStage workspace={workspace} />}
                {activeStage === "fwer" && <FwerStage workspace={workspace} />}
                {activeStage === "evidence" && <EvidenceStage workspace={workspace} />}
                <section className="evaluation-boundary">
                  <ShieldCheck size={22} />
                  <div><strong>展示权威 verdict，不在 UI 重算</strong><p>不调用 POST，不触发 Barrier/FWER/Finalize，不开放 Formal、Agent/Apex 或发布。</p></div>
                  <span className="mono">real_performance_claim_allowed=false</span>
                </section>
              </div>
            </div>
          )}
        </div>

        <footer className="plan-workspace-footer">
          <span><Info size={17} />Authority 来自 GET /v1/operator/search-rounds/{"{round_id}"}/evaluation-evidence</span>
          <span className="mono">{workspace ? `snapshot ${formatDateTime(workspace.generated_at)}` : "read-only"}</span>
          <button className="outline-button start-export-button" type="button" disabled={!workspace || loading} onClick={() => downloadWorkspace(workspace)}><DownloadSimple size={18} />导出证据 JSON</button>
        </footer>
      </section>
    </div>
  );
}

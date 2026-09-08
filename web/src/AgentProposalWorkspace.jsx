// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import { useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  Check,
  CheckCircle,
  CircleNotch,
  ClipboardText,
  Code,
  DownloadSimple,
  Eye,
  FileLock,
  Fingerprint,
  Info,
  LockKey,
  Package,
  Robot,
  ShieldCheck,
  Sparkle,
  Timer,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

import { loadOperatorAgentInspection, loadOperatorAgentProposals } from "./api.js";
import { agentWorkspacePresentation, inspectionReadModel, pendingReviewDisplay } from "./agent-proposals.js";
import { loadDemoAgentProposals } from "./demo-data.js";

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

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KiB`;
}

function formatDuration(value) {
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(value < 10 ? 1 : 0)} s`;
}

function downloadWorkspace(workspace) {
  const blob = new Blob([`${JSON.stringify(workspace, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `agent-apex-evidence-${shortValue(workspace.generation_run_id, 8)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function Fact({ label, value, title = value }) {
  return (
    <div className="agent-fact">
      <dt>{label}</dt>
      <dd className="mono" title={title || ""}>{value || "—"}</dd>
    </div>
  );
}

function BudgetMeter({ label, value, limit, ratio, format = (item) => item }) {
  const safeRatio = Math.max(0, Math.min(1, ratio || 0));
  return (
    <div className="agent-budget-meter">
      <div>
        <span>{label}</span>
        <strong>{format(value)} <small>/ {format(limit)}</small></strong>
      </div>
      <span className="agent-budget-track" aria-label={`${label} 使用率 ${Math.round(safeRatio * 100)}%`}>
        <i style={{ width: `${safeRatio * 100}%` }} />
      </span>
    </div>
  );
}

function PlanCard({ workspace, summary }) {
  const budget = summary.budget;
  return (
    <section className="agent-detail-card agent-plan-card">
      <div className="agent-card-head">
        <span className="agent-card-icon"><Sparkle size={24} /></span>
        <div>
          <span>APEX PLAN</span>
          <h2>生成范围与预算</h2>
        </div>
        <span className="evidence-status success"><CheckCircle size={16} weight="fill" />已冻结</span>
      </div>
      <p className="agent-card-intro">
        Apex 只协调生成器、预算和稳定去重；它不接触 HCU、计时、Barrier、FWER 或发布。
      </p>
      <dl className="agent-facts four">
        <Fact label="Generation Run" value={shortValue(workspace.generation_run_id)} title={workspace.generation_run_id} />
        <Fact label="Request" value={shortValue(workspace.request_id)} title={workspace.request_id} />
        <Fact label="Plan" value={shortValue(workspace.plan_id)} title={workspace.plan_id} />
        <Fact label="Target" value={workspace.target_id} />
        <Fact label="Baseline Epoch" value={shortValue(workspace.baseline_epoch_id)} title={workspace.baseline_epoch_id} />
        <Fact label="Replacement Point" value={workspace.replacement_point} title={workspace.replacement_point} />
        <Fact label="Request Hash" value={shortValue(workspace.request_hash)} title={workspace.request_hash} />
        <Fact label="Knowledge Hash" value={shortValue(workspace.knowledge_hash)} title={workspace.knowledge_hash} />
      </dl>
      <div className="agent-budget-grid">
        <BudgetMeter label="尝试次数" value={budget.usage.attempt_count} limit={budget.limit.max_generator_attempts} ratio={budget.attemptsRatio} />
        <BudgetMeter label="生成耗时" value={budget.usage.wall_seconds} limit={budget.limit.max_wall_seconds} ratio={budget.wallRatio} format={formatDuration} />
        <BudgetMeter label="输出大小" value={budget.usage.output_bytes} limit={budget.limit.max_total_output_bytes} ratio={budget.outputRatio} format={formatBytes} />
        <BudgetMeter label="Token" value={budget.usage.output_tokens} limit={budget.limit.max_total_tokens} ratio={budget.tokensRatio} />
        <BudgetMeter label="提案数" value={budget.usage.proposal_count} limit={budget.limit.max_proposals} ratio={budget.proposalsRatio} />
      </div>
    </section>
  );
}

function attemptMeta(attempt) {
  if (attempt.status === "succeeded") return ["success", "成功", CheckCircle];
  if (attempt.status === "timed_out") return ["danger", "超时", Timer];
  return ["locked", attempt.status || "未知", WarningCircle];
}

function AttemptsCard({ attempts }) {
  return (
    <section className="agent-detail-card">
      <div className="agent-card-head">
        <span className="agent-card-icon"><Robot size={24} /></span>
        <div><span>RUNNER RECEIPTS</span><h2>生成尝试与清理</h2></div>
        <span className="read-only-tag"><Eye size={15} />只读</span>
      </div>
      <div className="agent-attempt-list">
        {attempts.map((attempt) => {
          const [tone, label, Icon] = attemptMeta(attempt);
          return (
            <article className={`agent-attempt ${tone}`} key={attempt.attempt_id}>
              <span className="agent-attempt-node">
                {tone === "success" ? <Check size={18} weight="bold" /> : <Icon size={18} weight="fill" />}
              </span>
              <div className="agent-attempt-main">
                <header>
                  <div>
                    <span className="mono">{attempt.generator_id} · attempt {attempt.attempt_number}</span>
                    <strong>{label}{attempt.reason_code ? ` · ${attempt.reason_code}` : ""}</strong>
                  </div>
                  <span className={`evidence-status ${tone}`}><Icon size={15} weight="fill" />{label}</span>
                </header>
                <dl className="agent-facts compact">
                  <Fact label="Runner Receipt" value={shortValue(attempt.runner_receipt_id)} title={attempt.runner_receipt_id} />
                  <Fact label="Receipt Hash" value={shortValue(attempt.runner_receipt_hash)} title={attempt.runner_receipt_hash} />
                  <Fact label="Runner" value={attempt.runner_provenance?.adapter_name} />
                  <Fact label="Profile" value={attempt.runner_provenance?.profile} />
                  <Fact label="耗时" value={formatDuration(attempt.wall_seconds)} />
                  <Fact label="Token 用量" value={attempt.output_tokens} />
                  <Fact label="生成器" value={attempt.generator_adapter_profile} />
                  <Fact label="清理" value={attempt.cleanup_status} title={attempt.cleanup_summary} />
                </dl>
                <p className="agent-attempt-cleanup">
                  <ShieldCheck size={16} weight="fill" />
                  {attempt.cleanup_healthy ? "Runner 已清理" : "Runner 清理未确认"}：{attempt.cleanup_summary || "—"}
                </p>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function CopyIcon({ size = 20, ...props }) {
  return <ClipboardText size={size} {...props} />;
}

function proposalMeta(proposal) {
  if (proposal.status === "kept") return ["success", "保留", CheckCircle];
  if (proposal.reason_code?.startsWith("duplicate_")) return ["warning", "稳定去重", CopyIcon];
  return ["danger", "淘汰", WarningCircle];
}

function ProposalList({ proposals, selectedId, onSelect }) {
  return (
    <section className="agent-proposal-list-card">
      <div className="agent-proposal-list-head">
        <div><span>D VERDICT</span><h2>提案与去重</h2></div>
        <span className="candidate-count mono">{proposals.length}</span>
      </div>
      <div className="agent-proposal-list" role="listbox" aria-label="Agent 提案">
        {proposals.map((proposal) => {
          const [tone, label, Icon] = proposalMeta(proposal);
          const selected = proposal.proposal_id === selectedId;
          return (
            <button
              className={`${selected ? "selected" : ""} ${tone}`}
              type="button"
              role="option"
              aria-selected={selected}
              key={proposal.proposal_id}
              onClick={() => onSelect(proposal.proposal_id)}
            >
              <span className={`agent-proposal-status ${tone}`}><Icon size={16} weight="fill" />{label}</span>
              <strong>{proposal.optimization_intent}</strong>
              <span className="mono">{shortValue(proposal.proposal_id)}</span>
              <small>{proposal.reason_code || `${proposal.generator_id} · D 独立判定`}</small>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function ProposalDetail({ proposal }) {
  const [tone, label, Icon] = proposalMeta(proposal);
  const lifecycle = proposal.lifecycle || {};
  return (
    <section className="agent-detail-card">
      <div className="agent-card-head">
        <span className="agent-card-icon"><Code size={24} /></span>
        <div><span>CANDIDATE PROPOSAL</span><h2>变更预览与 D 判定</h2></div>
        <span className={`evidence-status ${tone}`}><Icon size={16} weight="fill" />{label}</span>
      </div>
      <div className="agent-intent-strip">
        <Fingerprint size={21} />
        <div><span>优化意图</span><strong>{proposal.optimization_intent}</strong></div>
      </div>
      <div className="agent-patch-preview">
        <header><span>Patch Preview</span><span className="mono">{shortValue(proposal.normalized_patch_hash, 16)}</span></header>
        <pre>{proposal.patch_preview || "未提供可见 Patch 预览"}</pre>
      </div>
      <div className="agent-risk-grid">
        <div><span>风险边界</span><p>{proposal.risk_summary}</p></div>
        <div><span>影响路径</span><p className="mono">{proposal.touched_paths?.join("\n") || "—"}</p></div>
      </div>
      <dl className="agent-facts four">
        <Fact label="Proposal" value={shortValue(proposal.proposal_id)} title={proposal.proposal_id} />
        <Fact label="Raw Hash" value={shortValue(proposal.exact_patch_hash)} title={proposal.exact_patch_hash} />
        <Fact label="Normalized Hash" value={shortValue(proposal.normalized_patch_hash)} title={proposal.normalized_patch_hash} />
        <Fact label="Identity Hash" value={shortValue(proposal.candidate_identity_hash)} title={proposal.candidate_identity_hash} />
      </dl>
      {lifecycle.review_status === "not_applicable" && (
        <p className="agent-locked-note"><LockKey size={17} />去重后的提案不进入人工审核，也不会打包成 Candidate。</p>
      )}
    </section>
  );
}

function LifecycleCard({ proposal, summary }) {
  const lifecycle = proposal.lifecycle || {};
  const review = lifecycle.review;
  const promotion = lifecycle.promotion;
  const missingReview = pendingReviewDisplay(lifecycle);
  return (
    <section className="agent-detail-card agent-lifecycle-card">
      <div className="agent-card-head">
        <span className="agent-card-icon"><Package size={24} /></span>
        <div><span>HUMAN + FAMILY EVIDENCE</span><h2>审核、打包与独立复读</h2></div>
        <span className="evidence-status warning"><LockKey size={16} />{summary.lifecycle.formalReadiness.toUpperCase()}</span>
      </div>
      {review ? (
        <div className="agent-lifecycle-step complete">
          <span><CheckCircle size={20} weight="fill" /></span>
          <div><small>人工审核</small><strong>{review.decision} · {review.reviewer}</strong><p>{review.reason}</p><em className="mono">record {shortValue(review.review_record_hash)}</em></div>
        </div>
      ) : (
        <div className="agent-lifecycle-step locked"><span><LockKey size={20} /></span><div><small>人工审核</small><strong>{missingReview.title}</strong><p>{missingReview.description}</p></div></div>
      )}
      {promotion ? (
        <div className="agent-lifecycle-step complete">
          <span><FileLock size={20} weight="fill" /></span>
          <div><small>Business Package</small><strong>已打包，但未接入 Formal</strong><p className="mono">candidate {shortValue(promotion.candidate_id)} · family {shortValue(promotion.source_family_hash)}</p><em>独立 Family Verifier：{promotion.source_family_verifier_provenance?.adapter_name || "—"}</em></div>
        </div>
      ) : (
        <div className="agent-lifecycle-step locked"><span><LockKey size={20} /></span><div><small>Business Package</small><strong>未形成</strong><p>没有 Promotion Receipt，页面不会假定存在业务候选。</p></div></div>
      )}
      <div className="agent-readiness-hold">
        <LockKey size={23} />
        <div><span>Formal Readiness</span><strong>HOLD</strong><p>business Package 不会伪装为 fixture；M2a Scripted Search / Holdout / Barrier / FWER 回归仍走独立链路。</p></div>
      </div>
    </section>
  );
}

function AuthorityBoundary({ workspace }) {
  return (
    <section className="agent-authority-boundary">
      <ShieldCheck size={26} />
      <div>
        <strong>只读 Proposal 证据，不扩大可信执行权限</strong>
        <p>此页不派发 HCU、不计时、不做 Holdout 或 FWER、不签核、不发布；性能结论固定为 {workspace.performance_conclusion || "not_measured"}。</p>
      </div>
      <span className="mono">release=false</span>
    </section>
  );
}

function LoadingState() {
  return <section className="agent-workspace-empty"><CircleNotch className="spin" size={36} /><h2>正在读取 Agent/Apex Evidence…</h2><p>仅读取 D 线已经判定的快照。</p></section>;
}

function ErrorState({ error, onRetry }) {
  return <section className="agent-workspace-empty danger"><WarningCircle size={42} /><span>Operator API error</span><h2>Agent/Apex 证据暂不可用</h2><p>{error}</p><button className="primary-button" type="button" onClick={onRetry}><ArrowClockwise size={18} />重新读取</button></section>;
}

export function AgentProposalWorkspace({ demoMode, generationRunId, onClose, inspectionMode = false, inspectionAuthorization }) {
  const [loadedWorkspace, setWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    document.body.classList.add("workspace-open");
    return () => document.body.classList.remove("workspace-open");
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function readWorkspace() {
      try {
        const value = inspectionMode
          ? inspectionReadModel(await loadOperatorAgentInspection(generationRunId, inspectionAuthorization), generationRunId)
          : demoMode
          ? await loadDemoAgentProposals()
          : await loadOperatorAgentProposals(generationRunId);
        if (cancelled) return;
        setWorkspace(value);
        setSelectedId((current) =>
          value.proposals?.some((proposal) => proposal.proposal_id === current)
            ? current
            : value.proposals?.[0]?.proposal_id || null,
        );
        setError(null);
      } catch (loadError) {
        if (cancelled) return;
        setWorkspace(null);
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void readWorkspace();
    return () => { cancelled = true; };
  }, [demoMode, generationRunId, reloadNonce, inspectionMode, inspectionAuthorization]);

  const retry = () => {
    setLoading(true);
    setError(null);
    setReloadNonce((current) => current + 1);
  };

  const { workspace, summary, metrics } = useMemo(
    () => agentWorkspacePresentation(loadedWorkspace, { loading, error }),
    [loadedWorkspace, loading, error],
  );
  const selectedProposal = workspace?.proposals?.find((proposal) => proposal.proposal_id === selectedId)
    || workspace?.proposals?.[0];

  return (
    <div className="plan-workspace-backdrop agent-workspace-backdrop" role="presentation">
      <section className="plan-workspace agent-workspace" role="dialog" aria-modal="true" aria-labelledby="agent-workspace-title">
        <header className="plan-workspace-header">
          <div className="plan-workspace-title">
            <span className="plan-icon"><Robot size={23} /></span>
            <div><span>UI-5 · READ-ONLY AGENT / APEX EVIDENCE</span><h1 id="agent-workspace-title">Agent Proposal → Review → Business Package</h1></div>
          </div>
          <div className="plan-workspace-meta">
            <span className="synthetic-badge">Proposal-only</span>
            <span className="read-only-tag"><Eye size={15} />只读</span>
            <button className="icon-button" type="button" aria-label="关闭 Agent/Apex 证据" title="关闭 Agent/Apex 证据" onClick={onClose}><X size={20} /></button>
          </div>
        </header>

        <div className="agent-summary">
          <div><span>Generator Attempts</span><strong>{metrics.attempts}</strong><small>{metrics.failed}</small></div>
          <div><span>Retained Proposal</span><strong>{metrics.retained}</strong><small>{metrics.duplicates}</small></div>
          <div><span>Budget Used</span><strong>{metrics.budgetPercent}</strong><small>{metrics.budgetAttempts}</small></div>
          <div className="release-locked"><LockKey size={21} /><span>Formal Readiness</span><strong>HOLD</strong><small>禁止自动发布</small></div>
        </div>

        <div className="plan-workspace-body agent-workspace-body">
          {loading ? <LoadingState /> : error ? <ErrorState error={error} onRetry={retry} /> : workspace && (
            <div className="agent-workspace-content">
              {inspectionMode && <section className="agent-locked-note"><LockKey size={18} />审核前只读检查，不是人工签核或终态发布。沿用 D v1 开发证据分类；真实 Runner 不等于真实模型质量或性能已验证。</section>}
              <PlanCard workspace={workspace} summary={summary} />
              <AttemptsCard attempts={workspace.attempts || []} />
              {!!summary.failureCodes.length && <section className="agent-locked-note" role="status">未通过项：{summary.failureCodes.join("、")}</section>}
              {!workspace.proposals?.length ? (
                <section className="agent-workspace-empty"><Robot size={42} /><h2>当前 Generation Run 没有有效提案</h2><p>执行记录与用量仍保留在上方；不会自动补造候选或重新调用模型。</p></section>
              ) : <div className="agent-proposal-grid">
                <ProposalList proposals={workspace.proposals} selectedId={selectedProposal?.proposal_id} onSelect={setSelectedId} />
                <div className="agent-proposal-detail">
                  <ProposalDetail proposal={selectedProposal} />
                  <LifecycleCard proposal={selectedProposal} summary={summary} />
                </div>
              </div>}
              <AuthorityBoundary workspace={workspace} />
            </div>
          )}
        </div>

        <footer className="plan-workspace-footer">
          <span><Info size={17} />Business generation 与 M2a fixture regression 分离；页面只消费 D Read Model。</span>
          <span className="mono">{workspace ? `snapshot ${formatDateTime(workspace.generated_at)}` : "awaiting snapshot"}</span>
          {workspace && <button className="agent-export-button" type="button" onClick={() => downloadWorkspace(workspace)}><DownloadSimple size={16} />导出证据</button>}
        </footer>
      </section>
    </div>
  );
}

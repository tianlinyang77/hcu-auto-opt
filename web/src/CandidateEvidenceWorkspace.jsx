import { useEffect, useMemo, useState } from "react";
import {
  ArrowClockwise,
  BracketsCurly,
  Check,
  CheckCircle,
  CheckSquareOffset,
  CircleNotch,
  DownloadSimple,
  Eye,
  FileLock,
  Fingerprint,
  Hammer,
  Info,
  LockKey,
  Package,
  ShieldCheck,
  Stack,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

import { loadOperatorCandidateEvidence } from "./api.js";
import {
  candidateStageState,
  correctnessReasonCopy,
  evidenceStageCopy,
  summarizeCandidateEvidence,
} from "./candidate-evidence.js";
import { loadDemoCandidateEvidence } from "./demo-data.js";

const stageIcons = {
  candidate: Stack,
  build: Hammer,
  correctness: CheckSquareOffset,
};

const buildCopy = {
  pending: ["等待 Build", "pending"],
  available: ["Artifact 可用", "success"],
  failed: ["Build 失败", "danger"],
};

const correctnessCopy = {
  not_available: ["证据未形成", "locked"],
  passed: ["正确性通过", "success"],
  failed: ["正确性失败", "danger"],
};

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

function downloadWorkspace(workspace) {
  const blob = new Blob([`${JSON.stringify(workspace, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `operator-candidate-evidence-${shortValue(workspace.round_id, 8)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function Fact({ label, value, title = value }) {
  return (
    <div className="candidate-evidence-fact">
      <dt>{label}</dt>
      <dd className="mono" title={title || ""}>{value || "—"}</dd>
    </div>
  );
}

function StageRail({ candidate, activeStage, onStageChange }) {
  return (
    <ol className="candidate-evidence-rail" aria-label="Candidate 证据阶段">
      {Object.entries(evidenceStageCopy).map(([key, copy]) => {
        const Icon = stageIcons[key];
        const state = candidateStageState(candidate, key);
        return (
          <li className={`${state} ${activeStage === key ? "active" : ""}`} key={key}>
            <button
              type="button"
              aria-pressed={activeStage === key}
              onClick={() => onStageChange(key)}
            >
              <span className="candidate-evidence-node">
                {state === "complete" ? (
                  <Check size={18} weight="bold" />
                ) : state === "failed" ? (
                  <WarningCircle size={19} weight="fill" />
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
                <em>{copy.detail}</em>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function CandidateList({ candidates, selectedId, onSelect }) {
  return (
    <section className="candidate-evidence-list-card">
      <div className="candidate-evidence-list-head">
        <div>
          <span>FROZEN FAMILY</span>
          <h2>本轮候选</h2>
        </div>
        <span className="candidate-count mono">{candidates.length}</span>
      </div>
      <div className="candidate-evidence-list" role="listbox" aria-label="候选成员">
        {candidates.map((candidate) => {
          const selected = candidate.candidate_id === selectedId;
          const [buildLabel, buildTone] = buildCopy[candidate.build.status];
          const [correctnessLabel, correctnessTone] =
            correctnessCopy[candidate.correctness.status];
          return (
            <button
              className={selected ? "selected" : ""}
              type="button"
              role="option"
              aria-selected={selected}
              key={candidate.round_candidate_id}
              onClick={() => onSelect(candidate.candidate_id)}
            >
              <div className="candidate-evidence-list-title">
                <span className="mono">
                  M2-{String(candidate.ordinal + 1).padStart(2, "0")}
                </span>
                <small className="mono">{shortValue(candidate.candidate_id)}</small>
              </div>
              <strong>{candidate.source.optimization_intent}</strong>
              <span className="candidate-evidence-path">
                <BracketsCurly size={16} />
                {candidate.source.replacement_point}
              </span>
              <div className="candidate-evidence-list-states">
                <span className={buildTone}>
                  {candidate.build.status === "available" ? (
                    <CheckCircle size={15} weight="fill" />
                  ) : candidate.build.status === "failed" ? (
                    <WarningCircle size={15} weight="fill" />
                  ) : (
                    <CircleNotch size={15} />
                  )}
                  {buildLabel}
                </span>
                <span className={correctnessTone}>
                  {candidate.correctness.status === "passed" ? (
                    <ShieldCheck size={15} weight="fill" />
                  ) : candidate.correctness.status === "failed" ? (
                    <WarningCircle size={15} weight="fill" />
                  ) : (
                    <LockKey size={15} />
                  )}
                  {correctnessLabel}
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function SourceEvidence({ candidate }) {
  const source = candidate.source;
  return (
    <section className="candidate-evidence-detail-card source">
      <div className="candidate-evidence-detail-head">
        <span className="candidate-evidence-detail-icon"><Package size={25} /></span>
        <div><span>Candidate Authority</span><h2>来源与替换边界</h2></div>
        <span className="evidence-status success"><CheckCircle size={16} weight="fill" />已绑定</span>
      </div>
      <div className="candidate-intent-callout">
        <Fingerprint size={22} />
        <div><span>优化意图</span><strong>{source.optimization_intent}</strong></div>
      </div>
      <dl className="candidate-evidence-facts">
        <Fact label="Candidate ID" value={shortValue(candidate.candidate_id)} title={candidate.candidate_id} />
        <Fact label="Round Member" value={shortValue(candidate.round_candidate_id)} title={candidate.round_candidate_id} />
        <Fact label="Package Hash" value={shortValue(source.source_package_ref.source_package_hash)} title={source.source_package_ref.source_package_hash} />
        <Fact label="Candidate Source" value={shortValue(source.source_package_ref.candidate_source_hash)} title={source.source_package_ref.candidate_source_hash} />
        <Fact label="Manifest Hash" value={shortValue(source.source_package_ref.manifest_hash)} title={source.source_package_ref.manifest_hash} />
        <Fact label="Baseline Source" value={shortValue(source.baseline_source_hash)} title={source.baseline_source_hash} />
        <Fact label="Input Digest" value={shortValue(source.candidate_input_digest)} title={source.candidate_input_digest} />
        <Fact label="Package Store" value={`${source.source_package_store_id} · v${source.source_package_store_version}`} />
      </dl>
      <div className="candidate-replacement-strip">
        <BracketsCurly size={20} />
        <div><span>Replacement point</span><strong className="mono">{source.replacement_point}</strong></div>
        <small>{source.track} · {source.release_mode}</small>
      </div>
    </section>
  );
}

function BuildEvidence({ candidate }) {
  const build = candidate.build;
  const [label, tone] = buildCopy[build.status];
  return (
    <section className={`candidate-evidence-detail-card build ${tone}`}>
      <div className="candidate-evidence-detail-head">
        <span className="candidate-evidence-detail-icon"><Hammer size={25} /></span>
        <div><span>Build Authority</span><h2>构建终态与制品</h2></div>
        <span className={`evidence-status ${tone}`}>
          {build.status === "available" ? <CheckCircle size={16} weight="fill" /> : build.status === "failed" ? <WarningCircle size={16} weight="fill" /> : <CircleNotch size={16} />}
          {label}
        </span>
      </div>
      {build.status === "available" ? (
        <>
          <div className="artifact-hero">
            <FileLock size={31} weight="fill" />
            <div><span>Artifact 已形成</span><strong className="mono">{shortValue(build.artifact_hash, 20)}</strong></div>
          </div>
          <dl className="candidate-evidence-facts compact">
            <Fact label="Artifact ID" value={shortValue(build.artifact_id)} title={build.artifact_id} />
            <Fact label="Artifact Hash" value={shortValue(build.artifact_hash, 20)} title={build.artifact_hash} />
          </dl>
        </>
      ) : build.status === "failed" ? (
        <div className="build-failure-card">
          <WarningCircle size={29} weight="fill" />
          <div>
            <span className="mono">{build.terminal_failure_code}</span>
            <strong>失败证据已保留，未生成 Artifact</strong>
            <small className="mono">evidence {shortValue(build.failure_evidence_hash, 20)}</small>
          </div>
        </div>
      ) : (
        <div className="evidence-locked-card">
          <CircleNotch size={29} />
          <div><strong>等待 Build 终态</strong><span>页面不会把排队或构建中解释为成功。</span></div>
        </div>
      )}
      <p className="candidate-evidence-note"><Info size={17} />Artifact 与失败证据互斥，均由控制面成对写入。</p>
    </section>
  );
}

function CorrectnessEvidence({ candidate }) {
  const correctness = candidate.correctness;
  const [label, tone] = correctnessCopy[correctness.status];
  return (
    <section className={`candidate-evidence-detail-card correctness ${tone}`}>
      <div className="candidate-evidence-detail-head">
        <span className="candidate-evidence-detail-icon"><CheckSquareOffset size={25} /></span>
        <div><span>Correctness Authority</span><h2>正确性证据</h2></div>
        <span className={`evidence-status ${tone}`}>
          {correctness.status === "passed" ? <ShieldCheck size={16} weight="fill" /> : correctness.status === "failed" ? <WarningCircle size={16} weight="fill" /> : <LockKey size={16} />}
          {label}
        </span>
      </div>
      {correctness.status === "passed" ? (
        <div className="correctness-result-card passed">
          <ShieldCheck size={33} weight="fill" />
          <div><span>Search Barrier Authority</span><strong>正确性通过</strong><small className="mono">evidence {shortValue(correctness.correctness_evidence_hash, 20)}</small></div>
        </div>
      ) : correctness.status === "failed" ? (
        <div className="correctness-result-card failed">
          <WarningCircle size={33} weight="fill" />
          <div><span>Search Barrier Authority</span><strong>正确性失败</strong><small className="mono">failure {shortValue(correctness.failure_evidence_hash, 20)}</small></div>
        </div>
      ) : (
        <div className="evidence-locked-card">
          <LockKey size={31} />
          <div>
            <strong>{correctnessReasonCopy[correctness.reason] || "正确性证据未形成"}</strong>
            <span>Candidate 状态不是证据；只有不可变 Search Barrier Hash 才能解锁这里。</span>
          </div>
        </div>
      )}
      {correctness.search_barrier_id && (
        <dl className="candidate-evidence-facts compact">
          <Fact label="Search Barrier" value={shortValue(correctness.search_barrier_id)} title={correctness.search_barrier_id} />
          <Fact label="Authority" value={correctness.authority} />
        </dl>
      )}
      <p className="candidate-evidence-note"><Info size={17} />UI 不根据 state 推断通过，也不读取真实性能或发布结论。</p>
    </section>
  );
}

function LoadingState() {
  return (
    <section className="candidate-evidence-empty">
      <CircleNotch className="spin" size={36} />
      <h2>正在读取 Candidate Authority…</h2>
      <p>等待 Source、Build 与 Correctness 的同一只读快照。</p>
    </section>
  );
}

function ErrorState({ error, onRetry }) {
  return (
    <section className="candidate-evidence-empty danger">
      <WarningCircle size={42} />
      <span>Operator API error</span>
      <h2>候选证据暂不可用</h2>
      <p>{error}</p>
      <button className="primary-button" type="button" onClick={onRetry}>
        <ArrowClockwise size={18} />重新读取
      </button>
    </section>
  );
}

export function CandidateEvidenceWorkspace({
  round,
  demoMode,
  initialCandidateId,
  initialStage = "candidate",
  onClose,
}) {
  const [workspace, setWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(initialCandidateId || null);
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
        ? await loadDemoCandidateEvidence(roundId)
        : await loadOperatorCandidateEvidence(roundId);
      setWorkspace(value);
      setSelectedId((current) =>
        value.candidates.some((candidate) => candidate.candidate_id === current)
          ? current
          : value.candidates[0]?.candidate_id || null,
      );
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
      ? loadDemoCandidateEvidence(roundId)
      : loadOperatorCandidateEvidence(roundId);
    loader
      .then((value) => {
        if (!active) return;
        setWorkspace(value);
        setSelectedId((current) =>
          value.candidates.some((candidate) => candidate.candidate_id === current)
            ? current
            : value.candidates[0]?.candidate_id || null,
        );
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

  const selectedCandidate =
    workspace?.candidates.find((candidate) => candidate.candidate_id === selectedId) ||
    workspace?.candidates[0];
  const summary = useMemo(
    () => summarizeCandidateEvidence(workspace),
    [workspace],
  );

  return (
    <div className="plan-workspace-backdrop candidate-evidence-backdrop" role="presentation">
      <section className="plan-workspace candidate-evidence-workspace" role="dialog" aria-modal="true" aria-labelledby="candidate-evidence-title">
        <header className="plan-workspace-header">
          <div className="plan-workspace-title">
            <span className="plan-icon"><Stack size={23} /></span>
            <div>
              <span>UI-3 · READ-ONLY EVIDENCE WORKSPACE</span>
              <h1 id="candidate-evidence-title">Candidate → Build → Correctness</h1>
            </div>
          </div>
          <div className="plan-workspace-meta">
            <span className="synthetic-badge">Synthetic</span>
            <span className="read-only-tag"><Eye size={15} />只读</span>
            <button className="icon-button" type="button" aria-label="关闭候选证据" title="关闭候选证据" onClick={onClose}>
              <X size={20} />
            </button>
          </div>
        </header>

        <div className="candidate-evidence-summary">
          <div><span>Candidate Family</span><strong>{summary.candidateCount}</strong><small>来源均已绑定</small></div>
          <div><span>Build Artifact</span><strong>{summary.buildAvailableCount}</strong><small>{summary.buildFailedCount} 个失败终态</small></div>
          <div><span>Correctness</span><strong>{summary.correctnessPassedCount}</strong><small>{summary.correctnessUnavailableCount} 个证据未形成</small></div>
          <div className="release-locked"><LockKey size={21} /><span>自动发布</span><strong>false</strong></div>
        </div>

        <div className="plan-workspace-body candidate-evidence-body">
          {loading ? (
            <LoadingState />
          ) : error ? (
            <ErrorState error={error} onRetry={load} />
          ) : !workspace?.candidates.length ? (
            <section className="candidate-evidence-empty">
              <Package size={42} />
              <h2>当前 Round 没有候选证据</h2>
              <p>页面不会从 Hotspot 或历史 Round 拼接一个不存在的 Candidate Family。</p>
            </section>
          ) : (
            <div className="candidate-evidence-content">
              <StageRail
                candidate={selectedCandidate}
                activeStage={activeStage}
                onStageChange={setActiveStage}
              />
              <div className="candidate-evidence-grid">
                <CandidateList
                  candidates={workspace.candidates}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                />
                <div className="candidate-evidence-detail">
                  {activeStage === "candidate" && <SourceEvidence candidate={selectedCandidate} />}
                  {activeStage === "build" && <BuildEvidence candidate={selectedCandidate} />}
                  {activeStage === "correctness" && <CorrectnessEvidence candidate={selectedCandidate} />}
                  <section className="candidate-evidence-boundary">
                    <ShieldCheck size={22} />
                    <div><strong>证据可见，不扩大执行权限</strong><p>不调用 POST，不接入真实 HCU，不开放 Agent/Apex、Signoff 或发布。</p></div>
                    <span className="mono">formal_signoff_allowed=false</span>
                  </section>
                </div>
              </div>
            </div>
          )}
        </div>

        <footer className="plan-workspace-footer">
          <span><Info size={17} />Authority 来自 GET /v1/operator/search-rounds/{"{round_id}"}/candidate-evidence</span>
          <span className="mono">{workspace ? `snapshot ${formatDateTime(workspace.generated_at)}` : "read-only"}</span>
          <button className="outline-button start-export-button" type="button" disabled={!workspace || loading} onClick={() => downloadWorkspace(workspace)}>
            <DownloadSimple size={18} />导出证据 JSON
          </button>
        </footer>
      </section>
    </div>
  );
}

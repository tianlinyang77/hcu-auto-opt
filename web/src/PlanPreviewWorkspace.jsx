import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle,
  CircleNotch,
  ClipboardText,
  ClockCountdown,
  DownloadSimple,
  Flask,
  Gauge,
  Hash,
  Info,
  LockKey,
  MagnifyingGlass,
  Package,
  ShieldCheck,
  SlidersHorizontal,
  Target,
  WarningCircle,
  X,
} from "@phosphor-icons/react";

import { createRoundPlanPreview, loadOperatorHotspots } from "./api.js";
import {
  buildPlanPreviewRequest,
  createDemoPlanPreview,
  isPreviewExpired,
  validatePlanDraft,
} from "./plan-preview.js";

const steps = [
  [1, "选择目标", Target],
  [2, "冻结候选", Package],
  [3, "核对 Preview", ShieldCheck],
];

const checkMeta = {
  pass: ["通过", "pass", CheckCircle],
  warn: ["需确认", "warn", WarningCircle],
  block: ["阻塞", "block", LockKey],
};

const checkTitles = {
  operator_service_identity_current: "服务身份与当前部署一致",
  operator_profiles_verified: "Profile 版本与 Hash 已验证",
  operator_profile_deprecated: "Profile 已弃用，需要明确确认",
  operator_authority_resolved: "Target、Stage 0、Baseline 与 Hotspot Authority 已解析",
  operator_authority_unavailable: "匹配的冻结 Authority 不可用",
  operator_candidate_packages_verified: "Candidate Package 已审核并绑定热点",
  operator_candidate_package_invalid: "Candidate Package 校验未通过",
  operator_candidate_id_conflict: "Candidate 标识已被其他工作流占用",
};

function itemKey(item) {
  if (!item) return "";
  return `${item.profile_id}@${item.profile_version}`;
}

function shortHash(value, length = 12) {
  if (!value) return "—";
  return String(value).replace("sha256:", "").slice(0, length);
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function secondsLabel(value) {
  if (value >= 3600) return `${Math.round(value / 360) / 10} 小时`;
  if (value >= 60) return `${Math.round(value / 6) / 10} 分钟`;
  return `${value} 秒`;
}

function downloadPreview(preview) {
  const blob = new Blob([`${JSON.stringify(preview, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `operator-plan-preview-${shortHash(preview.resolved_plan_hash, 8)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function WizardStepper({ step }) {
  return (
    <ol className="wizard-stepper" aria-label="Plan Preview 步骤">
      {steps.map(([number, label, Icon]) => (
        <li
          className={`${number < step ? "complete" : ""} ${number === step ? "current" : ""}`}
          key={number}
        >
          <span>{number < step ? <Check size={17} weight="bold" /> : <Icon size={18} />}</span>
          <div>
            <small>STEP {number}</small>
            <strong>{label}</strong>
          </div>
        </li>
      ))}
    </ol>
  );
}

function ProfileField({ label, icon: Icon, value, onChange, items, detail }) {
  return (
    <label className="plan-field">
      <span className="plan-field-label"><Icon size={18} />{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {items.map((item) => (
          <option key={itemKey(item)} value={itemKey(item)}>
            {item.display_name} · v{item.profile_version}
          </option>
        ))}
      </select>
      <small>{detail}</small>
    </label>
  );
}

function ScopeStep({
  targetProfiles,
  measurementProfiles,
  workloads,
  targetKey,
  measurementKey,
  workloadKey,
  onTarget,
  onMeasurement,
  onWorkload,
  hotspots,
  hotspotId,
  onHotspot,
  hotspotLoading,
  hotspotError,
  onContinue,
}) {
  return (
    <div className="wizard-layout">
      <section className="wizard-card profile-card">
        <div className="wizard-card-head">
          <div><span>01 / Authority</span><h2>选择运行边界</h2></div>
          <LockKey size={23} />
        </div>
        <p className="wizard-card-intro">
          Profile 只引用已经注册的版本和 Hash。UI 不复制 Target Lock、Stage 0 或预算事实。
        </p>
        <div className="plan-fields">
          <ProfileField
            label="Target Profile"
            icon={Target}
            value={targetKey}
            onChange={onTarget}
            items={targetProfiles}
            detail="固定目标环境、资源策略与 Candidate Package Store。"
          />
          <label className="plan-field">
            <span className="plan-field-label"><Flask size={18} />Workload</span>
            <select value={workloadKey} onChange={(event) => onWorkload(event.target.value)}>
              {workloads.map((item) => (
                <option key={itemKey(item.profile)} value={itemKey(item.profile)}>
                  {item.display_name} · v{item.profile.profile_version}
                </option>
              ))}
            </select>
            <small>绑定 workload、配置、数据集与热点作用域。</small>
          </label>
          <ProfileField
            label="Measurement Profile"
            icon={Gauge}
            value={measurementKey}
            onChange={onMeasurement}
            items={measurementProfiles}
            detail="Search、Holdout、选择规则和预算只读继承。"
          />
        </div>
      </section>

      <section className="wizard-card hotspot-card">
        <div className="wizard-card-head">
          <div><span>02 / Hotspot</span><h2>选择权威替换点</h2></div>
          <MagnifyingGlass size={23} />
        </div>
        <p className="wizard-card-intro">
          这里只列出与所选 Target 和 Workload 同时匹配的权威热点。
        </p>
        {hotspotLoading ? (
          <div className="plan-inline-state"><CircleNotch className="spin" size={24} />正在读取 Hotspot Authority…</div>
        ) : hotspotError ? (
          <div className="plan-inline-state danger"><WarningCircle size={24} />{hotspotError}</div>
        ) : hotspots.length ? (
          <div className="hotspot-options" role="radiogroup" aria-label="选择 Hotspot">
            {hotspots.map((item) => {
              const selected = item.hotspot.hotspot_id === hotspotId;
              return (
                <button
                  className={`hotspot-option ${selected ? "selected" : ""}`}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  key={item.hotspot.hotspot_id}
                  onClick={() => onHotspot(item.hotspot.hotspot_id)}
                >
                  <span className="hotspot-radio">{selected && <Check size={15} weight="bold" />}</span>
                  <div>
                    <strong>{item.symbol}</strong>
                    <span className="mono">{item.hotspot.shape.join(" × ")} · {item.hotspot.dtype}</span>
                    <small>{item.patchability} · Candidate {item.candidate_packages.length} 个</small>
                  </div>
                  <dl>
                    <div><dt>热点占比</dt><dd>{(item.share_ratio * 100).toFixed(1)}%</dd></div>
                    <div><dt>机会分</dt><dd>{item.opportunity_score.toFixed(2)}</dd></div>
                  </dl>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="plan-inline-state"><Info size={24} />当前组合没有可用 Hotspot。</div>
        )}
        <div className="wizard-actions right">
          <button className="primary-button" type="button" disabled={!hotspotId} onClick={onContinue}>
            选择 Candidate Family <ArrowRight size={18} />
          </button>
        </div>
      </section>
    </div>
  );
}

function CandidateStep({
  hotspot,
  selectedIds,
  onToggle,
  maxPromoted,
  onMaxPromoted,
  measurement,
  errors,
  onBack,
  onPreview,
  submitting,
}) {
  const packages = hotspot?.candidate_packages || [];
  const budget = measurement?.authority_refs?.budget;
  return (
    <div className="wizard-layout candidate-layout">
      <section className="wizard-card candidate-picker-card">
        <div className="wizard-card-head">
          <div><span>03 / Candidate Intake</span><h2>冻结 Candidate Family</h2></div>
          <Package size={23} />
        </div>
        <p className="wizard-card-intro">
          选择 2–4 个已经审核的 Package。Preview 后输入集合 Hash 固定，不能静默增删。
        </p>
        <div className="candidate-pick-list">
          {packages.map((item, index) => {
            const selected = selectedIds.includes(item.candidate_id);
            const atLimit = selectedIds.length >= 4 && !selected;
            return (
              <button
                className={`candidate-pick ${selected ? "selected" : ""}`}
                type="button"
                aria-pressed={selected}
                disabled={atLimit}
                key={item.candidate_id}
                onClick={() => onToggle(item.candidate_id)}
              >
                <span className="candidate-check">{selected && <Check size={16} weight="bold" />}</span>
                <span className="candidate-ordinal mono">M2-{String(index + 1).padStart(2, "0")}</span>
                <span className="candidate-pick-main">
                  <strong>{item.suggested_optimization_intent}</strong>
                  <small>{item.replacement_path}</small>
                </span>
                <span className="candidate-review">
                  <ShieldCheck size={16} />{item.reviewed_by}
                  <small>{formatDateTime(item.reviewed_at)}</small>
                </span>
                <span className="candidate-package-hash mono">{shortHash(item.source_package_ref.source_package_hash)}</span>
              </button>
            );
          })}
        </div>
        {errors.length > 0 && (
          <div className="draft-errors"><WarningCircle size={19} />{errors.join("；")}</div>
        )}
      </section>

      <aside className="wizard-card budget-card">
        <div className="wizard-card-head compact">
          <div><span>只读预算</span><h2>本轮上限</h2></div>
          <Gauge size={23} />
        </div>
        <label className="plan-field promotion-field">
          <span className="plan-field-label"><SlidersHorizontal size={18} />最多晋级</span>
          <select value={maxPromoted} onChange={(event) => onMaxPromoted(Number(event.target.value))}>
            {Array.from({ length: Math.max(1, Math.min(2, selectedIds.length)) }, (_, index) => index + 1).map((value) => (
              <option key={value} value={value}>{value} 个候选</option>
            ))}
          </select>
          <small>只决定 Search 后最多进入 Holdout 的数量。</small>
        </label>
        {budget && (
          <dl className="budget-list">
            <div><dt>候选数量</dt><dd>{selectedIds.length} / 4</dd></div>
            <div><dt>Build 尝试</dt><dd>{budget.max_build_attempts}</dd></div>
            <div><dt>正确性尝试</dt><dd>{budget.max_correctness_attempts}</dd></div>
            <div><dt>Search 样本</dt><dd>{budget.max_search_samples}</dd></div>
            <div><dt>Holdout 样本</dt><dd>{budget.max_holdout_samples}</dd></div>
            <div><dt>墙钟上限</dt><dd>{secondsLabel(budget.max_wall_seconds)}</dd></div>
          </dl>
        )}
        <div className="budget-boundary">
          <LockKey size={20} />
          <div><strong>UI 不可修改预算</strong><span>预算来自 Measurement Profile，后端在 Preview 时按候选数量解析。</span></div>
        </div>
      </aside>

      <div className="wizard-actions spread full-width">
        <button className="outline-button" type="button" onClick={onBack}><ArrowLeft size={18} />返回目标</button>
        <button className="primary-button" type="button" disabled={errors.length > 0 || submitting} onClick={onPreview}>
          {submitting ? <CircleNotch className="spin" size={18} /> : <ClipboardText size={18} />}
          {submitting ? "正在编译 Preview…" : "生成 Plan Preview"}
        </button>
      </div>
    </div>
  );
}

function PreviewStep({ preview, error, onBack, onRetry, onClose }) {
  if (error) {
    return (
      <section className="preview-error-state">
        <WarningCircle size={44} />
        <span>Preview API 未形成 Authority</span>
        <h2>计划预览失败</h2>
        <p>{error}</p>
        <div className="wizard-actions center">
          <button className="outline-button" type="button" onClick={onBack}><ArrowLeft size={18} />返回修改</button>
          <button className="primary-button" type="button" onClick={onRetry}>重新请求 Preview</button>
        </div>
      </section>
    );
  }
  if (!preview) return null;

  const expired = isPreviewExpired(preview);
  const state = expired ? "expired" : preview.start_allowed ? "ready" : "blocked";
  const stateCopy = {
    ready: ["Preflight 已通过", "该 Preview 满足后端 Start 前置条件，但 UI-1 不执行 Start。", CheckCircle],
    blocked: ["存在阻塞项", "必须处理所有 block 后重新生成 Preview。", LockKey],
    expired: ["Preview 已过期", "不可使用旧 Hash 继续执行，请返回并重新生成。", ClockCountdown],
  }[state];
  const StateIcon = stateCopy[2];
  const budget = preview.resolved_plan.budget;

  return (
    <div className="preview-layout">
      <section className={`preview-status ${state}`}>
        <StateIcon size={34} weight={state === "ready" ? "fill" : "regular"} />
        <div><span>Operator Authority</span><h2>{stateCopy[0]}</h2><p>{stateCopy[1]}</p></div>
        <div className="preview-expiry"><small>有效期至</small><strong className="mono">{formatDateTime(preview.expires_at)}</strong></div>
      </section>

      <section className="wizard-card preflight-card">
        <div className="wizard-card-head compact">
          <div><span>04 / Preflight</span><h2>后端检查结果</h2></div>
          <ShieldCheck size={23} />
        </div>
        <div className="preflight-list">
          {preview.checks.map((check) => {
            const [label, tone, Icon] = checkMeta[check.status];
            return (
              <div className={`preflight-row ${tone}`} key={check.code}>
                <Icon size={21} weight={check.status === "pass" ? "fill" : "regular"} />
                <div>
                  <strong>{checkTitles[check.code] || check.message}</strong>
                  <small>{check.message}</small>
                  <span className="mono">{check.code} · {check.scope}</span>
                </div>
                <span>{label}</span>
              </div>
            );
          })}
        </div>
      </section>

      <aside className="wizard-card preview-facts-card">
        <div className="wizard-card-head compact">
          <div><span>不可变引用</span><h2>Resolved Plan</h2></div>
          <Hash size={23} />
        </div>
        <dl className="preview-facts">
          <div><dt>Preview ID</dt><dd className="mono">{preview.preview_id}</dd></div>
          <div><dt>Request Digest</dt><dd className="mono">{shortHash(preview.preview_request_digest, 20)}</dd></div>
          <div><dt>Resolved Plan Hash</dt><dd className="mono emphasis">{shortHash(preview.resolved_plan_hash, 20)}</dd></div>
          <div><dt>Candidate Set Hash</dt><dd className="mono">{shortHash(preview.resolved_plan.candidate_input_set_hash, 20)}</dd></div>
          <div><dt>候选 / 晋级</dt><dd>{budget.max_candidates} / {preview.resolved_plan.max_promoted}</dd></div>
          <div><dt>Search / Holdout</dt><dd>{budget.max_search_samples} / {budget.max_holdout_samples} samples</dd></div>
          <div><dt>结论边界</dt><dd>{preview.resolved_plan.conclusion_boundary}</dd></div>
          <div><dt>自动发布</dt><dd className="locked-fact">false</dd></div>
        </dl>
        <button className="export-button" type="button" onClick={() => downloadPreview(preview)}>
          <DownloadSimple size={18} />导出 Preview JSON
        </button>
      </aside>

      <section className="preview-no-start full-width">
        <LockKey size={22} />
        <div><strong>UI-1 到此为止</strong><span>没有调用 <span className="mono">:start</span>，没有创建 Round、申请 HCU、签核或发布。</span></div>
      </section>

      <div className="wizard-actions spread full-width">
        <button className="outline-button" type="button" onClick={onBack}><ArrowLeft size={18} />返回 Candidate</button>
        <button className="primary-button" type="button" onClick={onClose}>完成核对并关闭</button>
      </div>
    </div>
  );
}

export function PlanPreviewWorkspace({ dashboard, demoMode, onClose }) {
  const targetProfiles = dashboard.profiles.filter((item) => item.profile_kind === "target");
  const measurementProfiles = dashboard.profiles.filter((item) => item.profile_kind === "measurement");
  const [step, setStep] = useState(1);
  const [targetKey, setTargetKey] = useState(itemKey(targetProfiles[0]));
  const [measurementKey, setMeasurementKey] = useState(itemKey(measurementProfiles[0]));
  const [workloadKey, setWorkloadKey] = useState(itemKey(dashboard.workloads[0]?.profile));
  const [hotspots, setHotspots] = useState(dashboard.hotspots);
  const [hotspotId, setHotspotId] = useState(dashboard.hotspots[0]?.hotspot.hotspot_id || "");
  const [hotspotLoading, setHotspotLoading] = useState(false);
  const [hotspotError, setHotspotError] = useState(null);
  const [selectedIds, setSelectedIds] = useState(
    dashboard.hotspots[0]?.candidate_packages.slice(0, 2).map((item) => item.candidate_id) || [],
  );
  const [maxPromoted, setMaxPromoted] = useState(1);
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [name] = useState("UI-1 Scripted Plan Preview");
  const bodyRef = useRef(null);

  const target = useMemo(
    () => targetProfiles.find((item) => itemKey(item) === targetKey),
    [targetKey, targetProfiles],
  );
  const measurement = useMemo(
    () => measurementProfiles.find((item) => itemKey(item) === measurementKey),
    [measurementKey, measurementProfiles],
  );
  const workload = useMemo(
    () => dashboard.workloads.find((item) => itemKey(item.profile) === workloadKey),
    [dashboard.workloads, workloadKey],
  );
  const hotspot = hotspots.find((item) => item.hotspot.hotspot_id === hotspotId);
  const selectedCandidates = (hotspot?.candidate_packages || []).filter((item) => selectedIds.includes(item.candidate_id));
  const draftErrors = validatePlanDraft({
    target,
    workload,
    measurement,
    hotspot,
    candidates: selectedCandidates,
    maxPromoted,
  });
  const scenario = new URLSearchParams(window.location.search).get("preview") || "pass";

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [step]);

  const resetForHotspots = (items) => {
    const nextHotspot = items[0];
    setHotspots(items);
    setHotspotId(nextHotspot?.hotspot.hotspot_id || "");
    setSelectedIds(nextHotspot?.candidate_packages.slice(0, 2).map((item) => item.candidate_id) || []);
    setMaxPromoted(1);
    setPreview(null);
    setPreviewError(null);
  };

  const refreshHotspots = async (nextTarget, nextWorkload) => {
    setHotspotError(null);
    if (demoMode) {
      resetForHotspots(dashboard.hotspots);
      return;
    }
    setHotspotLoading(true);
    try {
      resetForHotspots(await loadOperatorHotspots(nextTarget, nextWorkload));
    } catch (error) {
      resetForHotspots([]);
      setHotspotError(error instanceof Error ? error.message : String(error));
    } finally {
      setHotspotLoading(false);
    }
  };

  const changeTarget = (nextKey) => {
    setTargetKey(nextKey);
    const nextTarget = targetProfiles.find((item) => itemKey(item) === nextKey);
    if (nextTarget && workload) refreshHotspots(nextTarget, workload);
  };

  const changeWorkload = (nextKey) => {
    setWorkloadKey(nextKey);
    const nextWorkload = dashboard.workloads.find((item) => itemKey(item.profile) === nextKey);
    if (target && nextWorkload) refreshHotspots(target, nextWorkload);
  };

  const changeHotspot = (nextId) => {
    const nextHotspot = hotspots.find((item) => item.hotspot.hotspot_id === nextId);
    setHotspotId(nextId);
    setSelectedIds(nextHotspot?.candidate_packages.slice(0, 2).map((item) => item.candidate_id) || []);
    setMaxPromoted(1);
    setPreview(null);
    setPreviewError(null);
  };

  const toggleCandidate = (candidateId) => {
    setSelectedIds((current) => current.includes(candidateId)
      ? current.filter((item) => item !== candidateId)
      : [...current, candidateId].slice(0, 4));
    setPreview(null);
    setPreviewError(null);
  };

  const requestPreview = async () => {
    setSubmitting(true);
    setPreviewError(null);
    try {
      const idempotencyKey = `ui1-preview-${typeof crypto.randomUUID === "function" ? crypto.randomUUID() : Date.now()}`;
      const request = buildPlanPreviewRequest({
        name,
        identity: dashboard.identity,
        target,
        workload,
        measurement,
        hotspot,
        candidates: selectedCandidates,
        maxPromoted,
        idempotencyKey,
      });
      if (demoMode && scenario === "error") {
        throw new Error("operator_demo_preview_unavailable: Synthetic Preview API error fixture");
      }
      const result = demoMode
        ? createDemoPlanPreview(request, measurement, scenario)
        : await createRoundPlanPreview(request);
      setPreview(result);
      setStep(3);
    } catch (error) {
      setPreview(null);
      setPreviewError(error instanceof Error ? error.message : String(error));
      setStep(3);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="plan-workspace-backdrop">
      <section className="plan-workspace" role="dialog" aria-modal="true" aria-labelledby="plan-workspace-title">
        <header className="plan-workspace-header">
          <div className="plan-workspace-title">
            <span className="plan-icon"><ClipboardText size={23} /></span>
            <div><span>Operator Experience · UI-1</span><h1 id="plan-workspace-title">启动前 Plan Preview</h1></div>
          </div>
          <div className="plan-workspace-meta">
            <span className="synthetic-badge">Synthetic · Preview only</span>
            <span className="source-chip demo">{demoMode ? `Demo · ${scenario}` : "Operator API"}</span>
            <button className="icon-button" type="button" aria-label="关闭 Plan Preview" onClick={onClose}><X size={21} /></button>
          </div>
        </header>
        <WizardStepper step={step} />
        <div className="plan-workspace-body" ref={bodyRef}>
          {step === 1 && (
            <ScopeStep
              targetProfiles={targetProfiles}
              measurementProfiles={measurementProfiles}
              workloads={dashboard.workloads}
              targetKey={targetKey}
              measurementKey={measurementKey}
              workloadKey={workloadKey}
              onTarget={changeTarget}
              onMeasurement={setMeasurementKey}
              onWorkload={changeWorkload}
              hotspots={hotspots}
              hotspotId={hotspotId}
              onHotspot={changeHotspot}
              hotspotLoading={hotspotLoading}
              hotspotError={hotspotError}
              onContinue={() => setStep(2)}
            />
          )}
          {step === 2 && (
            <CandidateStep
              hotspot={hotspot}
              selectedIds={selectedIds}
              onToggle={toggleCandidate}
              maxPromoted={maxPromoted}
              onMaxPromoted={setMaxPromoted}
              measurement={measurement}
              errors={draftErrors}
              onBack={() => setStep(1)}
              onPreview={requestPreview}
              submitting={submitting}
            />
          )}
          {step === 3 && (
            <PreviewStep
              preview={preview}
              error={previewError}
              onBack={() => setStep(2)}
              onRetry={requestPreview}
              onClose={onClose}
            />
          )}
        </div>
        <footer className="plan-workspace-footer">
          <Info size={17} />
          <span>UI 只组装权威引用并展示返回值；不会计算 Plan Hash、Preflight、预算、裁决或发布结论。</span>
          <span className="mono">automatic_release_allowed=false</span>
        </footer>
      </section>
    </div>
  );
}

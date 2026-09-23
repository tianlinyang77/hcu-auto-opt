// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

async function request(path, options = {}, fetcher = fetch) {
  const response = await fetcher(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let detail = {};
    try { detail = await response.json(); } catch { /* stable fallback below */ }
    const error = new Error(detail.detail || detail.message || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export function loadManualCandidateSummary(taskId, fetcher = fetch) {
  if (!uuid.test(taskId)) throw new Error("无效的 M1 Task ID");
  return request(`/v1/manual-candidate/tasks/${taskId}/summary`, {}, fetcher);
}

export function adjudicationResult(summary) {
  const job = summary?.jobs?.find((item) => item.job_type === "manual_adjudicate" && item.state === "succeeded");
  const result = job?.result;
  if (!result?.evaluation || !result?.evidence || result.synthetic !== false) {
    throw new Error("正式裁决证据不完整，不能形成人工签核请求");
  }
  return result;
}

export function formatEvidencePercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未提供";
  return `${(value * 100).toFixed(2)}%`;
}

export function credibleThresholdStatus(metrics) {
  const lowerBound = metrics?.confidence_interval?.[0];
  const threshold = metrics?.credible_threshold;
  if (typeof lowerBound !== "number" || !Number.isFinite(lowerBound) ||
      typeof threshold !== "number" || !Number.isFinite(threshold)) {
    return "证据未提供，无法判断";
  }
  return lowerBound > threshold ? "置信区间下界高于可信门限" : "未达到可信门限";
}

export function measurementFacts(result) {
  const measurement = result?.evaluation?.measurement;
  if (!measurement) return [];
  return [
    ["测量协议", measurement.protocol_version],
    ["样本数", measurement.sample_count],
    ["预热数", measurement.warmup_count],
    ["进程重启数", measurement.process_restart_count],
    ["环境指纹", measurement.environment_fingerprint],
  ];
}

export function agentOrigin(summary) {
  if (summary?.task?.adapter_profile !== "bw20-m1-manual-v1") return null;
  const events = (summary.events || []).filter((event) =>
    event.event_type === "manual_candidate_agent_origin_recorded" &&
    event.details?.candidate_id === summary.candidate?.candidate_id);
  if (events.length !== 1) return { valid: false, reason: "Agent 来源记录缺失或重复" };
  const origin = events[0].details;
  const sha256 = /^sha256:[0-9a-f]{64}$/;
  const requiredHashes = [
    origin.review_record_hash,
    origin.proposal_review_evidence_hash,
    origin.generation_review_evidence_hash,
    origin.patch_hash,
    origin.candidate_source_hash,
    origin.source_package_hash,
    origin.manifest_hash,
  ];
  const requiredIds = [origin.generation_run_id, origin.proposal_id, origin.review_id];
  const valid = origin.schema_version === "bw20-agent-m1-origin-v1" &&
    origin.decision === "approved" && origin.synthetic === false &&
    origin.automatic_release_allowed === false &&
    origin.candidate_id === summary.candidate?.candidate_id &&
    origin.candidate_source_hash === summary.candidate?.source_hash &&
    requiredHashes.every((value) => typeof value === "string" && sha256.test(value)) &&
    requiredIds.every((value) => typeof value === "string" && uuid.test(value)) &&
    [origin.proposal_review_evidence_uri, origin.generation_review_evidence_uri]
      .every((value) => typeof value === "string" && value.length > 0);
  return valid ? { valid: true, ...origin } : {
    valid: false,
    reason: "Agent 来源与当前 Candidate、Source Hash 或证据哈希不匹配",
  };
}

export function freezeManualSignoff(summary, fields, idempotencyKey) {
  const candidate = summary?.candidate;
  const task = summary?.task;
  const result = adjudicationResult(summary);
  const evidenceId = result.evidence.evidence_id;
  if (task?.state !== "awaiting_signoff" || candidate?.state !== "awaiting_signoff") {
    throw new Error("Task 或 Candidate 当前不在等待签核状态");
  }
  if (fields.decision === "approved" &&
      (result.evaluation.metrics?.correctness_verdict !== "correct" ||
       result.evaluation.metrics?.verdict === "invalid")) {
    throw new Error("正确性未通过或 D 判决无效，不能批准为有效 M1 证据");
  }
  const origin = agentOrigin(summary);
  if (fields.decision === "approved" && origin && !origin.valid) {
    throw new Error(`BW20 M1 缺少有效 Agent 来源链：${origin.reason}`);
  }
  if (task.automatic_release_allowed !== false || result.evidence.synthetic !== false ||
      result.evidence.candidate_id !== candidate.candidate_id ||
      evidenceId !== candidate.evidence_bundle_id) {
    throw new Error("EvidenceBundle 身份或发布边界不匹配");
  }
  if (!["approved", "rejected"].includes(fields.decision)) throw new Error("签核决定无效");
  const actor = fields.actor.trim();
  const reason = fields.reason.trim();
  if (!actor || !reason) throw new Error("签署人和签核理由不能为空");
  if (!idempotencyKey || idempotencyKey.length < 8) throw new Error("签核请求编号无效");
  return Object.freeze({
    taskId: task.task_id,
    candidateId: candidate.candidate_id,
    taskVersion: task.version,
    payload: Object.freeze({
      decision: fields.decision,
      actor,
      reason,
      evidence_bundle_id: evidenceId,
      idempotency_key: idempotencyKey,
    }),
  });
}

export function submitManualSignoff(intent, fetcher = fetch) {
  return request(`/v1/manual-candidate/tasks/${intent.taskId}/signoff`, {
    method: "POST",
    body: JSON.stringify(intent.payload),
  }, fetcher);
}

export function signoffMatches(intent, receipt) {
  return receipt?.task_id === intent.taskId &&
    receipt?.candidate_id === intent.candidateId &&
    receipt?.decision === intent.payload.decision &&
    receipt?.actor === intent.payload.actor &&
    receipt?.reason === intent.payload.reason &&
    receipt?.evidence_bundle_id === intent.payload.evidence_bundle_id &&
    receipt?.idempotency_key === intent.payload.idempotency_key;
}

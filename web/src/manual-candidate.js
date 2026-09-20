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

export function freezeManualSignoff(summary, fields, idempotencyKey) {
  const candidate = summary?.candidate;
  const task = summary?.task;
  const result = adjudicationResult(summary);
  const evidenceId = result.evidence.evidence_id;
  if (task?.state !== "awaiting_signoff" || candidate?.state !== "awaiting_signoff") {
    throw new Error("Task 或 Candidate 当前不在等待签核状态");
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

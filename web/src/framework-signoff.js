// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

const uuid = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

function endpoint(taskId) {
  if (!uuid.test(taskId)) throw new Error("任务编号无效。");
  return `/v1/framework-smoke/tasks/${taskId}/signoff`;
}

function headers(credential) {
  if (typeof credential !== "string" || !/^[\x21-\x7e]{32,256}$/.test(credential)) {
    throw new Error("请输入独立的签核凭据。");
  }
  return { Accept: "application/json", Authorization: `Bearer ${credential}` };
}

export async function loadSigningStatus(taskId, credential, fetcher = fetch) {
  const response = await fetcher(endpoint(taskId), {
    headers: headers(credential), cache: "no-store", redirect: "error",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(response.status === 403 ?
    "签核凭据不正确、已过期或已撤销，请重新输入有效签核凭据。" : "无法确认签核状态，请稍后查询。");
  const data = await response.json();
  if (data.task_id !== taskId || typeof data.actor !== "string" || !data.actor.trim() ||
      data.automatic_release_allowed !== false || !Number.isFinite(Date.parse(data.expires_at)) ||
      (data.signoff !== null && (!data.signoff || data.signoff.task_id !== taskId))) {
    throw new Error("签核身份或任务不匹配，已停止操作。");
  }
  return data;
}

export function freezeSignoff(data, identity, decision, reason, key = crypto.randomUUID()) {
  if (data.write_actions_available !== true || data.ready_for_human_review !== true ||
      data.task?.state !== "awaiting_signoff" || identity.task_id !== data.task.task_id ||
      identity.signoff !== null || !Number.isFinite(Date.parse(identity.expires_at)) ||
      Date.parse(identity.expires_at) <= Date.now() ||
      typeof identity.actor !== "string" || !identity.actor.trim() ||
      data.performance_conclusion !== "not_measured" || data.automatic_release_allowed !== false ||
      !uuid.test(data.review_evidence_id) || !uuid.test(data.evaluation_id) ||
      !["approved", "rejected"].includes(decision) || !reason.trim() || reason.trim().length > 2000 ||
      !uuid.test(key)) throw new Error("当前结果或签核信息不完整，请重新读取证据。");
  return Object.freeze({ taskId: data.task.task_id, evaluationId: data.evaluation_id,
    payload: Object.freeze({ decision, actor: identity.actor, reason: reason.trim(),
      evidence_bundle_id: data.review_evidence_id, idempotency_key: key }) });
}

export function matchesSignoff(intent, receipt) {
  return receipt?.task_id === intent.taskId && uuid.test(receipt.signoff_id) &&
    receipt.task_state === (intent.payload.decision === "approved" ? "completed" : "rejected") &&
    Object.entries(intent.payload).every(([key, value]) => receipt[key] === value);
}

export async function submitSignoff(intent, credential, fetcher = fetch) {
  // Caller retains the frozen intent on ANY unknown outcome; never create a new key here.
  const response = await fetcher(endpoint(intent.taskId), {
    method: "POST", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000),
    headers: { ...headers(credential), "Content-Type": "application/json" },
    body: JSON.stringify(intent.payload),
  });
  if (!response.ok) {
    const error = new Error(response.status === 409 ?
      "任务或证据已变化，不能自动改绑证据；请先查询签核结果。" :
      "提交未确认，请先查询签核结果；重试会保持原证据和请求编号。");
    error.status = response.status;
    throw error;
  }
  const result = await response.json();
  if (!matchesSignoff(intent, result)) throw new Error("返回结果与本次确认不匹配，请查询签核状态。");
  return result;
}

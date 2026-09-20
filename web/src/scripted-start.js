// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
const sessions = new Map();
const identityFields = ["source_commit", "control_contract_version", "operator_contract_version",
  "profile_catalog_hash", "server_instance_id"];
const storageKey = "hcuopt-scripted-start-v1";

export function savedStartRequests(storage = globalThis.sessionStorage) {
  if (!storage) return [];
  const raw = storage.getItem(storageKey);
  if (!raw) return [];
  const rows = JSON.parse(raw);
  if (!Array.isArray(rows) || rows.some((row) => !row || typeof row.preview_id !== "string" ||
      typeof row.idempotency_key !== "string" || !row.expected_service_identity)) {
    throw new Error("启动恢复记录损坏，请先核查已有请求，不能清空记录后重新提交");
  }
  return rows;
}

export function saveStartRequest(request, storage = globalThis.sessionStorage) {
  if (!storage) {
    if (typeof window !== "undefined") throw new Error("浏览器无法保存恢复记录，启动未发送");
    return;
  }
  const rows = savedStartRequests(storage);
  const previous = rows.find((row) => row.preview_id === request.preview_id);
  if (previous && JSON.stringify(previous) !== JSON.stringify(request)) {
    throw new Error("此计划已有不同的冻结请求，请恢复原请求");
  }
  if (!previous) rows.push(request);
  storage.setItem(storageKey, JSON.stringify(rows));
}

export function freezeScriptedStart(preview, actor, acknowledgements, key, now = Date.now()) {
  if (preview?.synthetic !== true || preview?.automatic_release_allowed !== false ||
      preview?.resolved_plan?.run_mode !== "scripted" ||
      preview?.resolved_plan?.synthetic !== true ||
      preview?.resolved_plan?.automatic_release_allowed !== false) {
    throw new Error("此入口只允许 Scripted 演练计划");
  }
  if (!preview.start_allowed || !(Date.parse(preview.expires_at) > now)) {
    throw new Error("计划被阻塞或已过期，请重新预览");
  }
  const required = [...preview.required_ack_codes].sort();
  const confirmed = [...new Set(acknowledgements)].sort();
  if (JSON.stringify(required) !== JSON.stringify(confirmed)) throw new Error("请逐项确认警告");
  if (!actor.trim() || actor.trim().length > 200) throw new Error("请填写操作者（最多 200 字）");
  if (!key || key.length < 8) throw new Error("缺少幂等请求编号");
  const identity = Object.fromEntries(identityFields.map((field) => [field, preview.service_identity?.[field]]));
  if (Object.values(identity).some((value) => !value)) throw new Error("服务身份不完整");
  return Object.freeze({preview_id: preview.preview_id, resolved_plan_hash: preview.resolved_plan_hash,
    actor: actor.trim(), idempotency_key: key,
    acknowledged_warning_codes: Object.freeze(confirmed),
    expected_service_identity: Object.freeze(identity)});
}

export function validateStartReceipt(receipt, request) {
  if (receipt?.preview_id !== request.preview_id || receipt?.resolved_plan_hash !== request.resolved_plan_hash ||
      receipt?.actor !== request.actor || receipt?.idempotency_key !== request.idempotency_key ||
      receipt?.synthetic !== true || receipt?.automatic_release_allowed !== false ||
      !receipt?.intent_id || !receipt?.round_id ||
      !["preparing", "plans_frozen", "round_created", "intake_closed", "finalized", "failed"].includes(receipt.state) ||
      identityFields.some((field) => receipt.service_identity?.[field] !== request.expected_service_identity[field])) {
    throw new Error("启动回执与冻结请求不匹配，请保留请求编号核查");
  }
  return receipt;
}

export function startSession(previewId) {
  if (!sessions.has(previewId)) {
    let request = null;
    let recoveryError = null;
    try { request = savedStartRequests().find((row) => row.preview_id === previewId) || null; }
    catch (error) { recoveryError = error; }
    sessions.set(previewId, {request, receipt: null, pending: null, recoveryError});
  }
  return sessions.get(previewId);
}

export async function submitScriptedStart(session, request, fetcher = fetch) {
  if (session.recoveryError) throw session.recoveryError;
  if (session.request && JSON.stringify(session.request) !== JSON.stringify(request)) {
    throw new Error("此计划已提交，不能更换请求身份");
  }
  saveStartRequest(request);
  session.request = request;
  if (session.receipt) return session.receipt;
  if (session.pending) return session.pending;
  session.pending = (async () => {
    const response = await fetcher(`/v1/operator/round-plans/${encodeURIComponent(request.preview_id)}:start`, {
      credentials: "omit", redirect: "error",
      method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
      body: JSON.stringify(request),
    });
    if (!response.ok) throw new Error(`启动返回 HTTP ${response.status}；保留原请求编号核查或重试`);
    session.receipt = validateStartReceipt(await response.json(), request);
    return session.receipt;
  })();
  try { return await session.pending; } finally { session.pending = null; }
}

// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
const HASH = /^sha256:[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FIELDS = ["preview_id", "resolved_plan_hash", "formal_authorization_hash", "execution_authority_hash", "evaluation_authority_hash", "idempotency_key", "expected_service_identity"];

export function freezeFormalSubmission(value) {
  if (!value || Object.keys(value).sort().join() !== [...FIELDS].sort().join() ||
      !UUID.test(value.preview_id) || typeof value.idempotency_key !== "string" ||
      value.idempotency_key.length < 8 || value.idempotency_key.length > 300 ||
      FIELDS.filter((key) => key.endsWith("_hash")).some((key) => !HASH.test(value[key]))) {
    throw new Error("正式计划引用不完整，已阻止提交。");
  }
  const identity = value.expected_service_identity;
  if (!identity || !UUID.test(identity.server_instance_id) || !HASH.test(identity.profile_catalog_hash) ||
      !/^[0-9a-f]{40}$/.test(identity.source_commit) || typeof identity.control_contract_version !== "string") {
    throw new Error("正式计划服务身份无效。");
  }
  return Object.freeze({ ...value, expected_service_identity: Object.freeze({ ...identity }) });
}

async function request(path, token, options, fetchImpl) {
  if (typeof token !== "string" || !token || /\s/.test(token)) throw new Error("请输入独立操作凭据。");
  let response;
  try {
    response = await fetchImpl(path, { ...options, credentials: "omit", redirect: "error", cache: "no-store",
      headers: { Authorization: `Bearer ${token}`, ...(options.body ? { "Content-Type": "application/json" } : {}) } });
  } catch { throw new Error(options.method === "GET" ? "状态读取失败，请稍后重试。" : "网络失败，提交结果可能未知。请使用同一凭据和原请求重试。"); }
  if (!response.ok) throw new Error(response.status === 403 ? "凭据无效、已撤销或已过期。" :
    response.status === 409 ? "授权或请求发生冲突，请核对原意图，不要另建请求。" :
    "正式管理入口不可用或请求被拒绝；不会转用演练入口。");
  try { return await response.json(); } catch { throw new Error("回执无法解析，请核对原意图。"); }
}

export async function loadFormalSubmission(token, fetchImpl = fetch) {
  return freezeFormalSubmission(await request("/v1/operator/formal-start-submission", token, { method: "GET" }, fetchImpl));
}

export async function submitFormalIntent(submission, token, fetchImpl = fetch) {
  const frozen = freezeFormalSubmission(submission);
  const receipt = await request("/v1/operator/formal-start-intents", token,
    { method: "POST", body: JSON.stringify(frozen) }, fetchImpl);
  const identity = frozen.expected_service_identity;
  if (!receipt || !UUID.test(receipt.intent_id) || receipt.preview_id !== frozen.preview_id ||
      receipt.idempotency_key !== frozen.idempotency_key ||
      FIELDS.filter((key) => key.endsWith("_hash")).some((key) => receipt[key] !== frozen[key]) ||
      Object.keys(identity).some((key) => receipt.service_identity?.[key] !== identity[key]) ||
      receipt.round_creation_allowed !== false || receipt.hcu_accessed !== false ||
      receipt.automatic_release_allowed !== false || receipt.synthetic !== false ||
      !["awaiting_authority", "ready_for_round_creation", "failed", "cancelled"].includes(receipt.state)) {
    throw new Error("回执身份或执行边界不匹配，不能视为受理成功。");
  }
  return receipt;
}

export async function loadFormalDispatch(submission, receipt, token, fetchImpl = fetch) {
  const frozen = freezeFormalSubmission(submission);
  const status = await request("/v1/operator/formal-round-dispatch", token, { method: "GET" }, fetchImpl);
  if (!status || status.schema_version !== "formal-dispatch-status-v1" ||
      status.intent_id !== receipt.intent_id || !UUID.test(status.round_id) ||
      status.round_id !== receipt.round_id || status.resolved_plan_hash !== frozen.resolved_plan_hash ||
      Object.keys(frozen.expected_service_identity).some((key) => status.service_identity?.[key] !== frozen.expected_service_identity[key]) ||
      !["not_created", "queued", "cancelled"].includes(status.state) ||
      status.execution_consumer_enabled !== false || status.automatic_release_allowed !== false) {
    throw new Error("派发状态与原请求不匹配，不能推断执行进度。");
  }
  return status;
}

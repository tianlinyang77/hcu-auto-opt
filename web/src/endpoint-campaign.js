// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const sha256 = /^sha256:[0-9a-f]{64}$/;

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
    const message = detail.detail || detail.message || `请求失败（HTTP ${response.status}）`;
    throw new Error(message);
  }
  return response.json();
}

export function loadEndpointCampaignSummary(campaignId, fetcher = fetch) {
  if (!uuid.test(campaignId)) throw new Error("无效的 Endpoint Campaign ID");
  return request(
    `/v1/endpoint-validation-campaigns/${campaignId}/summary`,
    {},
    fetcher,
  );
}

export function validateEndpointCampaignSummary(summary, campaignId) {
  const campaign = summary?.campaign;
  if (campaign?.campaign_id !== campaignId || campaign.automatic_release_allowed !== false ||
      summary.automatic_release_allowed !== false ||
      !Array.isArray(campaign.endpoint_run_ids) || campaign.endpoint_run_ids.length !== 8) {
    throw new Error("Campaign 身份、组数或发布边界不完整");
  }
  if (campaign.adjudication_result) {
    const result = campaign.adjudication_result;
    if (result.campaign_id !== campaignId || result.formal_d_adjudication !== true ||
        result.automatic_release_allowed !== false ||
        !sha256.test(summary.adjudication_result_sha256 || "")) {
      throw new Error("正式 D 结果或结果 Hash 不完整");
    }
  }
  return summary;
}

export function freezeEndpointCampaignSignoff(summary, fields, idempotencyKey) {
  const campaign = summary?.campaign;
  const result = campaign?.adjudication_result;
  if (campaign?.state !== "awaiting_signoff" || !result ||
      result.formal_d_adjudication !== true || result.automatic_release_allowed !== false ||
      !sha256.test(summary.adjudication_result_sha256 || "")) {
    throw new Error("Campaign 当前没有可签核的正式 D 结果");
  }
  if (!["accepted", "rejected"].includes(fields.decision)) {
    throw new Error("签核决定无效");
  }
  const actor = fields.actor.trim();
  const reason = fields.reason.trim();
  if (!actor || !reason) throw new Error("签署人和签核理由不能为空");
  if (!idempotencyKey || idempotencyKey.length < 8) throw new Error("签核请求编号无效");
  return Object.freeze({
    campaignId: campaign.campaign_id,
    verdict: result.verdict,
    payload: Object.freeze({
      decision: fields.decision,
      actor,
      reason,
      adjudication_result_sha256: summary.adjudication_result_sha256,
      idempotency_key: idempotencyKey,
      automatic_release_allowed: false,
    }),
  });
}

export function submitEndpointCampaignSignoff(intent, fetcher = fetch) {
  return request(
    `/v1/endpoint-validation-campaigns/${intent.campaignId}/signoff`,
    { method: "POST", body: JSON.stringify(intent.payload) },
    fetcher,
  );
}

export function endpointCampaignSignoffMatches(intent, receipt) {
  return receipt?.campaign_id === intent.campaignId &&
    receipt?.decision === intent.payload.decision &&
    receipt?.actor === intent.payload.actor &&
    receipt?.reason === intent.payload.reason &&
    receipt?.adjudication_result_sha256 === intent.payload.adjudication_result_sha256 &&
    receipt?.idempotency_key === intent.payload.idempotency_key &&
    receipt?.automatic_release_allowed === false;
}

import assert from "node:assert/strict";
import test from "node:test";
import {
  endpointCampaignSignoffMatches,
  freezeEndpointCampaignSignoff,
  loadEndpointCampaignSummary,
  submitEndpointCampaignSignoff,
  validateEndpointCampaignSummary,
} from "../src/endpoint-campaign.js";

const campaignId = "00000000-0000-0000-0000-000000000157";
const resultHash = `sha256:${"a".repeat(64)}`;
const credential = "campaign-signing-credential-" + "x".repeat(32);
const summary = () => ({
  campaign: {
    campaign_id: campaignId,
    state: "awaiting_signoff",
    endpoint_run_ids: Array.from({ length: 8 }, () => crypto.randomUUID()),
    automatic_release_allowed: false,
    adjudication_result: {
      campaign_id: campaignId,
      verdict: "inconclusive",
      formal_d_adjudication: true,
      automatic_release_allowed: false,
    },
  },
  endpoint_runs: [],
  signoff: null,
  adjudication_result_sha256: resultHash,
  formal_d_adjudication: true,
  automatic_release_allowed: false,
});

test("loads and validates the exact Endpoint Campaign summary", async () => {
  const value = await loadEndpointCampaignSummary(campaignId, async (path) => {
    assert.equal(path, `/v1/endpoint-validation-campaigns/${campaignId}/summary`);
    return { ok: true, json: async () => summary() };
  });
  assert.equal(validateEndpointCampaignSummary(value, campaignId), value);
  assert.throws(
    () => validateEndpointCampaignSummary({ ...value, automatic_release_allowed: true }, campaignId),
    /发布边界/,
  );
});

test("freezes signoff to the current formal D result hash", () => {
  const intent = freezeEndpointCampaignSignoff(
    summary(),
    { decision: "accepted", actor: "reviewer", reason: "accept evidence only" },
    "endpoint-signoff-001",
  );
  assert.equal(intent.payload.adjudication_result_sha256, resultHash);
  assert.equal(intent.payload.automatic_release_allowed, false);
  assert.throws(
    () => freezeEndpointCampaignSignoff(
      { ...summary(), campaign: { ...summary().campaign, state: "adjudicating" } },
      { decision: "accepted", actor: "reviewer", reason: "accept" },
      "endpoint-signoff-002",
    ),
    /没有可签核/,
  );
});

test("submits one frozen signoff and rejects a mismatched receipt", async () => {
  const intent = freezeEndpointCampaignSignoff(
    summary(),
    { decision: "rejected", actor: "reviewer", reason: "insufficient effect" },
    "endpoint-signoff-003",
  );
  const receipt = await submitEndpointCampaignSignoff(intent, credential, async (path, options) => {
    assert.equal(path, `/v1/endpoint-validation-campaigns/${campaignId}/signoff`);
    assert.equal(options.headers.Authorization, `Bearer ${credential}`);
    assert.deepEqual(JSON.parse(options.body), intent.payload);
    return {
      ok: true,
      json: async () => ({
        signoff_id: crypto.randomUUID(),
        campaign_id: campaignId,
        campaign_state: "rejected",
        ...intent.payload,
      }),
    };
  });
  assert.equal(endpointCampaignSignoffMatches(intent, receipt), true);
  assert.equal(
    endpointCampaignSignoffMatches(intent, { ...receipt, adjudication_result_sha256: `sha256:${"b".repeat(64)}` }),
    false,
  );
  assert.throws(
    () => submitEndpointCampaignSignoff(intent, "short", async () => assert.fail("must not fetch")),
    /独立的 Campaign 签核凭据/,
  );
});

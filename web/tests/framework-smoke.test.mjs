import assert from "node:assert/strict";
import test from "node:test";
import { checkLabel, frameworkState, loadFrameworkInspection } from "../src/framework-smoke.js";
import { freezeSignoff, loadSigningStatus, matchesSignoff, submitSignoff } from "../src/framework-signoff.js";

const id = "8b853469-df3b-4aba-8a30-cad83b2b349f";
test("unknown checks are never passing", () => {
  assert.equal(checkLabel(null), "尚无证据");
  assert.equal(checkLabel("true"), "尚无证据");
  assert.equal(checkLabel(false), "未通过");
  assert.equal(frameworkState("rejected"), "验收未通过");
});
test("only a matching task response with no release authority is accepted", async () => {
  const data = { schema: "framework-smoke-inspection-v1", task: { task_id: id },
    performance_conclusion: "not_measured", automatic_release_allowed: false };
  const fetcher = async (path, options) => {
    assert.equal(path, `/v1/framework-smoke-inspection/${id}`);
    assert.equal(options.headers.Authorization, "Bearer independent-task-key");
    assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => data };
  };
  assert.deepEqual(await loadFrameworkInspection(id, "independent-task-key", fetcher), data);
  data.automatic_release_allowed = true;
  await assert.rejects(loadFrameworkInspection(id, "independent-task-key", fetcher), /不匹配/);
});
test("authentication failure hides untrusted server messages", async () => {
  await assert.rejects(loadFrameworkInspection(id, "wrong", async () => ({ok: false, status: 401})), /凭据/);
});
test("invalid task rejected before network access", async () => {
  await assert.rejects(loadFrameworkInspection("../../secret", "key", () => assert.fail()), /编号无效/);
});

const evidence = "a371a627-269d-5961-8dcf-06ffa4c968a2";
const signingData = () => ({ task: { task_id: id, state: "awaiting_signoff" },
  write_actions_available: true, ready_for_human_review: true, review_evidence_id: evidence,
  evaluation_id: "04184dab-e277-51e8-8626-2175a74a434c", performance_conclusion: "not_measured",
  automatic_release_allowed: false });
const identity = () => ({ task_id: id, actor: "fixture", signoff: null,
  expires_at: new Date(Date.now() + 60000).toISOString() });
const token = "s".repeat(43);

test("signoff requires explicit readiness and freezes evidence independently of refresh", () => {
  const data = signingData();
  const intent = freezeSignoff(data, identity(), "approved", " fixture only ");
  data.review_evidence_id = id;
  assert.equal(intent.payload.evidence_bundle_id, evidence);
  assert.equal(intent.payload.reason, "fixture only");
  assert.ok(Object.isFrozen(intent) && Object.isFrozen(intent.payload));
  for (const field of ["write_actions_available", "ready_for_human_review"]) {
    assert.throws(() => freezeSignoff({ ...data, [field]: false }, identity(), "approved", "fixture"));
  }
  assert.throws(() => freezeSignoff(data, { ...identity(), task_id: evidence }, "approved", "fixture"));
  assert.throws(() => freezeSignoff(data, { ...identity(), expires_at: "2020-01-01" }, "approved", "fixture"));
});

test("credential stays in header; timeout retries preserve exact key and payload", async () => {
  const intent = freezeSignoff(signingData(), identity(), "approved", "fixture");
  const bodies = [];
  const receipt = { task_id: id, signoff_id: evidence, task_state: "completed", ...intent.payload };
  const fetcher = async (path, options) => {
    assert.equal(path, `/v1/framework-smoke/tasks/${id}/signoff`);
    assert.equal(options.headers.Authorization, `Bearer ${token}`);
    assert.equal(options.redirect, "error");
    bodies.push(options.body);
    if (bodies.length === 1) throw new Error("timeout");
    return { ok: true, json: async () => receipt };
  };
  await assert.rejects(submitSignoff(intent, token, fetcher), /timeout/);
  assert.deepEqual(await submitSignoff(intent, token, fetcher), receipt);
  assert.equal(bodies[0], bodies[1]);
  assert.ok(!bodies[0].includes(token));
  assert.equal(matchesSignoff(intent, { ...receipt, evidence_bundle_id: id }), false);
  assert.equal(matchesSignoff(intent, { ...receipt, actor: "forged" }), false);
});

test("status lookup is read-only and conflict never replaces evidence", async () => {
  const status = { ...identity(), automatic_release_allowed: false };
  assert.deepEqual(await loadSigningStatus(id, token, async (_path, options) => {
    assert.equal(options.method, undefined);
    return { ok: true, json: async () => status };
  }), status);
  const intent = freezeSignoff(signingData(), identity(), "rejected", "fixture");
  await assert.rejects(submitSignoff(intent, token, async () => ({ ok: false, status: 409 })),
    (error) => error.status === 409);
  assert.equal(intent.payload.evidence_bundle_id, evidence);
  await assert.rejects(loadSigningStatus(id, token, async () => ({ ok: true,
    json: async () => ({ ...status, task_id: evidence }) })), /不匹配/);
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { freezeScriptedStart, submitScriptedStart, validateStartReceipt } from "../src/scripted-start.js";

import {
  deriveStartAuditSteps,
  summarizeStartIntent,
} from "../src/start-intent.js";

const fixture = JSON.parse(
  await readFile(new URL("../public/fixtures/demo-start-intent.json", import.meta.url), "utf8"),
);

function startPreview() {
  return {preview_id: fixture.preview_id, resolved_plan_hash: fixture.resolved_plan_hash,
    service_identity: fixture.service_identity, synthetic: true, automatic_release_allowed: false,
    resolved_plan: {run_mode: "scripted", synthetic: true, automatic_release_allowed: false},
    start_allowed: true, required_ack_codes: ["warning"], expires_at: "2099-01-01T00:00:00Z"};
}

test("start requires explicit warning acknowledgement and live scripted authority", () => {
  const preview = startPreview();
  assert.throws(() => freezeScriptedStart(preview, "operator", [], "start-key"));
  assert.throws(() => freezeScriptedStart({...preview, synthetic: false}, "operator", ["warning"], "start-key"));
  assert.throws(() => freezeScriptedStart({...preview, expires_at: "bad"}, "operator", ["warning"], "start-key"));
  assert.throws(() => freezeScriptedStart({...preview, start_allowed: false}, "operator", ["warning"], "start-key"));
  assert.throws(() => freezeScriptedStart(preview, " ", ["warning"], "start-key"));
});

test("lost response retry uses the same request and does not duplicate concurrent calls", async () => {
  const request = freezeScriptedStart(startPreview(), fixture.actor, ["warning"], fixture.idempotency_key);
  const session = {};
  const bodies = [];
  await assert.rejects(submitScriptedStart(session, request, async (_, options) => {
    bodies.push(options.body);
    throw new Error("connection lost after server accepted");
  }));
  let resolve;
  const fetcher = async (_, options) => {
    bodies.push(options.body);
    return await new Promise((done) => { resolve = done; });
  };
  const first = submitScriptedStart(session, request, fetcher);
  const second = submitScriptedStart(session, request, fetcher);
  resolve({ok: true, json: async () => fixture});
  assert.deepEqual(await first, fixture);
  assert.deepEqual(await second, fixture);
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0], bodies[1]);
  await assert.rejects(submitScriptedStart(session, {...request, actor: "other"}, fetcher));
});

test("receipt must bind actor, plan, identity and synthetic release boundary", () => {
  const request = freezeScriptedStart(startPreview(), fixture.actor, ["warning"], fixture.idempotency_key);
  for (const change of [{actor: "wrong"}, {resolved_plan_hash: "wrong"},
    {automatic_release_allowed: true}, {synthetic: false}, {state: "success"},
    {service_identity: {}}]) {
    assert.throws(() => validateStartReceipt({...fixture, ...change}, request));
  }
});

test("maps a finalized StartIntent to a complete read-only audit", () => {
  const summary = summarizeStartIntent(fixture);
  const steps = deriveStartAuditSteps(fixture);

  assert.equal(summary.finalized, true);
  assert.equal(summary.releaseLocked, true);
  assert.equal(summary.boundCount, 3);
  assert.deepEqual(steps.map((step) => step.status), [
    "complete",
    "complete",
    "complete",
    "complete",
    "complete",
  ]);
});

test("keeps the first unobserved durable step failed without inventing recovery", () => {
  const failed = {
    ...fixture,
    state: "failed",
    candidate_members: fixture.candidate_members.map((member) => ({
      ...member,
      state: "pending",
    })),
    candidate_family_hash: null,
    error_code: "operator_start_interrupted",
    error_message: "The scripted Start stopped before Round member binding.",
    finalized_at: null,
  };
  const summary = summarizeStartIntent(failed);
  const steps = deriveStartAuditSteps(failed);

  assert.equal(summary.failed, true);
  assert.equal(summary.finalized, false);
  assert.equal(summary.releaseLocked, true);
  assert.deepEqual(steps.map((step) => step.status), [
    "complete",
    "complete",
    "failed",
    "pending",
    "pending",
  ]);
});

test("does not mark a partial Candidate Family as closed", () => {
  const partial = {
    ...fixture,
    state: "round_created",
    candidate_members: fixture.candidate_members.map((member, index) => ({
      ...member,
      state: index === 0 ? "round_member_bound" : "pending",
    })),
    candidate_family_hash: null,
    finalized_at: null,
  };
  const steps = deriveStartAuditSteps(partial);

  assert.equal(steps[2].status, "complete");
  assert.equal(steps[3].status, "current");
  assert.equal(steps[4].status, "pending");
});

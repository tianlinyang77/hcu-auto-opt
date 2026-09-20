import assert from "node:assert/strict";
import test from "node:test";
import { adjudicationResult, freezeManualSignoff, loadManualCandidateSummary, signoffMatches, submitManualSignoff } from "../src/manual-candidate.js";

const task = "73f6f07d-14ed-5614-a8e5-75e77c4356f0";
const candidate = "bbcdc4f0-0369-54d0-b4cd-57763203c272";
const evidence = "d1c3ca75-46de-5aab-92d3-4a0203f95f8b";
const summary = () => ({
  task: { task_id: task, state: "awaiting_signoff", version: 14, automatic_release_allowed: false },
  candidate: { candidate_id: candidate, state: "awaiting_signoff", evidence_bundle_id: evidence },
  jobs: [{ job_type: "manual_adjudicate", state: "succeeded", result: {
    synthetic: false,
    evaluation: { evaluation_run_id: crypto.randomUUID(), metrics: { verdict: "faster" } },
    evidence: { evidence_id: evidence, candidate_id: candidate, synthetic: false },
  }}],
});

test("loads the exact M1 summary route", async () => {
  const value = await loadManualCandidateSummary(task, async (path) => {
    assert.equal(path, `/v1/manual-candidate/tasks/${task}/summary`);
    return { ok: true, json: async () => summary() };
  });
  assert.equal(value.task.task_id, task);
});

test("freezes signoff to task, candidate and non-synthetic evidence", () => {
  const intent = freezeManualSignoff(summary(), { decision: "approved", actor: "reviewer", reason: "accepted; no release" }, "stable-key-001");
  assert.equal(intent.payload.evidence_bundle_id, evidence);
  assert.equal(intent.candidateId, candidate);
  assert.throws(() => freezeManualSignoff({ ...summary(), task: { ...summary().task, automatic_release_allowed: true } },
    { decision: "approved", actor: "reviewer", reason: "accepted" }, "stable-key-002"), /边界/);
});

test("submits one frozen request and verifies the database receipt", async () => {
  const intent = freezeManualSignoff(summary(), { decision: "approved", actor: "reviewer", reason: "accepted; no release" }, "stable-key-003");
  const receipt = await submitManualSignoff(intent, async (path, options) => {
    assert.equal(path, `/v1/manual-candidate/tasks/${task}/signoff`);
    assert.deepEqual(JSON.parse(options.body), intent.payload);
    return { ok: true, json: async () => ({ signoff_id: crypto.randomUUID(), task_id: task, candidate_id: candidate, ...intent.payload }) };
  });
  assert.equal(signoffMatches(intent, receipt), true);
  assert.equal(signoffMatches(intent, { ...receipt, evidence_bundle_id: crypto.randomUUID() }), false);
  assert.equal(adjudicationResult(summary()).evidence.evidence_id, evidence);
});

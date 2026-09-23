import assert from "node:assert/strict";
import test from "node:test";
import { adjudicationResult, agentOrigin, credibleThresholdStatus, formatEvidencePercent, freezeManualSignoff, historicalSignedOriginGap, loadManualCandidateSummary, measurementFacts, signoffMatches, submitManualSignoff } from "../src/manual-candidate.js";

const task = "73f6f07d-14ed-5614-a8e5-75e77c4356f0";
const candidate = "bbcdc4f0-0369-54d0-b4cd-57763203c272";
const evidence = "d1c3ca75-46de-5aab-92d3-4a0203f95f8b";
const summary = () => ({
  task: { task_id: task, state: "awaiting_signoff", version: 14, automatic_release_allowed: false },
  candidate: { candidate_id: candidate, state: "awaiting_signoff", evidence_bundle_id: evidence },
  jobs: [{ job_type: "manual_adjudicate", state: "succeeded", result: {
    synthetic: false,
    evaluation: { evaluation_run_id: crypto.randomUUID(), metrics: { verdict: "faster", correctness_verdict: "correct" } },
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

test("does not approve failed correctness or invalid D evidence", () => {
  const fields = { decision: "approved", actor: "reviewer", reason: "accept evidence" };
  const invalidD = summary();
  invalidD.jobs[0].result.evaluation.metrics.verdict = "invalid";
  assert.throws(() => freezeManualSignoff(invalidD, fields, "stable-key-invalid-d"), /不能批准/);

  const incorrect = summary();
  incorrect.jobs[0].result.evaluation.metrics.correctness_verdict = "incorrect";
  assert.throws(() => freezeManualSignoff(incorrect, fields, "stable-key-incorrect"), /不能批准/);

  const inconclusive = summary();
  inconclusive.jobs[0].result.evaluation.metrics.verdict = "inconclusive";
  assert.equal(freezeManualSignoff(inconclusive, fields, "stable-key-inconclusive").payload.decision,
    "approved");
});

test("BW20 approval requires exactly one hash-bound Agent origin event", () => {
  const bw20 = summary();
  bw20.task.adapter_profile = "bw20-m1-manual-v1";
  bw20.candidate.source_hash = `sha256:${"a".repeat(64)}`;
  const approved = { decision: "approved", actor: "reviewer", reason: "accept" };
  assert.throws(() => freezeManualSignoff(bw20, approved, "stable-key-origin-missing"), /Agent 来源链/);

  const origin = {
    schema_version: "bw20-agent-m1-origin-v1",
    generation_run_id: crypto.randomUUID(),
    proposal_id: crypto.randomUUID(),
    review_id: crypto.randomUUID(),
    review_record_hash: `sha256:${"b".repeat(64)}`,
    proposal_review_evidence_uri: "file:///proposal-review.json",
    proposal_review_evidence_hash: `sha256:${"c".repeat(64)}`,
    generation_review_evidence_uri: "file:///generation-review.json",
    generation_review_evidence_hash: `sha256:${"d".repeat(64)}`,
    patch_hash: `sha256:${"e".repeat(64)}`,
    candidate_id: candidate,
    candidate_source_hash: bw20.candidate.source_hash,
    source_package_hash: `sha256:${"f".repeat(64)}`,
    manifest_hash: `sha256:${"1".repeat(64)}`,
    decision: "approved",
    synthetic: false,
    automatic_release_allowed: false,
  };
  bw20.events = [{ event_type: "manual_candidate_agent_origin_recorded", details: origin }];
  assert.equal(agentOrigin(bw20).valid, true);
  assert.equal(freezeManualSignoff(bw20, approved, "stable-key-origin-valid").payload.decision, "approved");

  const drifted = structuredClone(bw20);
  drifted.events[0].details.candidate_source_hash = `sha256:${"9".repeat(64)}`;
  assert.equal(agentOrigin(drifted).valid, false);
  assert.throws(() => freezeManualSignoff(drifted, approved, "stable-key-origin-drift"), /Agent 来源链/);
  assert.equal(freezeManualSignoff(drifted,
    { decision: "rejected", actor: "reviewer", reason: "reject missing provenance" },
    "stable-key-origin-reject").payload.decision, "rejected");
});

test("only an already approved historical task treats a missing origin as a legacy gap", () => {
  const historical = summary();
  historical.task.adapter_profile = "bw20-m1-manual-v1";
  historical.task.state = "completed";
  historical.candidate.state = "accepted";
  historical.signoff = { decision: "approved" };
  assert.equal(historicalSignedOriginGap(historical), true);

  const pending = structuredClone(historical);
  pending.task.state = "awaiting_signoff";
  pending.candidate.state = "awaiting_signoff";
  pending.signoff = null;
  assert.equal(historicalSignedOriginGap(pending), false);

  const duplicate = structuredClone(historical);
  duplicate.events = Array.from({ length: 2 }, () => ({
    event_type: "manual_candidate_agent_origin_recorded",
    details: { candidate_id: candidate },
  }));
  assert.equal(historicalSignedOriginGap(duplicate), false);
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

test("formats only metrics present in the M1 evidence", () => {
  assert.equal(formatEvidencePercent(0.0125), "1.25%");
  assert.equal(formatEvidencePercent(null), "未提供");
  assert.equal(credibleThresholdStatus({ confidence_interval: [0.03, 0.05], credible_threshold: 0.02 }),
    "置信区间下界高于可信门限");
  assert.equal(credibleThresholdStatus({ confidence_interval: null, credible_threshold: 0.02 }),
    "证据未提供，无法判断");
  const result = { evaluation: { measurement: {
    protocol_version: "m1-kernel-performance-v1",
    sample_count: 400,
    warmup_count: 2,
    process_restart_count: 40,
    environment_fingerprint: "sha256:environment",
  } } };
  assert.deepEqual(measurementFacts(result), [
    ["测量协议", "m1-kernel-performance-v1"],
    ["样本数", 400],
    ["预热数", 2],
    ["进程重启数", 40],
    ["环境指纹", "sha256:environment"],
  ]);
  assert.deepEqual(measurementFacts({}), []);
});

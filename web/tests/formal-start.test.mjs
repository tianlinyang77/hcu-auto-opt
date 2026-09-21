// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import test from "node:test";
import assert from "node:assert/strict";
import { freezeFormalSubmission, loadFormalDispatch, loadFormalRecovery, loadFormalSubmission, submitFormalIntent } from "../src/formal-start.js";
const id = "12345678-1234-1234-1234-123456789abc";
const hash = "sha256:" + "a".repeat(64);
const plan = { preview_id: id, idempotency_key: "formal-test-key", resolved_plan_hash: hash,
  formal_authorization_hash: hash, execution_authority_hash: hash, evaluation_authority_hash: hash,
  expected_service_identity: { server_instance_id: id, source_commit: "a".repeat(40), profile_catalog_hash: hash, control_contract_version: "v1" } };
const receipt = { ...plan, intent_id: id, round_id: id, service_identity: plan.expected_service_identity,
  round_creation_allowed: false, hcu_accessed: false, automatic_release_allowed: false, synthetic: false, state: "ready_for_round_creation" };

test("recovery is scoped read-only and rejects broadened permissions", async () => {
  const report = { schema_version: "formal-correctness-recovery-v1", job_id: id, input_hash: hash,
    snapshot_hash: hash, status: "unknown_requires_manual_recovery", resource_id: "fixture",
    resource_state: "active", budget_state: "reserved", resource_owned_by_attempt: true,
    recorded_result: false, release_recorded: false, reconciliation_allowed: false,
    execution_retry_allowed: false, automatic_release_allowed: false, required_manual_checks: ["inspect"] };
  const observed = { schema_version: "formal-recovery-observation-v1", intent_id: id, round_id: id,
    resolved_plan_hash: hash, service_identity: plan.expected_service_identity, report,
    web_reconciliation_allowed: false };
  const fetchStatus = (value) => async (url, options) => {
    assert.equal(url, "/v1/operator/formal-correctness-recovery");
    assert.equal(options.method, "GET"); assert.equal(options.body, undefined);
    return { ok: true, json: async () => value };
  };
  assert.equal((await loadFormalRecovery(plan, receipt, "test-token", fetchStatus(observed))).report.status, report.status);
  for (const value of [{ ...observed, web_reconciliation_allowed: true },
    { ...observed, round_id: "other" },
    { ...observed, report: { ...report, execution_retry_allowed: true } },
    { ...observed, report: { ...report, reconciliation_allowed: true } }]) {
    await assert.rejects(loadFormalRecovery(plan, receipt, "test-token", fetchStatus(value)), /不匹配/);
  }
});

test("formal candidate progress shows persisted facts and rejects malformed evidence", async () => {
  const candidate = { candidate_id: id, round_candidate_id: id, state: "built", artifact_id: id, artifact_hash: hash };
  const status = { schema_version: "formal-dispatch-status-v1", intent_id: id, round_id: id,
    resolved_plan_hash: hash, service_identity: plan.expected_service_identity, state: "claimed",
    execution_consumer_enabled: false, automatic_release_allowed: false, candidates: [candidate] };
  const fetchStatus = (value) => async () => ({ ok: true, json: async () => value });
  const value = await loadFormalDispatch(plan, receipt, "test-token", fetchStatus(status));
  assert.equal(value.candidates[0].state, "built");
  for (const candidates of [[candidate, candidate], [{ ...candidate, state: "speedup_confirmed" }],
    [{ ...candidate, artifact_hash: null }], [{ ...candidate, candidate_id: "wrong" }], null]) {
    await assert.rejects(loadFormalDispatch(plan, receipt, "test-token", fetchStatus({ ...status, candidates })), /不匹配/);
  }
});
test("formal preparation uses protected same-origin read and frozen refs", async () => {
  const loaded = await loadFormalSubmission("test-token", async (url, options) => {
    assert.equal(url, "/v1/operator/formal-start-submission");
    assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error");
    assert.equal(options.cache, "no-store"); assert.equal(options.headers.Authorization, "Bearer test-token");
    return { ok: true, json: async () => plan };
  });
  assert.ok(Object.isFrozen(loaded)); assert.ok(Object.isFrozen(loaded.expected_service_identity));
  assert.throws(() => freezeFormalSubmission({ ...plan, actor_id: "injected" }));
});
test("formal retries preserve payload without credentials in body", async () => {
  const bodies = [];
  const send = async (url, options) => {
    assert.equal(url, "/v1/operator/formal-start-intents");
    bodies.push(options.body); assert.ok(!options.body.includes("test-token"));
    return { ok: true, json: async () => receipt };
  };
  await submitFormalIntent(plan, "test-token", send); await submitFormalIntent(plan, "test-token", send);
  assert.equal(bodies[0], bodies[1]);
});
test("formal receipts reject permission or identity substitution", async () => {
  for (const change of [{ hcu_accessed: true }, { automatic_release_allowed: true },
    { round_creation_allowed: true }, { synthetic: true }, { state: "running" },
    { idempotency_key: "other-key" }, { resolved_plan_hash: "sha256:" + "b".repeat(64) }]) {
    await assert.rejects(submitFormalIntent(plan, "test-token", async () =>
      ({ ok: true, json: async () => ({ ...receipt, ...change }) })), /不匹配/);
  }
});
test("errors do not echo credentials", async () => {
  await assert.rejects(loadFormalSubmission("test-token", async () => { throw new Error("test-token"); }), /读取失败/);
  await assert.rejects(loadFormalSubmission("test-token", async () => ({ ok: false, status: 503 })), /不可用/);
});
test("dispatch status is a scoped read, never a new submission", async () => {
  const status = { schema_version: "formal-dispatch-status-v1", intent_id: id, round_id: id,
    resolved_plan_hash: hash, service_identity: plan.expected_service_identity, state: "queued",
    execution_consumer_enabled: false, automatic_release_allowed: false };
  const observed = await loadFormalDispatch(plan, receipt, "test-token", async (url, options) => {
    assert.equal(url, "/v1/operator/formal-round-dispatch");
    assert.equal(options.method, "GET"); assert.equal(options.body, undefined);
    assert.equal(options.credentials, "omit"); assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => status };
  });
  assert.equal(observed.state, "queued");
  for (const state of ["claimed", "recovery_required", "stop_requested"]) {
    const value = await loadFormalDispatch(plan, receipt, "test-token", async () =>
      ({ ok: true, json: async () => ({ ...status, state }) }));
    assert.equal(value.state, state);
    assert.equal(value.execution_consumer_enabled, false);
  }
  for (const change of [{ intent_id: "other" }, { state: "running" },
    { execution_consumer_enabled: true }, { automatic_release_allowed: true },
    { round_id: "22345678-1234-1234-1234-123456789abc" }]) {
    await assert.rejects(loadFormalDispatch(plan, receipt, "test-token", async () =>
      ({ ok: true, json: async () => ({ ...status, ...change }) })), /不匹配/);
  }
});

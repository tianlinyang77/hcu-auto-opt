// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
import test from "node:test";
import assert from "node:assert/strict";
import { freezeFormalSubmission, loadFormalSubmission, submitFormalIntent } from "../src/formal-start.js";
const id = "12345678-1234-1234-1234-123456789abc";
const hash = "sha256:" + "a".repeat(64);
const plan = { preview_id: id, idempotency_key: "formal-test-key", resolved_plan_hash: hash,
  formal_authorization_hash: hash, execution_authority_hash: hash, evaluation_authority_hash: hash,
  expected_service_identity: { server_instance_id: id, source_commit: "a".repeat(40), profile_catalog_hash: hash, control_contract_version: "v1" } };
const receipt = { ...plan, intent_id: id, service_identity: plan.expected_service_identity,
  round_creation_allowed: false, hcu_accessed: false, automatic_release_allowed: false, synthetic: false, state: "ready_for_round_creation" };
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
  await assert.rejects(loadFormalSubmission("test-token", async () => { throw new Error("test-token"); }), /网络失败/);
  await assert.rejects(loadFormalSubmission("test-token", async () => ({ ok: false, status: 503 })), /不可用/);
});

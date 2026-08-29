import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  deriveStartAuditSteps,
  summarizeStartIntent,
} from "../src/start-intent.js";

const fixture = JSON.parse(
  await readFile(new URL("../public/fixtures/demo-start-intent.json", import.meta.url), "utf8"),
);

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

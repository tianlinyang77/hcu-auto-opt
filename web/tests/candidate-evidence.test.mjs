import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  candidateStageState,
  correctnessReasonCopy,
  summarizeCandidateEvidence,
} from "../src/candidate-evidence.js";

const fixture = JSON.parse(
  await readFile(
    new URL("../public/fixtures/demo-candidate-evidence.json", import.meta.url),
    "utf8",
  ),
);

test("summarizes immutable Build terminals without unlocking correctness", () => {
  const summary = summarizeCandidateEvidence(fixture);

  assert.equal(summary.candidateCount, 3);
  assert.equal(summary.buildAvailableCount, 2);
  assert.equal(summary.buildFailedCount, 1);
  assert.equal(summary.correctnessPassedCount, 2);
  assert.equal(summary.correctnessUnavailableCount, 1);
  assert.equal(summary.releaseLocked, true);
});

test("maps each Candidate stage from explicit evidence status", () => {
  const built = fixture.candidates[0];
  const failed = fixture.candidates[2];

  assert.equal(candidateStageState(built, "candidate"), "complete");
  assert.equal(candidateStageState(built, "build"), "complete");
  assert.equal(candidateStageState(built, "correctness"), "complete");
  assert.equal(candidateStageState(failed, "build"), "failed");
  assert.equal(candidateStageState(failed, "correctness"), "locked");
  assert.equal(
    correctnessReasonCopy[failed.correctness.reason],
    "Build 失败，未进入正确性",
  );
});

test("does not promote a state label into correctness evidence", () => {
  const stateOnly = {
    ...fixture.candidates[0],
    state: "correctness_passed",
    correctness: {
      status: "not_available",
      authority: "not_available",
      reason: "awaiting_search_barrier",
    },
  };

  assert.equal(candidateStageState(stateOnly, "correctness"), "locked");
});

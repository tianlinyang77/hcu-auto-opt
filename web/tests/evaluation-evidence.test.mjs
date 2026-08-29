import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  evaluationStageState,
  summarizeEvaluationEvidence,
  verdictTone,
} from "../src/evaluation-evidence.js";

const fixture = JSON.parse(
  await readFile(
    new URL("../public/fixtures/demo-evaluation-evidence.json", import.meta.url),
    "utf8",
  ),
);

test("summarizes a complete batch authority without releasing it", () => {
  const summary = summarizeEvaluationEvidence(fixture);

  assert.equal(summary.searchMemberCount, 3);
  assert.equal(summary.promotedCount, 1);
  assert.equal(summary.holdoutMemberCount, 1);
  assert.equal(summary.fwerCandidateCount, 1);
  assert.equal(summary.terminal, true);
  assert.equal(summary.releaseLocked, true);
});

test("maps stage availability only from explicit backend statuses", () => {
  assert.equal(evaluationStageState(fixture, "search"), "complete");
  assert.equal(evaluationStageState(fixture, "holdout"), "complete");
  assert.equal(evaluationStageState(fixture, "fwer"), "complete");
  assert.equal(evaluationStageState(fixture, "evidence"), "complete");

  const pending = { ...fixture, fwer_status: "pending", fwer: null };
  assert.equal(evaluationStageState(pending, "fwer"), "locked");
});

test("uses authority verdict labels without calculating a new verdict", () => {
  assert.equal(fixture.fwer.candidates[0].verdict, "faster");
  assert.equal(verdictTone(fixture.fwer.candidates[0].verdict), "success");
  assert.equal(fixture.evidence_bundle.summary.performance_conclusion, "not_measured");
});

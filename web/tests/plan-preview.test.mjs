import assert from "node:assert/strict";
import test from "node:test";

import { demoDashboard } from "../src/demo-data.js";
import {
  buildPlanPreviewRequest,
  createDemoPlanPreview,
  isPreviewExpired,
  validatePlanDraft,
} from "../src/plan-preview.js";

function fixture() {
  const target = demoDashboard.profiles.find((item) => item.profile_kind === "target");
  const measurement = demoDashboard.profiles.find((item) => item.profile_kind === "measurement");
  const workload = demoDashboard.workloads[0];
  const hotspot = demoDashboard.hotspots[0];
  const candidates = hotspot.candidate_packages.slice(0, 2);
  return { target, measurement, workload, hotspot, candidates };
}

test("builds an exact Scripted Preview request from discoverable Authority", () => {
  const values = fixture();
  const request = buildPlanPreviewRequest({
    name: "UI-1 Preview",
    identity: demoDashboard.identity,
    ...values,
    maxPromoted: 1,
    idempotencyKey: "ui1-preview-test",
  });

  assert.equal(request.run_mode, "scripted");
  assert.equal(request.target_profile.profile_hash, values.target.profile_hash);
  assert.equal(request.workload_profile.profile_hash, values.workload.profile.profile_hash);
  assert.equal(request.hotspot.hotspot_id, values.hotspot.hotspot.hotspot_id);
  assert.equal(request.candidates.length, 2);
  assert.deepEqual(request.candidates.map((item) => item.ordinal), [0, 1]);
  assert.equal(
    request.expected_service_identity.server_instance_id,
    demoDashboard.identity.server_instance_id,
  );
});

test("rejects Candidate families outside the frozen 2-4 bounds", () => {
  const values = fixture();
  const problems = validatePlanDraft({
    ...values,
    candidates: values.candidates.slice(0, 1),
    maxPromoted: 1,
  });
  assert.ok(problems.some((item) => item.includes("至少需要 2 个")));
});

test("renders pass, block, and expiry without changing the release boundary", () => {
  const values = fixture();
  const request = buildPlanPreviewRequest({
    name: "UI-1 Preview",
    identity: demoDashboard.identity,
    ...values,
    maxPromoted: 1,
    idempotencyKey: "ui1-preview-states",
  });
  const pass = createDemoPlanPreview(request, values.measurement, "pass");
  const blocked = createDemoPlanPreview(request, values.measurement, "blocked");
  const expired = createDemoPlanPreview(request, values.measurement, "expired");

  assert.equal(pass.start_allowed, true);
  assert.equal(pass.automatic_release_allowed, false);
  assert.equal(pass.resolved_plan.budget.max_candidates, 2);
  assert.equal(pass.resolved_plan.authority.synthetic, true);
  assert.equal(pass.resolved_plan.candidates.length, 2);
  assert.equal(pass.resolved_plan.candidates[0].candidate_kind, "fixture");
  assert.equal(
    pass.resolved_plan.candidates[0].source_package_ref.source_package_hash,
    values.candidates[0].source_package_ref.source_package_hash,
  );
  assert.equal(blocked.start_allowed, false);
  assert.deepEqual(blocked.resolved_plan.candidates, []);
  assert.ok(blocked.resolved_plan.authority);
  assert.ok(blocked.checks.some((item) => item.status === "block"));
  assert.equal(isPreviewExpired(expired), true);
});

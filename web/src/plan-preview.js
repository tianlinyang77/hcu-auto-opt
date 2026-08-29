function requireValue(value, message) {
  if (!value) throw new Error(message);
  return value;
}

export function profileRef(profile) {
  const value = requireValue(profile, "请选择完整的 Operator Profile");
  return {
    profile_id: value.profile_id,
    profile_version: value.profile_version,
    profile_kind: value.profile_kind,
    profile_hash: value.profile_hash,
  };
}

export function validatePlanDraft({
  target,
  workload,
  measurement,
  hotspot,
  candidates,
  maxPromoted,
}) {
  const problems = [];
  if (!target) problems.push("请选择 Target Profile");
  if (!workload) problems.push("请选择 Workload");
  if (!measurement) problems.push("请选择 Measurement Profile");
  if (!hotspot) problems.push("请选择 Hotspot");
  if (candidates.length < 2) problems.push("Candidate Family 至少需要 2 个候选");
  if (candidates.length > 4) problems.push("Candidate Family 最多只能包含 4 个候选");
  if (maxPromoted < 1 || maxPromoted > Math.min(2, candidates.length)) {
    problems.push("max_promoted 必须在 1 到已选候选数量之间，且不能超过 2");
  }
  return problems;
}

export function buildPlanPreviewRequest({
  name,
  identity,
  target,
  workload,
  measurement,
  hotspot,
  candidates,
  maxPromoted,
  idempotencyKey,
}) {
  const problems = validatePlanDraft({
    target,
    workload,
    measurement,
    hotspot,
    candidates,
    maxPromoted,
  });
  if (!name.trim()) problems.push("请输入计划名称");
  if (problems.length) throw new Error(problems.join("；"));

  const serviceIdentity = requireValue(identity, "Operator Service Identity 不可用");
  return {
    name: name.trim(),
    run_mode: "scripted",
    target_profile: profileRef(target),
    workload_profile: profileRef(workload.profile),
    measurement_profile: profileRef(measurement),
    hotspot: hotspot.hotspot,
    candidates: candidates.map((candidate, ordinal) => ({
      ordinal,
      source_package_ref: candidate.source_package_ref,
      optimization_intent: candidate.suggested_optimization_intent,
    })),
    max_promoted: maxPromoted,
    idempotency_key: idempotencyKey,
    expected_service_identity: {
      source_commit: serviceIdentity.source_commit,
      control_contract_version: serviceIdentity.control_contract_version,
      operator_contract_version: serviceIdentity.operator_contract_version,
      profile_catalog_hash: serviceIdentity.profile_catalog_hash,
      server_instance_id: serviceIdentity.server_instance_id,
    },
  };
}

function demoHash(value) {
  let state = 2166136261;
  for (const character of value) {
    state ^= character.charCodeAt(0);
    state = Math.imul(state, 16777619) >>> 0;
  }
  return `sha256:${state.toString(16).padStart(8, "0").repeat(8)}`;
}

export function createDemoPlanPreview(request, measurement, scenario = "pass") {
  const now = new Date();
  const createdAt = scenario === "expired"
    ? new Date(now.getTime() - 20 * 60_000)
    : now;
  const expiresAt = scenario === "expired"
    ? new Date(now.getTime() - 10 * 60_000)
    : new Date(now.getTime() + 10 * 60_000);
  const blocked = scenario === "blocked";
  const baseBudget = measurement.authority_refs.budget;
  const authority = {
    target_snapshot_id: "a25f159b-09a8-5dd2-9b5c-7349e0f66b3f",
    stage0_run_id: "0d67ca16-2f74-59d4-9f0e-c62500523c9a",
    stage0_protocol_hash: demoHash("stage0-protocol"),
    baseline_epoch_id: "ef06a71d-5669-432c-96ae-5815aa8cb625",
    baseline_source_hash: demoHash("baseline-source"),
    hotspot_id: request.hotspot.hotspot_id,
    replacement_point: request.hotspot.replacement_point,
    workload_id: request.workload_profile.profile_id,
    workload_hash: request.hotspot.workload_hash,
    configuration_hash: demoHash("configuration"),
    image_digest: demoHash("scripted-image"),
    adapter_profile: "scripted-fixture-v1",
    profiler_evidence_uri: request.hotspot.profiler_evidence_uri,
    profiler_evidence_hash: request.hotspot.profiler_evidence_hash,
    synthetic: true,
  };
  const candidateIds = [
    "0021d533-5802-43f7-ad61-c214029d7071",
    "eb24fca2-4219-4f63-a99a-b5b0c8bfe976",
    "a359c911-fc41-40d2-867f-503504a4fb36",
    "2688118a-a25c-439e-8942-08a9b221b57d",
  ];
  const resolvedCandidates = request.candidates.map((candidate, ordinal) => ({
    ordinal,
    candidate_id: candidateIds[ordinal],
    source_package_store_id: "m2-scripted-store",
    source_package_store_version: 1,
    source_package_store_hash: demoHash("candidate-package-store"),
    source_package_ref: candidate.source_package_ref,
    baseline_source_hash: authority.baseline_source_hash,
    hotspot_id: request.hotspot.hotspot_id,
    replacement_point: request.hotspot.replacement_point,
    candidate_kind: "fixture",
    optimization_intent: candidate.optimization_intent,
  }));
  const checks = [
    {
      code: "operator_service_identity_current",
      scope: "service",
      status: "pass",
      message: "Operator service identity matches this deployment.",
      retryable: false,
      action_code: "none",
    },
    {
      code: "operator_profiles_verified",
      scope: "profiles",
      status: "pass",
      message: "Exact synthetic Scripted Profile versions and Hashes are registered.",
      retryable: false,
      action_code: "none",
    },
    {
      code: "operator_authority_resolved",
      scope: "authority",
      status: "pass",
      message: "Target, Stage 0, Baseline, Workload, and Hotspot Authority resolved.",
      retryable: false,
      action_code: "none",
    },
    {
      code: blocked
        ? "operator_candidate_package_invalid"
        : "operator_candidate_packages_verified",
      scope: "candidates",
      status: blocked ? "block" : "pass",
      message: blocked
        ? "Candidate Packages could not be verified by the bound Store."
        : "All Candidate Packages and Manifest bindings were verified.",
      retryable: false,
      action_code: blocked ? "replace_candidate_packages" : "none",
    },
  ];
  const requestSignature = JSON.stringify(request);
  return {
    preview_id: "7ee66085-e350-54ba-929d-68b4c0c77001",
    preview_request_digest: demoHash(`request:${requestSignature}`),
    resolved_plan_hash: demoHash(`plan:${requestSignature}`),
    resolved_plan: {
      run_mode: "scripted",
      target_profile: request.target_profile,
      workload_profile: request.workload_profile,
      measurement_profile: request.measurement_profile,
      hotspot: request.hotspot,
      authority,
      candidates: blocked ? [] : resolvedCandidates,
      candidate_input_set_hash: blocked ? null : demoHash(`candidates:${requestSignature}`),
      search_protocol_version: measurement.authority_refs.search_protocol_version,
      search_protocol_hash: measurement.authority_refs.search_protocol_hash,
      holdout_protocol_version: measurement.authority_refs.holdout_protocol_version,
      holdout_protocol_hash: measurement.authority_refs.holdout_protocol_hash,
      selection_rule_hash: measurement.authority_refs.selection_rule_hash,
      budget: {
        ...baseBudget,
        max_candidates: request.candidates.length,
      },
      max_promoted: request.max_promoted,
      conclusion_boundary: measurement.authority_refs.conclusion_boundary,
      synthetic: true,
      automatic_release_allowed: false,
    },
    checks,
    start_allowed: !blocked,
    required_ack_codes: [],
    expires_at: expiresAt.toISOString(),
    service_identity: {
      ...request.expected_service_identity,
      server_instance_id: "66e91268-c3c4-4a52-8b71-228c28993179",
    },
    synthetic: true,
    automatic_release_allowed: false,
    created_at: createdAt.toISOString(),
  };
}

export function isPreviewExpired(preview, now = new Date()) {
  return new Date(preview.expires_at).getTime() <= now.getTime();
}

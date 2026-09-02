const hash = (seed) => {
  let state = 2166136261;
  for (const character of seed) {
    state ^= character.charCodeAt(0);
    state = Math.imul(state, 16777619) >>> 0;
  }
  return `sha256:${state.toString(16).padStart(8, "0").repeat(8)}`;
};

export async function loadDemoStartIntent(intentId) {
  const response = await fetch("/fixtures/demo-start-intent.json", {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`demo_fixture_${response.status}: Synthetic StartIntent fixture unavailable`);
  }
  const intent = await response.json();
  if (intent.intent_id !== intentId) {
    throw new Error("demo_intent_mismatch: 当前 Round 与 Synthetic StartIntent 不匹配");
  }
  return intent;
}

export async function loadDemoCandidateEvidence(roundId) {
  const response = await fetch("/fixtures/demo-candidate-evidence.json", {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      `demo_fixture_${response.status}: Synthetic Candidate evidence fixture unavailable`,
    );
  }
  const workspace = await response.json();
  if (workspace.round_id !== roundId) {
    throw new Error(
      "demo_round_mismatch: 当前 Round 与 Synthetic Candidate evidence 不匹配",
    );
  }
  return workspace;
}

export async function loadDemoEvaluationEvidence(roundId) {
  const response = await fetch("/fixtures/demo-evaluation-evidence.json", {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      `demo_fixture_${response.status}: Synthetic Evaluation evidence fixture unavailable`,
    );
  }
  const workspace = await response.json();
  if (workspace.round_id !== roundId) {
    throw new Error(
      "demo_round_mismatch: 当前 Round 与 Synthetic Evaluation evidence 不匹配",
    );
  }
  return workspace;
}

export async function loadDemoAgentProposals() {
  const response = await fetch("/fixtures/demo-agent-proposals.json", {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      `demo_fixture_${response.status}: Synthetic Agent Proposal fixture unavailable`,
    );
  }
  return response.json();
}

export const demoDashboard = {
  mode: "demo",
  identity: {
    source_commit: "1b592bd9392853103c143c2d248da58cfe3b4ade",
    control_contract_version: "hcuopt-control-v1",
    operator_contract_version: "m2-operator-v1",
    profile_catalog_hash: hash("a"),
    server_instance_id: "66e91268-c3c4-4a52-8b71-228c28993179",
  },
  profiles: [
    {
      profile_id: "m2-scripted-target",
      profile_version: 1,
      profile_kind: "target",
      profile_hash: hash("t"),
      display_name: "SGLang Scripted Target",
      summary: "Synthetic target lock for Operator UI and control-flow validation.",
      state: "active",
      allowed_run_modes: ["scripted"],
      authority_refs: {
        target_id: "m2-scripted-target-v1",
        target_spec_hash: hash("u"),
        adapter_profile: "scripted-fixture-v1",
        resource_policy_id: "scripted-no-hcu",
        resource_policy_hash: hash("v"),
        candidate_package_store_id: "m2-scripted-store",
        candidate_package_store_version: 1,
        candidate_package_store_hash: hash("w"),
        required_stage0_protocol_hash: hash("x"),
      },
      synthetic: true,
    },
    {
      profile_id: "m2-scripted-workload",
      profile_version: 1,
      profile_kind: "workload",
      profile_hash: hash("b"),
      display_name: "VisionLLM-7B / fixture workload",
      summary: "Synthetic workload for control-flow and UI validation only.",
      state: "active",
      allowed_run_modes: ["scripted"],
      authority_refs: {
        workload_id: "m2-scripted-workload-v1",
        workload_hash: hash("c"),
        configuration_hash: hash("d"),
        dataset_uri: "fixture://datasets/visionllm-v1",
        dataset_hash: hash("e"),
        model_uri: "fixture://models/visionllm-7b",
        model_hash: hash("f"),
        hotspot_scope_id: "m2-scripted-fixture-hotspots",
        hotspot_scope_hash: hash("g"),
        baseline_selection_policy: "latest_frozen_matching",
      },
      synthetic: true,
    },
    {
      profile_id: "m2-scripted-measurement",
      profile_version: 1,
      profile_kind: "measurement",
      profile_hash: hash("m"),
      display_name: "Scripted Search / Holdout protocol",
      summary: "Synthetic measurement budget and protocol references; no HCU is allocated.",
      state: "active",
      allowed_run_modes: ["scripted"],
      authority_refs: {
        search_protocol_version: "m2-scripted-search-v1",
        search_protocol_hash: hash("n"),
        holdout_protocol_version: "m2-scripted-holdout-v1",
        holdout_protocol_hash: hash("o"),
        selection_rule_hash: hash("p"),
        budget: {
          max_candidates: 4,
          max_build_attempts: 4,
          max_correctness_attempts: 4,
          max_search_samples: 120,
          max_holdout_samples: 60,
          max_wall_seconds: 3600,
          max_exclusive_lease_seconds: 1,
        },
        conclusion_boundary: "explore",
      },
      synthetic: true,
    },
  ],
  workloads: [
    {
      profile: {
        profile_id: "m2-scripted-workload",
        profile_version: 1,
        profile_kind: "workload",
        profile_hash: hash("b"),
      },
      state: "active",
      display_name: "VisionLLM-7B / fixture workload",
      summary: "Synthetic workload for control-flow and UI validation only.",
      authority_refs: {
        workload_id: "m2-scripted-workload-v1",
        workload_hash: hash("c"),
        configuration_hash: hash("d"),
        dataset_uri: "fixture://datasets/visionllm-v1",
        dataset_hash: hash("e"),
        model_uri: "fixture://models/visionllm-7b",
        model_hash: hash("f"),
        hotspot_scope_id: "m2-scripted-fixture-hotspots",
        hotspot_scope_hash: hash("g"),
        baseline_selection_policy: "latest_frozen_matching",
      },
      synthetic: true,
    },
  ],
  hotspots: [
    {
      hotspot: {
        source: "profiler",
        hotspot_id: "3e62de7d-31f6-47b7-9983-62dedb1d5644",
        hotspot_intake_hash: hash("h"),
        profiler_evidence_uri: "fixture://evidence/profiler.json",
        profiler_evidence_hash: hash("i"),
        correctness_evidence_uri: "fixture://evidence/correctness.json",
        correctness_evidence_hash: hash("j"),
        replacement_point: "sglang.fixture.layer_norm",
        workload_hash: hash("c"),
        shape: [1, 128],
        dtype: "float16",
      },
      target_profile: {
        profile_id: "m2-scripted-target",
        profile_version: 1,
        profile_kind: "target",
        profile_hash: hash("t"),
      },
      workload_profile: {
        profile_id: "m2-scripted-workload",
        profile_version: 1,
        profile_kind: "workload",
        profile_hash: hash("b"),
      },
      baseline_epoch_id: "ef06a71d-5669-432c-96ae-5815aa8cb625",
      baseline_source_hash: hash("k"),
      symbol: "sglang.fixture.layer_norm",
      share_ratio: 0.2,
      opportunity_score: 0.5,
      patchability: "python_overlay",
      candidate_packages: [
        {
          candidate_id: "0021d533-5802-43f7-ad61-c214029d7071",
          source_package_ref: {
            candidate_source_hash: hash("1"),
            source_package_hash: hash("a1"),
            manifest_hash: hash("b1"),
            manifest_schema_version: "m1-candidate-source-v1",
          },
          candidate_kind: "fixture",
          reviewed_by: "scripted-reviewer",
          reviewed_at: "2026-08-28T09:10:00Z",
          replacement_path: "sglang/fixture_kernel.py",
          suggested_optimization_intent: "Validate the no-op Candidate path.",
        },
        {
          candidate_id: "eb24fca2-4219-4f63-a99a-b5b0c8bfe976",
          source_package_ref: {
            candidate_source_hash: hash("2"),
            source_package_hash: hash("a2"),
            manifest_hash: hash("b2"),
            manifest_schema_version: "m1-candidate-source-v1",
          },
          candidate_kind: "fixture",
          reviewed_by: "scripted-reviewer",
          reviewed_at: "2026-08-28T09:12:00Z",
          replacement_path: "sglang/fixture_kernel.py",
          suggested_optimization_intent: "Exercise a known-faster Scripted branch.",
        },
        {
          candidate_id: "a359c911-fc41-40d2-867f-503504a4fb36",
          source_package_ref: {
            candidate_source_hash: hash("3"),
            source_package_hash: hash("a3"),
            manifest_hash: hash("b3"),
            manifest_schema_version: "m1-candidate-source-v1",
          },
          candidate_kind: "fixture",
          reviewed_by: "scripted-reviewer",
          reviewed_at: "2026-08-28T09:14:00Z",
          replacement_path: "sglang/fixture_kernel.py",
          suggested_optimization_intent: "Exercise a known-slower Scripted branch.",
        },
        {
          candidate_id: "2688118a-a25c-439e-8942-08a9b221b57d",
          source_package_ref: {
            candidate_source_hash: hash("4"),
            source_package_hash: hash("a4"),
            manifest_hash: hash("b4"),
            manifest_schema_version: "m1-candidate-source-v1",
          },
          candidate_kind: "fixture",
          reviewed_by: "scripted-reviewer",
          reviewed_at: "2026-08-28T09:16:00Z",
          replacement_path: "sglang/fixture_kernel.py",
          suggested_optimization_intent: "Exercise an immutable build-failure branch.",
        },
      ],
      synthetic: true,
      automatic_release_allowed: false,
    },
  ],
  rounds: [
    {
      schema_version: "m2-operator-read-model-v1",
      generated_at: "2026-08-29T09:42:00Z",
      intent_id: "7b93528c-4ef0-45fa-8537-b978611f274b",
      task_id: "af7735bd-2e5f-47ee-a72d-7eb3a753f6bc",
      round_id: "250cb54c-f0da-4475-87d8-9618f10232e2",
      round_version: 9,
      state: "scripted_completed",
      next_action: "none",
      reason: "Synthetic Search, Holdout, FWER, and EvidenceBundle are complete.",
      candidates: [
        {
          ordinal: 0,
          round_candidate_id: "3934a9f4-fc90-4749-8d7c-c74609f346ef",
          candidate_id: "0021d533-5802-43f7-ad61-c214029d7071",
          state: "holdout_measured",
          artifact_id: "91f30676-04da-4af4-89ad-2d9500360a30",
          artifact_hash: hash("1"),
          terminal_failure_code: null,
        },
        {
          ordinal: 1,
          round_candidate_id: "f79033c6-dd7e-49d3-9a97-6c53b2e7f46b",
          candidate_id: "eb24fca2-4219-4f63-a99a-b5b0c8bfe976",
          state: "not_promoted",
          artifact_id: "60be7365-f4bb-4390-9621-3c16638436c1",
          artifact_hash: hash("2"),
          terminal_failure_code: null,
        },
        {
          ordinal: 2,
          round_candidate_id: "6572408f-358a-4a44-a94a-018591862181",
          candidate_id: "a359c911-fc41-40d2-867f-503504a4fb36",
          state: "build_failed",
          artifact_id: null,
          artifact_hash: null,
          terminal_failure_code: "scripted_build_failure",
        },
      ],
      candidate_count: 3,
      build_terminal_count: 3,
      settled_budget_entry_count: 5,
      evidence_status: "available",
      terminal: true,
      synthetic: true,
      automatic_release_allowed: false,
    },
  ],
  reports: {
    "250cb54c-f0da-4475-87d8-9618f10232e2": {
      schema_version: "m2-operator-report-v1",
      generated_at: "2026-08-29T09:42:00Z",
      report_status: "final",
      evidence_bundle: {
        round_evidence_bundle_id: "40000000-0000-4000-8000-000000000004",
        terminal_reason: "holdout_completed",
        evidence_index_hash: hash("evidence-index"),
        summary: {
          performance_conclusion: "not_measured",
          evidence_authority: "synthetic_fixture_only",
        },
        synthetic: true,
        automatic_release_allowed: false,
      },
      conclusion_boundary: "synthetic_only_no_real_performance_claim",
      synthetic: true,
      performance_evidence: false,
      formal_signoff_allowed: false,
      automatic_release_allowed: false,
    },
  },
};

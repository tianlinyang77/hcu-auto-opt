const API_BASE = (import.meta.env.VITE_HCUOPT_API_BASE || "").replace(/\/$/, "");

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let detail = {};
    try {
      detail = await response.json();
    } catch {
      // Keep the stable HTTP fallback below when the server did not return JSON.
    }
    const code = detail.code || `http_${response.status}`;
    const message = detail.message || "Operator API request failed";
    throw new Error(`${code}: ${message}`);
  }
  return response.json();
}

export async function loadOperatorHotspots(target, workload) {
  const query = new URLSearchParams({
    target_profile_id: target.profile_id,
    target_profile_version: String(target.profile_version),
    workload_profile_id: workload.profile.profile_id,
    workload_profile_version: String(workload.profile.profile_version),
  });
  return request(`/v1/operator/hotspots?${query}`);
}

export async function createRoundPlanPreview(payload) {
  return request("/v1/operator/round-plans:preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function loadOperatorStartIntent(intentId) {
  return request(`/v1/operator/start-intents/${encodeURIComponent(intentId)}`);
}

export async function loadOperatorCandidateEvidence(roundId) {
  return request(
    `/v1/operator/search-rounds/${encodeURIComponent(roundId)}/candidate-evidence`,
  );
}

export async function loadOperatorEvaluationEvidence(roundId) {
  return request(
    `/v1/operator/search-rounds/${encodeURIComponent(roundId)}/evaluation-evidence`,
  );
}

export async function loadOperatorAgentProposals(generationRunId) {
  if (!generationRunId) {
    throw new Error(
      "missing_generation_run_id: 当前 Operator Read Model 尚未选择 Agent Generation Run",
    );
  }
  return request(
    `/v1/operator/agent-generations/${encodeURIComponent(generationRunId)}/evidence`,
  );
}

export async function loadOperatorDashboard() {
  const [identity, profiles, workloads, rounds] = await Promise.all([
    request("/v1/operator/identity"),
    request("/v1/operator/profiles"),
    request("/v1/operator/workloads"),
    request("/v1/operator/search-rounds?limit=20"),
  ]);

  const target = profiles.find((item) => item.profile_kind === "target");
  const workload = workloads[0];
  let hotspots = [];
  if (target && workload) {
    hotspots = await loadOperatorHotspots(target, workload);
  }

  const reports = {};
  await Promise.all(
    rounds.map(async (round) => {
      reports[round.round_id] = await request(
        `/v1/operator/search-rounds/${round.round_id}/report`,
      );
    }),
  );

  return {
    mode: "api",
    identity,
    profiles,
    workloads,
    hotspots,
    rounds,
    reports,
  };
}

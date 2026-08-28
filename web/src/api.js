const API_BASE = (import.meta.env.VITE_HCUOPT_API_BASE || "").replace(/\/$/, "");

async function request(path) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
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
    const query = new URLSearchParams({
      target_profile_id: target.profile_id,
      target_profile_version: String(target.profile_version),
      workload_profile_id: workload.profile.profile_id,
      workload_profile_version: String(workload.profile.profile_version),
    });
    hotspots = await request(`/v1/operator/hotspots?${query}`);
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

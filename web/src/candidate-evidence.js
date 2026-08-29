export const evidenceStageCopy = {
  candidate: {
    eyebrow: "01 · CANDIDATE",
    label: "候选来源",
    detail: "固定 Package、源码 Hash、替换点与优化意图",
  },
  build: {
    eyebrow: "02 · BUILD",
    label: "构建制品",
    detail: "展示 Artifact，或保留不可变失败证据",
  },
  correctness: {
    eyebrow: "03 · CORRECTNESS",
    label: "正确性",
    detail: "只承认 Search Barrier 已绑定的正确性证据",
  },
};

export const correctnessReasonCopy = {
  build_not_terminal: "Build 尚未形成终态",
  build_failed: "Build 失败，未进入正确性",
  awaiting_search_barrier: "等待 Search Barrier 写入权威证据",
  correctness_passed: "正确性证据已由 Search Barrier 绑定",
  correctness_failed: "Search Barrier 保留了正确性失败证据",
};

export function candidateStageState(candidate, stage) {
  if (!candidate) return "locked";
  if (stage === "candidate") return "complete";
  if (stage === "build") {
    if (candidate.build.status === "failed") return "failed";
    if (candidate.build.status === "available") return "complete";
    return "current";
  }
  if (candidate.correctness.status === "passed") return "complete";
  if (candidate.correctness.status === "failed") return "failed";
  return "locked";
}

export function summarizeCandidateEvidence(workspace) {
  const candidates = workspace?.candidates || [];
  return {
    candidateCount: candidates.length,
    buildAvailableCount: candidates.filter(
      (candidate) => candidate.build.status === "available",
    ).length,
    buildFailedCount: candidates.filter(
      (candidate) => candidate.build.status === "failed",
    ).length,
    correctnessPassedCount: candidates.filter(
      (candidate) => candidate.correctness.status === "passed",
    ).length,
    correctnessFailedCount: candidates.filter(
      (candidate) => candidate.correctness.status === "failed",
    ).length,
    correctnessUnavailableCount: candidates.filter(
      (candidate) => candidate.correctness.status === "not_available",
    ).length,
    releaseLocked:
      workspace?.synthetic === true &&
      workspace?.automatic_release_allowed === false &&
      workspace?.formal_signoff_allowed === false,
  };
}

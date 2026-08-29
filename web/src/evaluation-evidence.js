export const evaluationStageCopy = {
  search: {
    eyebrow: "01 · SEARCH",
    label: "Search Barrier",
    detail: "全家族到齐后，冻结晋级成员",
  },
  holdout: {
    eyebrow: "02 · HOLDOUT",
    label: "Holdout 隔离验证",
    detail: "Reveal 与 Barrier 必须绑定同一家族",
  },
  fwer: {
    eyebrow: "03 · FWER",
    label: "批级统计校正",
    detail: "展示 D 侧已形成的 Bonferroni 结果",
  },
  evidence: {
    eyebrow: "04 · EVIDENCE",
    label: "EvidenceBundle",
    detail: "汇总不可变证据索引和终态边界",
  },
};

export const evaluationStatusCopy = {
  pending: "等待权威证据",
  revealed: "Holdout Plan 已揭示",
  available: "权威证据可用",
  not_applicable: "本轮不适用",
};

export function evaluationStageState(workspace, stage) {
  if (!workspace) return "locked";
  const status = workspace[`${stage}_status`];
  if (status === "available") return "complete";
  if (status === "revealed") return "current";
  if (status === "not_applicable") return "skipped";
  return stage === "search" ? "current" : "locked";
}

export function summarizeEvaluationEvidence(workspace) {
  const searchMembers = workspace?.search?.members || [];
  const holdoutMembers = workspace?.holdout?.members || [];
  const fwerCandidates = workspace?.fwer?.candidates || [];
  return {
    searchMemberCount: searchMembers.length,
    promotedCount: workspace?.search?.promoted_candidate_ids?.length || 0,
    holdoutMemberCount: holdoutMembers.length,
    fwerCandidateCount: fwerCandidates.length,
    recommendationId: workspace?.fwer?.recommended_candidate_id || null,
    terminal: workspace?.evidence_status === "available",
    releaseLocked:
      workspace?.synthetic === true &&
      workspace?.real_performance_claim_allowed === false &&
      workspace?.formal_signoff_allowed === false &&
      workspace?.automatic_release_allowed === false,
  };
}

export function verdictTone(verdict) {
  if (verdict === "faster") return "success";
  if (verdict === "slower" || verdict === "invalid") return "danger";
  return "warning";
}

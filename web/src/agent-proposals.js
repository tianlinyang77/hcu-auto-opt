export function agentProposalSummary(readModel) {
  const proposals = Array.isArray(readModel?.proposals) ? readModel.proposals : []
  const attempts = Array.isArray(readModel?.attempts) ? readModel.attempts : []
  return Object.freeze({
    status: readModel?.status ?? 'invalid',
    keptCount: proposals.filter((item) => item.status === 'kept').length,
    eliminatedCount: proposals.filter((item) => item.status === 'eliminated').length,
    failedAttempts: attempts.filter((item) => item.status !== 'succeeded'),
    proposals,
    failureCodes: readModel?.failure_codes ?? [],
    lifecycle: Object.freeze({
      humanReview: readModel?.human_review_status ?? 'pending',
      packagePromotion: readModel?.package_promotion_status ?? 'pending',
    }),
    authority: Object.freeze({
      synthetic: readModel?.synthetic === true,
      environment: readModel?.environment ?? 'unknown',
      performanceConclusion: readModel?.performance_conclusion ?? 'not_measured',
      formalIntakeAllowed: false,
      automaticReleaseAllowed: false,
    }),
  })
}

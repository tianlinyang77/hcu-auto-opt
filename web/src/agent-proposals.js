export function agentProposalSummary(readModel) {
  const proposals = Array.isArray(readModel?.proposals) ? readModel.proposals : []
  const attempts = Array.isArray(readModel?.attempts) ? readModel.attempts : []
  const budget = readModel?.budget ?? {}
  const limit = readModel?.budget_limit ?? {}
  return Object.freeze({
    status: readModel?.status ?? 'invalid',
    keptCount: proposals.filter((item) => item.status === 'kept').length,
    eliminatedCount: proposals.filter((item) => item.status === 'eliminated').length,
    failedAttempts: attempts.filter((item) => item.status !== 'succeeded'),
    succeededAttempts: attempts.filter((item) => item.status === 'succeeded'),
    duplicateCount: proposals.filter((item) => item.reason_code?.startsWith('duplicate_')).length,
    proposals,
    attempts,
    failureCodes: readModel?.failure_codes ?? [],
    budget: Object.freeze({
      usage: budget,
      limit,
      attemptsRatio: ratio(budget.attempt_count, limit.max_generator_attempts),
      wallRatio: ratio(budget.wall_seconds, limit.max_wall_seconds),
      outputRatio: ratio(budget.output_bytes, limit.max_total_output_bytes),
      tokensRatio: ratio(budget.output_tokens, limit.max_total_tokens),
      proposalsRatio: ratio(budget.proposal_count, limit.max_proposals),
    }),
    lifecycle: Object.freeze({
      humanReview: readModel?.human_review_status ?? 'pending',
      packagePromotion: readModel?.package_promotion_status ?? 'pending',
      formalReadiness: 'hold',
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

function ratio(value, limit) {
  if (!Number.isFinite(value) || !Number.isFinite(limit) || limit <= 0) return 0
  return Math.min(1, Math.max(0, value / limit))
}

export function inspectionReadModel(inspection, expectedRunId) {
  const model = inspection?.read_model
  if (inspection?.schema_version !== 'm2b-agent-inspection-v1'
    || inspection.generation_run_id !== expectedRunId
    || model?.generation_run_id !== expectedRunId
    || inspection.inspection_kind !== 'pre_signoff_read_only'
    || inspection.evidence_scope !== 'development_only_not_performance'
    || inspection.formal_intake_allowed !== false
    || inspection.automatic_release_allowed !== false
    || model.formal_intake_allowed !== false
    || model.automatic_release_allowed !== false
    || model.performance_conclusion !== 'not_measured') {
    throw new Error('invalid_agent_inspection: 检查快照的身份或权限边界不匹配')
  }
  return model
}

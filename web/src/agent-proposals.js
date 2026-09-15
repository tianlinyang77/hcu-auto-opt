export function pendingReviewDisplay(lifecycle) {
  if (lifecycle?.review_status === 'pending') {
    return { title: '待人工审核', description: '尚无人工审核记录；此页面只读，不执行审核或晋级。' }
  }
  if (lifecycle?.review_status === 'not_applicable') {
    return { title: '不适用', description: '该提案未被保留，不能进入审核。' }
  }
  return { title: '审核证据未确认', description: '缺少可展示的审核记录，不能推定已审核或不适用。' }
}

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

// An unavailable read is not a measured zero. Hide the previous snapshot while
// revalidating it, including its export, until the current request succeeds.
export function agentWorkspacePresentation(readModel, { loading = false, error = null } = {}) {
  const workspace = loading || error ? null : readModel
  const summary = agentProposalSummary(workspace)
  const hasAttempts = Array.isArray(workspace?.attempts)
  const hasProposals = Array.isArray(workspace?.proposals)
  const used = workspace?.budget?.attempt_count
  const limit = workspace?.budget_limit?.max_generator_attempts
  const hasBudget = Number.isFinite(used) && used >= 0 && Number.isFinite(limit) && limit > 0
  return {
    workspace,
    summary,
    metrics: {
      attempts: hasAttempts ? summary.attempts.length : '—',
      failed: hasAttempts ? `${summary.failedAttempts.length} 次超时 / 失败` : '未读取到执行证据',
      retained: hasProposals ? summary.keptCount : '—',
      duplicates: hasProposals ? `${summary.duplicateCount} 个稳定去重` : '未读取到提案证据',
      budgetPercent: hasBudget ? `${Math.round(summary.budget.attemptsRatio * 100)}%` : '—',
      budgetAttempts: hasBudget ? `${used}/${limit} 次尝试` : '用量未知，不代表未消耗',
    },
  }
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

export function terminalEvidenceReadModel(model, expectedRunId) {
  if (model?.schema_version !== 'm2b-agent-generation-read-model-v1'
    || model.generation_run_id !== expectedRunId
    || model.formal_intake_allowed !== false
    || model.automatic_release_allowed !== false
    || model.formal_readiness !== 'hold'
    || model.performance_conclusion !== 'not_measured'
    || !Array.isArray(model.attempts) || !Array.isArray(model.proposals)) {
    throw new Error('invalid_agent_evidence: 终态报告的身份或权限边界不匹配')
  }
  return model
}

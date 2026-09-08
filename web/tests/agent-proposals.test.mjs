import assert from 'node:assert/strict'
import test from 'node:test'

import { agentProposalSummary, agentWorkspacePresentation, inspectionReadModel, pendingReviewDisplay, terminalEvidenceReadModel } from '../src/agent-proposals.js'
import { readFileSync } from 'node:fs'

test('unavailable evidence never displays zero attempts or zero budget', () => {
  for (const model of [null, undefined, {}]) {
    const { metrics } = agentWorkspacePresentation(model)
    assert.equal(metrics.attempts, '—')
    assert.equal(metrics.retained, '—')
    assert.equal(metrics.budgetPercent, '—')
    assert.match(metrics.budgetAttempts, /未知/)
  }
})

test('terminal report checks the real contract, Run binding and locked permissions', () => {
  const model = JSON.parse(readFileSync(new URL('../public/fixtures/demo-agent-proposals.json', import.meta.url)))
  const runId = model.generation_run_id
  assert.equal(terminalEvidenceReadModel(model, runId), model)
  for (const changes of [{schema_version: 'm2b-agent-inspection-v1'}, {generation_run_id: 'other'},
    {formal_intake_allowed: true}, {automatic_release_allowed: true}, {formal_readiness: 'ready'},
    {performance_conclusion: 'faster'}, {attempts: null}, {proposals: null}]) {
    assert.throws(() => terminalEvidenceReadModel({...model, ...changes}, runId))
  }
  assert.throws(() => terminalEvidenceReadModel({read_model: model}, runId))
})

test('loading and denied refresh hide the old evidence and export snapshot', () => {
  const old = { attempts: [{status: 'succeeded'}], proposals: [{status: 'kept'}],
    budget: {attempt_count: 1}, budget_limit: {max_generator_attempts: 2} }
  for (const state of [{loading: true}, {error: 'denied'}, {error: 'transport failure'}]) {
    const view = agentWorkspacePresentation(old, state)
    assert.equal(view.workspace, null)
    assert.equal(view.metrics.attempts, '—')
    assert.equal(view.metrics.retained, '—')
    assert.equal(view.metrics.budgetPercent, '—')
  }
  const recovered = agentWorkspacePresentation(old)
  assert.equal(recovered.workspace, old)
  assert.equal(recovered.metrics.attempts, 1)
  assert.equal(recovered.metrics.retained, 1)
  assert.equal(recovered.metrics.budgetPercent, '50%')
})

test('verified zero remains zero while missing or invalid usage remains unknown', () => {
  const empty = {attempts: [], proposals: [], budget: {attempt_count: 0},
    budget_limit: {max_generator_attempts: 2}}
  const view = agentWorkspacePresentation(empty)
  assert.equal(view.metrics.attempts, 0)
  assert.equal(view.metrics.retained, 0)
  assert.equal(view.metrics.budgetPercent, '0%')
  assert.equal(view.metrics.budgetAttempts, '0/2 次尝试')
  for (const used of [undefined, null, -1, NaN, Infinity, '0']) {
    assert.equal(agentWorkspacePresentation({...empty, budget: {attempt_count: used}}).metrics.budgetPercent, '—')
  }
  for (const limit of [undefined, null, 0, -1, NaN, Infinity]) {
    assert.equal(agentWorkspacePresentation({...empty, budget_limit: {max_generator_attempts: limit}}).metrics.budgetPercent, '—')
  }
})

test('retained proposal pending review is not rendered as eliminated', () => {
  const display = pendingReviewDisplay({ review_status: 'pending', review: null })
  assert.equal(display.title, '待人工审核')
  assert.doesNotMatch(display.description, /未被保留/)
  assert.match(display.description, /只读/)
})

test('only explicit not_applicable displays non-applicable review', () => {
  assert.equal(pendingReviewDisplay({ review_status: 'not_applicable' }).title, '不适用')
  for (const status of ['approved', 'rejected', 'unexpected', undefined]) {
    assert.equal(pendingReviewDisplay({ review_status: status }).title, '审核证据未确认')
  }
  assert.equal(pendingReviewDisplay(null).title, '审核证据未确认')
})

test('proposal read model copies D verdicts and never upgrades authority', () => {
  const summary = agentProposalSummary({
    status: 'ready_for_review',
    proposals: [
      { proposal_id: 'p1', status: 'kept' },
      { proposal_id: 'p2', status: 'eliminated', reason_code: 'duplicate_exact_patch' },
    ],
    attempts: [{ attempt_id: 'a1', status: 'timed_out', reason_code: 'runner_timeout' }],
    failure_codes: ['runner_timeout'],
    human_review_status: 'pending',
    package_promotion_status: 'pending',
    budget: { attempt_count: 1, wall_seconds: 2, output_bytes: 10, output_tokens: 20, proposal_count: 2 },
    budget_limit: { max_generator_attempts: 4, max_wall_seconds: 10, max_total_output_bytes: 100, max_total_tokens: 200, max_proposals: 4 },
    synthetic: true,
    environment: 'scripted_dev_only',
    performance_conclusion: 'not_measured',
    formal_intake_allowed: true,
    automatic_release_allowed: true,
  })

  assert.equal(summary.keptCount, 1)
  assert.equal(summary.eliminatedCount, 1)
  assert.equal(summary.failedAttempts[0].reason_code, 'runner_timeout')
  assert.equal(summary.authority.performanceConclusion, 'not_measured')
  assert.equal(summary.lifecycle.humanReview, 'pending')
  assert.equal(summary.lifecycle.packagePromotion, 'pending')
  assert.equal(summary.lifecycle.formalReadiness, 'hold')
  assert.equal(summary.budget.attemptsRatio, 0.25)
  assert.equal(summary.budget.proposalsRatio, 0.5)
  assert.equal(summary.authority.formalIntakeAllowed, false)
  assert.equal(summary.authority.automaticReleaseAllowed, false)
})

test('zero proposals preserves failed attempts and consumed tokens', () => {
  const summary = agentProposalSummary({proposals: [],
    attempts: [{status: 'failed', reason_code: 'invalid_model_proposal'}],
    failure_codes: ['invalid_model_proposal'], budget: {output_tokens: 100}})
  assert.equal(summary.keptCount, 0)
  assert.equal(summary.failedAttempts.length, 1)
  assert.equal(summary.budget.usage.output_tokens, 100)
  assert.deepEqual(summary.failureCodes, ['invalid_model_proposal'])
})

test('inspection rejects cross-run and upgraded authority without a demo fallback', () => {
  const fixture = {schema_version: 'm2b-agent-inspection-v1', generation_run_id: 'run',
    inspection_kind: 'pre_signoff_read_only', evidence_scope: 'development_only_not_performance',
    formal_intake_allowed: false, automatic_release_allowed: false,
    read_model: {generation_run_id: 'run', formal_intake_allowed: false,
      automatic_release_allowed: false, performance_conclusion: 'not_measured'}}
  assert.equal(inspectionReadModel(fixture, 'run'), fixture.read_model)
  assert.throws(() => inspectionReadModel(fixture, 'other'))
  assert.throws(() => inspectionReadModel({...fixture, automatic_release_allowed: true}, 'run'))
  assert.throws(() => inspectionReadModel({...fixture, read_model: null}, 'run'))
})

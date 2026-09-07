import assert from 'node:assert/strict'
import test from 'node:test'

import { agentProposalSummary, inspectionReadModel } from '../src/agent-proposals.js'

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

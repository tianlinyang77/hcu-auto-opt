import assert from 'node:assert/strict'
import test from 'node:test'

import { agentProposalSummary } from '../src/agent-proposals.js'

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
  assert.equal(summary.authority.formalIntakeAllowed, false)
  assert.equal(summary.authority.automaticReleaseAllowed, false)
})

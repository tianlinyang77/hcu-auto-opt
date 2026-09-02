CREATE TABLE agent_generation_runs (
    generation_run_id UUID PRIMARY KEY,
    request_id UUID NOT NULL UNIQUE,
    plan_id UUID NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    request JSONB NOT NULL,
    plan JSONB NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'created',
    planned_generator_count INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    terminal_attempt_count INTEGER NOT NULL DEFAULT 0,
    terminal_generator_count INTEGER NOT NULL DEFAULT 0,
    proposal_count INTEGER NOT NULL DEFAULT 0,
    retained_proposal_count INTEGER NOT NULL DEFAULT 0,
    budget_reserved JSONB NOT NULL,
    budget_consumed JSONB NOT NULL,
    review_evidence_uri TEXT,
    review_evidence_hash TEXT,
    error_code TEXT,
    error_message TEXT,
    dev_only BOOLEAN NOT NULL DEFAULT TRUE,
    formal_intake_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    hcu_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    measurement_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    CONSTRAINT agent_generation_run_state_known CHECK (
        state IN ('created', 'running', 'awaiting_review', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT agent_generation_run_counts_bounded CHECK (
        planned_generator_count BETWEEN 1 AND 8
        AND attempt_count BETWEEN 0 AND 64
        AND terminal_attempt_count BETWEEN 0 AND attempt_count
        AND terminal_generator_count BETWEEN 0 AND planned_generator_count
        AND proposal_count BETWEEN 0 AND 32
        AND retained_proposal_count BETWEEN 0 AND proposal_count
    ),
    CONSTRAINT agent_generation_run_hashes_valid CHECK (
        request_hash ~ '^sha256:[0-9a-f]{64}$'
        AND plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND (
            review_evidence_hash IS NULL
            OR review_evidence_hash ~ '^sha256:[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT agent_generation_run_payload_objects CHECK (
        jsonb_typeof(request) = 'object'
        AND jsonb_typeof(plan) = 'object'
        AND jsonb_typeof(budget_reserved) = 'object'
        AND jsonb_typeof(budget_consumed) = 'object'
    ),
    CONSTRAINT agent_generation_run_payload_identity CHECK (
        request->>'generation_run_id' = generation_run_id::TEXT
        AND plan->>'generation_run_id' = generation_run_id::TEXT
        AND request->>'request_id' = request_id::TEXT
        AND plan->>'request_id' = request_id::TEXT
        AND plan->>'plan_id' = plan_id::TEXT
    ),
    CONSTRAINT agent_generation_run_review_atomic CHECK (
        (review_evidence_uri IS NULL) = (review_evidence_hash IS NULL)
        AND ((state = 'completed') = (review_evidence_uri IS NOT NULL))
    ),
    CONSTRAINT agent_generation_run_error_atomic CHECK (
        (error_code IS NULL) = (error_message IS NULL)
        AND ((state = 'failed') = (error_code IS NOT NULL))
    ),
    CONSTRAINT agent_generation_run_terminal_time CHECK (
        ((state IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
        AND updated_at >= created_at
        AND (finished_at IS NULL OR finished_at >= created_at)
    ),
    CONSTRAINT agent_generation_run_dev_only CHECK (dev_only = TRUE),
    CONSTRAINT agent_generation_run_never_formal_intake CHECK (formal_intake_allowed = FALSE),
    CONSTRAINT agent_generation_run_no_hcu CHECK (hcu_access_allowed = FALSE),
    CONSTRAINT agent_generation_run_no_measurement CHECK (measurement_access_allowed = FALSE),
    CONSTRAINT agent_generation_run_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    ),
    UNIQUE (generation_run_id, plan_id, request_id)
);

CREATE TABLE agent_generator_attempts (
    attempt_id UUID PRIMARY KEY,
    generation_run_id UUID NOT NULL,
    plan_id UUID NOT NULL,
    request_id UUID NOT NULL,
    generator_id TEXT NOT NULL,
    generator_ordinal INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    adapter_profile TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    reserved JSONB NOT NULL,
    actual JSONB NOT NULL,
    worker_id TEXT,
    claim_token UUID,
    lease_expires_at TIMESTAMPTZ,
    batch_id UUID,
    batch_hash TEXT,
    batch_status TEXT,
    raw_output_uri TEXT,
    raw_output_hash TEXT,
    adapter_provenance JSONB,
    error_code TEXT,
    error_message TEXT,
    dev_only BOOLEAN NOT NULL DEFAULT TRUE,
    hcu_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    measurement_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CONSTRAINT agent_generator_attempt_run_fk FOREIGN KEY (
        generation_run_id, plan_id, request_id
    ) REFERENCES agent_generation_runs (
        generation_run_id, plan_id, request_id
    ) ON DELETE RESTRICT,
    CONSTRAINT agent_generator_attempt_state_known CHECK (
        state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT agent_generator_attempt_identity_bounded CHECK (
        generator_id ~ '^[a-z0-9][a-z0-9._-]{2,99}$'
        AND generator_ordinal BETWEEN 0 AND 7
        AND attempt_number BETWEEN 1 AND 8
    ),
    CONSTRAINT agent_generator_attempt_usage_objects CHECK (
        jsonb_typeof(reserved) = 'object'
        AND jsonb_typeof(actual) = 'object'
    ),
    CONSTRAINT agent_generator_attempt_claim_atomic CHECK (
        (worker_id IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND started_at IS NULL)
        OR
        (worker_id IS NOT NULL AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)
    ),
    CONSTRAINT agent_generator_attempt_batch_atomic CHECK (
        (
            batch_id IS NULL AND batch_hash IS NULL AND batch_status IS NULL
            AND raw_output_uri IS NULL AND raw_output_hash IS NULL
            AND adapter_provenance IS NULL
        ) OR (
            batch_id IS NOT NULL AND batch_hash IS NOT NULL AND batch_status IS NOT NULL
            AND raw_output_uri IS NOT NULL AND raw_output_hash IS NOT NULL
            AND adapter_provenance IS NOT NULL
            AND jsonb_typeof(adapter_provenance) = 'object'
        )
    ),
    CONSTRAINT agent_generator_attempt_batch_hashes_valid CHECK (
        (batch_hash IS NULL OR batch_hash ~ '^sha256:[0-9a-f]{64}$')
        AND (raw_output_hash IS NULL OR raw_output_hash ~ '^sha256:[0-9a-f]{64}$')
        AND (batch_status IS NULL OR batch_status IN ('succeeded', 'partial', 'failed'))
    ),
    CONSTRAINT agent_generator_attempt_state_binding CHECK (
        (state <> 'pending' OR (worker_id IS NULL AND finished_at IS NULL))
        AND (state <> 'running' OR (worker_id IS NOT NULL AND finished_at IS NULL))
        AND (state <> 'succeeded' OR (worker_id IS NOT NULL AND batch_id IS NOT NULL))
        AND (state NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL)
        AND (state NOT IN ('pending', 'running', 'cancelled') OR batch_id IS NULL)
    ),
    CONSTRAINT agent_generator_attempt_error_atomic CHECK (
        (error_code IS NULL) = (error_message IS NULL)
        AND ((state = 'failed') = (error_code IS NOT NULL))
    ),
    CONSTRAINT agent_generator_attempt_times_valid CHECK (
        updated_at >= created_at
        AND (started_at IS NULL OR started_at >= created_at)
        AND (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
    ),
    CONSTRAINT agent_generator_attempt_dev_only CHECK (dev_only = TRUE),
    CONSTRAINT agent_generator_attempt_no_hcu CHECK (hcu_access_allowed = FALSE),
    CONSTRAINT agent_generator_attempt_no_measurement CHECK (measurement_access_allowed = FALSE),
    CONSTRAINT agent_generator_attempt_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    ),
    UNIQUE (generation_run_id, generator_id, attempt_number)
);

CREATE INDEX agent_generator_attempt_pending_idx
ON agent_generator_attempts (generation_run_id, generator_ordinal, attempt_number)
WHERE state = 'pending';

CREATE INDEX agent_generator_attempt_lease_idx
ON agent_generator_attempts (lease_expires_at)
WHERE state = 'running';

CREATE TABLE agent_generation_budget_ledger (
    ledger_entry_id UUID PRIMARY KEY,
    generation_run_id UUID NOT NULL REFERENCES agent_generation_runs(generation_run_id)
        ON DELETE RESTRICT,
    attempt_id UUID NOT NULL REFERENCES agent_generator_attempts(attempt_id)
        ON DELETE RESTRICT,
    entry_type TEXT NOT NULL,
    reserved JSONB NOT NULL,
    actual JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT agent_generation_budget_entry_type_known CHECK (
        entry_type IN ('reserve', 'settle', 'release')
    ),
    CONSTRAINT agent_generation_budget_usage_objects CHECK (
        jsonb_typeof(reserved) = 'object' AND jsonb_typeof(actual) = 'object'
    ),
    CONSTRAINT agent_generation_budget_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    ),
    UNIQUE (attempt_id, entry_type)
);

CREATE TABLE agent_candidate_proposal_refs (
    proposal_id UUID PRIMARY KEY,
    proposal_hash TEXT NOT NULL,
    generation_run_id UUID NOT NULL REFERENCES agent_generation_runs(generation_run_id)
        ON DELETE RESTRICT,
    request_id UUID NOT NULL,
    attempt_id UUID NOT NULL REFERENCES agent_generator_attempts(attempt_id)
        ON DELETE RESTRICT,
    batch_id UUID NOT NULL,
    generator_id TEXT NOT NULL,
    generator_ordinal INTEGER NOT NULL,
    proposal_ordinal INTEGER NOT NULL,
    patch_uri TEXT NOT NULL,
    patch_hash TEXT NOT NULL,
    normalized_patch_hash TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'pending',
    duplicate_of_proposal_id UUID REFERENCES agent_candidate_proposal_refs(proposal_id)
        ON DELETE RESTRICT,
    review_required BOOLEAN NOT NULL DEFAULT TRUE,
    formal_intake_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    performance_conclusion TEXT NOT NULL DEFAULT 'not_measured',
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT agent_candidate_proposal_hashes_valid CHECK (
        proposal_hash ~ '^sha256:[0-9a-f]{64}$'
        AND patch_hash ~ '^sha256:[0-9a-f]{64}$'
        AND normalized_patch_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT agent_candidate_proposal_disposition_known CHECK (
        disposition IN ('pending', 'retained', 'duplicate')
    ),
    CONSTRAINT agent_candidate_proposal_duplicate_atomic CHECK (
        (disposition = 'duplicate') = (duplicate_of_proposal_id IS NOT NULL)
        AND duplicate_of_proposal_id IS DISTINCT FROM proposal_id
    ),
    CONSTRAINT agent_candidate_proposal_review_only CHECK (review_required = TRUE),
    CONSTRAINT agent_candidate_proposal_never_formal_intake CHECK (
        formal_intake_allowed = FALSE
    ),
    CONSTRAINT agent_candidate_proposal_not_measured CHECK (
        performance_conclusion = 'not_measured'
    ),
    CONSTRAINT agent_candidate_proposal_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    ),
    UNIQUE (generation_run_id, generator_id, proposal_ordinal)
);

CREATE INDEX agent_candidate_proposal_dedupe_idx
ON agent_candidate_proposal_refs (
    generation_run_id, normalized_patch_hash, generator_ordinal, proposal_ordinal
);

CREATE OR REPLACE FUNCTION protect_agent_generation_run_update()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Agent Generation Run cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
            'state', 'attempt_count', 'terminal_attempt_count',
            'terminal_generator_count', 'proposal_count', 'retained_proposal_count',
            'budget_reserved', 'budget_consumed', 'review_evidence_uri',
            'review_evidence_hash', 'error_code', 'error_message', 'version',
            'updated_at', 'finished_at'
        ]::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'state', 'attempt_count', 'terminal_attempt_count',
            'terminal_generator_count', 'proposal_count', 'retained_proposal_count',
            'budget_reserved', 'budget_consumed', 'review_evidence_uri',
            'review_evidence_hash', 'error_code', 'error_message', 'version',
            'updated_at', 'finished_at'
        ]::TEXT[])
    THEN
        RAISE EXCEPTION 'Agent Generation Run immutable authority cannot change';
    END IF;
    IF NOT (
        OLD.state = NEW.state
        OR (OLD.state = 'created' AND NEW.state IN ('running', 'failed', 'cancelled'))
        OR (OLD.state = 'running' AND NEW.state IN ('awaiting_review', 'failed', 'cancelled'))
        OR (OLD.state = 'awaiting_review' AND NEW.state IN ('completed', 'failed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'Agent Generation Run state transition is invalid';
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION 'Agent Generation Run version cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_generation_run_protected
BEFORE UPDATE OR DELETE ON agent_generation_runs
FOR EACH ROW EXECUTE FUNCTION protect_agent_generation_run_update();

CREATE OR REPLACE FUNCTION protect_agent_generator_attempt_update()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Agent Generator Attempt cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
            'state', 'actual', 'worker_id', 'claim_token', 'lease_expires_at',
            'batch_id', 'batch_hash', 'batch_status', 'raw_output_uri',
            'raw_output_hash', 'adapter_provenance', 'error_code', 'error_message',
            'version', 'updated_at', 'started_at', 'finished_at'
        ]::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'state', 'actual', 'worker_id', 'claim_token', 'lease_expires_at',
            'batch_id', 'batch_hash', 'batch_status', 'raw_output_uri',
            'raw_output_hash', 'adapter_provenance', 'error_code', 'error_message',
            'version', 'updated_at', 'started_at', 'finished_at'
        ]::TEXT[])
    THEN
        RAISE EXCEPTION 'Agent Generator Attempt immutable authority cannot change';
    END IF;
    IF NOT (
        OLD.state = NEW.state
        OR (OLD.state = 'pending' AND NEW.state IN ('running', 'cancelled'))
        OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'Agent Generator Attempt state transition is invalid';
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION 'Agent Generator Attempt version cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_generator_attempt_protected
BEFORE UPDATE OR DELETE ON agent_generator_attempts
FOR EACH ROW EXECUTE FUNCTION protect_agent_generator_attempt_update();

CREATE OR REPLACE FUNCTION protect_agent_candidate_proposal_ref_update()
RETURNS TRIGGER AS $$
DECLARE
    retained agent_candidate_proposal_refs%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Agent Candidate Proposal Ref cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['disposition', 'duplicate_of_proposal_id']::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['disposition', 'duplicate_of_proposal_id']::TEXT[])
    THEN
        RAISE EXCEPTION 'Agent Candidate Proposal Ref immutable identity cannot change';
    END IF;
    IF NOT (
        OLD.disposition = NEW.disposition
        OR (OLD.disposition = 'pending' AND NEW.disposition IN ('retained', 'duplicate'))
    ) THEN
        RAISE EXCEPTION 'Agent Candidate Proposal disposition transition is invalid';
    END IF;
    IF NEW.duplicate_of_proposal_id IS NOT NULL THEN
        SELECT * INTO retained FROM agent_candidate_proposal_refs
        WHERE proposal_id = NEW.duplicate_of_proposal_id FOR SHARE;
        IF retained.proposal_id IS NULL
            OR retained.generation_run_id <> NEW.generation_run_id
            OR retained.normalized_patch_hash <> NEW.normalized_patch_hash
            OR retained.disposition NOT IN ('pending', 'retained')
        THEN
            RAISE EXCEPTION 'Agent duplicate Proposal does not bind one retained peer';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_candidate_proposal_ref_protected
BEFORE UPDATE OR DELETE ON agent_candidate_proposal_refs
FOR EACH ROW EXECUTE FUNCTION protect_agent_candidate_proposal_ref_update();

CREATE OR REPLACE FUNCTION reject_agent_generation_budget_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Agent Generation Budget Ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_generation_budget_ledger_append_only
BEFORE UPDATE OR DELETE ON agent_generation_budget_ledger
FOR EACH ROW EXECUTE FUNCTION reject_agent_generation_budget_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (15, 'm2b_agent_generation_authority')
ON CONFLICT (version) DO NOTHING;

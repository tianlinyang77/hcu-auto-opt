CREATE TABLE formal_operator_start_intents (
    intent_id UUID PRIMARY KEY,
    preview_id UUID NOT NULL UNIQUE,
    resolved_plan_hash TEXT NOT NULL,
    formal_authorization_hash TEXT NOT NULL,
    execution_authority_hash TEXT NOT NULL,
    evaluation_authority_hash TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_assertion_hash TEXT NOT NULL,
    actor_signer_id TEXT NOT NULL,
    actor_signer_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_id UUID NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE,
    candidate_bindings JSONB NOT NULL,
    state TEXT NOT NULL,
    blocker_codes JSONB NOT NULL,
    error_code TEXT,
    error_message TEXT,
    service_identity JSONB NOT NULL,
    authority_reconcile_count INTEGER NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_reconciled_at TIMESTAMPTZ,
    ready_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    authority_ready BOOLEAN NOT NULL,
    round_creation_allowed BOOLEAN NOT NULL,
    hcu_accessed BOOLEAN NOT NULL,
    synthetic BOOLEAN NOT NULL,
    automatic_release_allowed BOOLEAN NOT NULL,
    CONSTRAINT formal_start_state_valid CHECK (
        state IN (
            'awaiting_authority', 'ready_for_round_creation',
            'cancelled', 'failed'
        )
    ),
    CONSTRAINT formal_start_hashes_valid CHECK (
        resolved_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND formal_authorization_hash ~ '^sha256:[0-9a-f]{64}$'
        AND execution_authority_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evaluation_authority_hash ~ '^sha256:[0-9a-f]{64}$'
        AND request_digest ~ '^sha256:[0-9a-f]{64}$'
        AND actor_assertion_hash ~ '^sha256:[0-9a-f]{64}$'
        AND actor_signer_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT formal_start_candidate_bindings_array CHECK (
        jsonb_typeof(candidate_bindings) = 'array'
        AND jsonb_array_length(candidate_bindings) BETWEEN 2 AND 4
    ),
    CONSTRAINT formal_start_blockers_array CHECK (
        jsonb_typeof(blocker_codes) = 'array'
    ),
    CONSTRAINT formal_start_service_identity_object CHECK (
        jsonb_typeof(service_identity) = 'object'
    ),
    CONSTRAINT formal_start_authority_state CHECK (
        authority_ready = (state = 'ready_for_round_creation')
        AND (
            (state = 'awaiting_authority' AND jsonb_array_length(blocker_codes) > 0)
            OR (state <> 'awaiting_authority' AND jsonb_array_length(blocker_codes) = 0)
        )
        AND ((state = 'ready_for_round_creation') = (ready_at IS NOT NULL))
    ),
    CONSTRAINT formal_start_error_state CHECK (
        (error_code IS NULL) = (error_message IS NULL)
        AND ((state = 'failed') = (error_code IS NOT NULL))
    ),
    CONSTRAINT formal_start_cancel_state CHECK (
        (state = 'cancelled') = (cancelled_at IS NOT NULL)
    ),
    CONSTRAINT formal_start_nonexecuting_slice CHECK (
        round_creation_allowed = FALSE
        AND hcu_accessed = FALSE
        AND synthetic = FALSE
        AND automatic_release_allowed = FALSE
    ),
    CONSTRAINT formal_start_versions_positive CHECK (
        authority_reconcile_count >= 0 AND version >= 1
    ),
    CONSTRAINT formal_start_time_order CHECK (
        updated_at >= created_at
        AND (last_reconciled_at IS NULL OR last_reconciled_at >= created_at)
        AND (ready_at IS NULL OR ready_at >= created_at)
        AND (cancelled_at IS NULL OR cancelled_at >= created_at)
    )
);

CREATE INDEX formal_operator_start_intents_recovery_idx
ON formal_operator_start_intents (state, updated_at, intent_id)
WHERE state IN ('awaiting_authority', 'ready_for_round_creation');

CREATE TABLE formal_operator_start_intent_events (
    intent_id UUID NOT NULL REFERENCES formal_operator_start_intents(intent_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    blocker_codes JSONB NOT NULL,
    error_code TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (intent_id, sequence),
    CONSTRAINT formal_start_event_sequence_positive CHECK (sequence >= 1),
    CONSTRAINT formal_start_event_type_valid CHECK (
        event_type IN ('created', 'reconciled', 'cancelled')
        AND ((event_type = 'created') = (sequence = 1))
    ),
    CONSTRAINT formal_start_event_state_valid CHECK (
        state IN (
            'awaiting_authority', 'ready_for_round_creation',
            'cancelled', 'failed'
        )
    ),
    CONSTRAINT formal_start_event_blockers_array CHECK (
        jsonb_typeof(blocker_codes) = 'array'
        AND (
            (state = 'awaiting_authority' AND jsonb_array_length(blocker_codes) > 0)
            OR (state <> 'awaiting_authority' AND jsonb_array_length(blocker_codes) = 0)
        )
    ),
    CONSTRAINT formal_start_event_error_state CHECK (
        (state = 'failed') = (error_code IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION reject_formal_start_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Formal StartIntent events are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_start_events_append_only
BEFORE UPDATE OR DELETE ON formal_operator_start_intent_events
FOR EACH ROW EXECUTE FUNCTION reject_formal_start_event_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (18, 'm2a_formal_start_intent')
ON CONFLICT (version) DO NOTHING;

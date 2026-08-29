CREATE TABLE formal_round_signoff_intents (
    signoff_intent_id UUID PRIMARY KEY,
    round_signoff_id UUID NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id) ON DELETE RESTRICT,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    round_evidence_bundle_id UUID NOT NULL UNIQUE,
    evidence_bundle_hash TEXT NOT NULL,
    candidate_family_hash TEXT NOT NULL,
    artifact_family_hash TEXT NOT NULL,
    holdout_family_hash TEXT,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_identity_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    input_digest TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'preparing',
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_signoff_intent_context_fk FOREIGN KEY (
        round_id, authority_context_id, authority_context_hash
    ) REFERENCES formal_round_authority_contexts (
        round_id, authority_context_id, context_hash
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_signoff_intent_evidence_fk FOREIGN KEY (
        round_evidence_bundle_id
    ) REFERENCES formal_round_evidence_bundles (
        round_evidence_bundle_id
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_signoff_intent_decision_known
        CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT formal_signoff_intent_state_known CHECK (
        state IN ('preparing', 'artifact_published', 'finalized', 'failed')
    ),
    CONSTRAINT formal_signoff_intent_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_signoff_intent_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_signoff_intent_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_signoff_intent_text_nonempty CHECK (
        length(actor) BETWEEN 1 AND 300
        AND length(reason) BETWEEN 1 AND 4000
        AND length(idempotency_key) BETWEEN 8 AND 300
    ),
    CONSTRAINT formal_signoff_intent_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evidence_bundle_hash ~ '^sha256:[0-9a-f]{64}$'
        AND candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND artifact_family_hash ~ '^sha256:[0-9a-f]{64}$'
        AND (
            holdout_family_hash IS NULL
            OR holdout_family_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND actor_identity_hash ~ '^sha256:[0-9a-f]{64}$'
        AND input_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    UNIQUE (round_id, signoff_intent_id, round_signoff_id)
);

CREATE OR REPLACE FUNCTION enforce_formal_signoff_intent_parent()
RETURNS TRIGGER AS $$
DECLARE
    parent search_rounds%ROWTYPE;
    parent_task tasks%ROWTYPE;
    evidence formal_round_evidence_bundles%ROWTYPE;
BEGIN
    SELECT * INTO parent FROM search_rounds
    WHERE round_id = NEW.round_id FOR SHARE;
    SELECT * INTO parent_task FROM tasks
    WHERE task_id = NEW.task_id FOR SHARE;
    SELECT * INTO evidence FROM formal_round_evidence_bundles
    WHERE round_evidence_bundle_id = NEW.round_evidence_bundle_id FOR SHARE;
    IF parent.round_id IS NULL
        OR parent_task.task_id IS NULL
        OR evidence.round_evidence_bundle_id IS NULL
        OR parent.run_mode <> 'formal'
        OR parent.state <> 'awaiting_signoff'
        OR parent.automatic_release_allowed
        OR parent.task_id <> NEW.task_id
        OR parent_task.workflow_type <> 'search_round'
        OR parent_task.state <> 'awaiting_signoff'
        OR parent_task.automatic_release_allowed
        OR evidence.round_id <> NEW.round_id
        OR evidence.task_id <> NEW.task_id
        OR evidence.authority_context_id <> NEW.authority_context_id
        OR evidence.authority_context_hash <> NEW.authority_context_hash
        OR evidence.payload_hash <> NEW.evidence_bundle_hash
        OR evidence.candidate_family_hash <> NEW.candidate_family_hash
        OR evidence.artifact_family_hash <> NEW.artifact_family_hash
        OR evidence.holdout_family_hash IS DISTINCT FROM NEW.holdout_family_hash
        OR evidence.synthetic
        OR evidence.automatic_release_allowed
    THEN
        RAISE EXCEPTION 'Formal Signoff Intent disagrees with awaiting Round Evidence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_signoff_intent_parent
BEFORE INSERT ON formal_round_signoff_intents
FOR EACH ROW EXECUTE FUNCTION enforce_formal_signoff_intent_parent();

CREATE TABLE formal_round_signoff_outbox (
    outbox_event_id UUID PRIMARY KEY,
    signoff_intent_id UUID NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL DEFAULT 'formal_round',
    aggregate_id UUID NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'formal_round_signoff_decision_publish',
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    decision_artifact_uri TEXT,
    decision_artifact_hash TEXT,
    signature JSONB,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_signoff_outbox_intent_fk FOREIGN KEY (
        round_id, signoff_intent_id, aggregate_id
    ) REFERENCES formal_round_signoff_intents (
        round_id, signoff_intent_id, round_signoff_id
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_signoff_outbox_aggregate CHECK (
        aggregate_type = 'formal_round'
        AND event_type = 'formal_round_signoff_decision_publish'
    ),
    CONSTRAINT formal_signoff_outbox_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT formal_signoff_outbox_state_known
        CHECK (state IN ('pending', 'artifact_published', 'finalized')),
    CONSTRAINT formal_signoff_outbox_retry_nonnegative CHECK (retry_count >= 0),
    CONSTRAINT formal_signoff_outbox_publication_atomic CHECK (
        (
            state = 'pending'
            AND decision_artifact_uri IS NULL
            AND decision_artifact_hash IS NULL
            AND signature IS NULL
        ) OR (
            state IN ('artifact_published', 'finalized')
            AND decision_artifact_uri IS NOT NULL
            AND decision_artifact_hash IS NOT NULL
            AND signature IS NOT NULL
            AND jsonb_typeof(signature) = 'object'
        )
    ),
    CONSTRAINT formal_signoff_outbox_artifact_file_uri CHECK (
        decision_artifact_uri IS NULL
        OR decision_artifact_uri ~ '^(file:///|file://localhost/)[^?#]+$'
    ),
    CONSTRAINT formal_signoff_outbox_hashes_valid CHECK (
        payload_hash ~ '^sha256:[0-9a-f]{64}$'
        AND (
            decision_artifact_hash IS NULL
            OR decision_artifact_hash ~ '^sha256:[0-9a-f]{64}$'
        )
    ),
    UNIQUE (aggregate_type, aggregate_id, event_type)
);

CREATE TABLE formal_round_signoffs (
    round_signoff_id UUID PRIMARY KEY,
    signoff_intent_id UUID NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE,
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    authority_context_id UUID NOT NULL,
    authority_context_hash TEXT NOT NULL,
    round_evidence_bundle_id UUID NOT NULL UNIQUE,
    evidence_bundle_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_identity_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    input_digest TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    decision_artifact_uri TEXT NOT NULL,
    decision_artifact_hash TEXT NOT NULL UNIQUE,
    signature JSONB NOT NULL,
    run_mode TEXT NOT NULL DEFAULT 'formal',
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_round_signoff_intent_fk FOREIGN KEY (
        round_id, signoff_intent_id, round_signoff_id
    ) REFERENCES formal_round_signoff_intents (
        round_id, signoff_intent_id, round_signoff_id
    ) ON DELETE RESTRICT,
    CONSTRAINT formal_round_signoff_decision_known
        CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT formal_round_signoff_artifact_file_uri CHECK (
        decision_artifact_uri ~ '^(file:///|file://localhost/)[^?#]+$'
    ),
    CONSTRAINT formal_round_signoff_signature_object
        CHECK (jsonb_typeof(signature) = 'object'),
    CONSTRAINT formal_round_signoff_run_mode CHECK (run_mode = 'formal'),
    CONSTRAINT formal_round_signoff_non_synthetic CHECK (synthetic = FALSE),
    CONSTRAINT formal_round_signoff_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT formal_round_signoff_hashes_valid CHECK (
        authority_context_hash ~ '^sha256:[0-9a-f]{64}$'
        AND evidence_bundle_hash ~ '^sha256:[0-9a-f]{64}$'
        AND actor_identity_hash ~ '^sha256:[0-9a-f]{64}$'
        AND input_digest ~ '^sha256:[0-9a-f]{64}$'
        AND decision_artifact_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE OR REPLACE FUNCTION enforce_formal_round_signoff_parent()
RETURNS TRIGGER AS $$
DECLARE
    intent formal_round_signoff_intents%ROWTYPE;
    outbox formal_round_signoff_outbox%ROWTYPE;
    parent search_rounds%ROWTYPE;
    parent_task tasks%ROWTYPE;
BEGIN
    SELECT * INTO intent FROM formal_round_signoff_intents
    WHERE signoff_intent_id = NEW.signoff_intent_id FOR SHARE;
    SELECT * INTO outbox FROM formal_round_signoff_outbox
    WHERE signoff_intent_id = NEW.signoff_intent_id FOR SHARE;
    SELECT * INTO parent FROM search_rounds
    WHERE round_id = NEW.round_id FOR SHARE;
    SELECT * INTO parent_task FROM tasks
    WHERE task_id = NEW.task_id FOR SHARE;
    IF intent.signoff_intent_id IS NULL
        OR outbox.outbox_event_id IS NULL
        OR parent.round_id IS NULL
        OR parent_task.task_id IS NULL
        OR intent.state <> 'artifact_published'
        OR outbox.state <> 'artifact_published'
        OR parent.state <> 'awaiting_signoff'
        OR parent_task.state <> 'awaiting_signoff'
        OR parent.run_mode <> 'formal'
        OR parent.automatic_release_allowed
        OR parent_task.automatic_release_allowed
        OR intent.round_signoff_id <> NEW.round_signoff_id
        OR intent.round_id <> NEW.round_id
        OR intent.task_id <> NEW.task_id
        OR intent.authority_context_id <> NEW.authority_context_id
        OR intent.authority_context_hash <> NEW.authority_context_hash
        OR intent.round_evidence_bundle_id <> NEW.round_evidence_bundle_id
        OR intent.evidence_bundle_hash <> NEW.evidence_bundle_hash
        OR intent.decision <> NEW.decision
        OR intent.actor <> NEW.actor
        OR intent.actor_identity_hash <> NEW.actor_identity_hash
        OR intent.reason <> NEW.reason
        OR intent.decision_at <> NEW.decision_at
        OR intent.input_digest <> NEW.input_digest
        OR intent.idempotency_key <> NEW.idempotency_key
        OR outbox.decision_artifact_uri <> NEW.decision_artifact_uri
        OR outbox.decision_artifact_hash <> NEW.decision_artifact_hash
        OR outbox.signature <> NEW.signature
    THEN
        RAISE EXCEPTION 'Formal Round Signoff disagrees with Intent or published Artifact';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_round_signoff_parent
BEFORE INSERT ON formal_round_signoffs
FOR EACH ROW EXECUTE FUNCTION enforce_formal_round_signoff_parent();

CREATE OR REPLACE FUNCTION protect_formal_signoff_intent_update()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal Signoff Intent cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['state', 'updated_at']::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['state', 'updated_at']::TEXT[])
    THEN
        RAISE EXCEPTION 'Formal Signoff Intent immutable inputs cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'preparing' AND NEW.state IN ('artifact_published', 'failed'))
        OR (OLD.state = 'artifact_published' AND NEW.state = 'finalized')
        OR OLD.state = NEW.state
    ) THEN
        RAISE EXCEPTION 'Formal Signoff Intent state transition is invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_signoff_intent_protected
BEFORE UPDATE OR DELETE ON formal_round_signoff_intents
FOR EACH ROW EXECUTE FUNCTION protect_formal_signoff_intent_update();

CREATE OR REPLACE FUNCTION protect_formal_signoff_outbox_update()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal Signoff Outbox cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
            'state', 'decision_artifact_uri', 'decision_artifact_hash',
            'signature', 'retry_count', 'last_error', 'updated_at'
        ]::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'state', 'decision_artifact_uri', 'decision_artifact_hash',
            'signature', 'retry_count', 'last_error', 'updated_at'
        ]::TEXT[])
    THEN
        RAISE EXCEPTION 'Formal Signoff Outbox immutable inputs cannot change';
    END IF;
    IF NOT (
        (OLD.state = 'pending' AND NEW.state = 'artifact_published')
        OR (OLD.state = 'artifact_published' AND NEW.state = 'finalized')
        OR OLD.state = NEW.state
    ) THEN
        RAISE EXCEPTION 'Formal Signoff Outbox state transition is invalid';
    END IF;
    IF OLD.state IN ('artifact_published', 'finalized') AND (
        NEW.decision_artifact_uri IS DISTINCT FROM OLD.decision_artifact_uri
        OR NEW.decision_artifact_hash IS DISTINCT FROM OLD.decision_artifact_hash
        OR NEW.signature IS DISTINCT FROM OLD.signature
    ) THEN
        RAISE EXCEPTION 'Published Formal Signoff Artifact identity cannot change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_signoff_outbox_protected
BEFORE UPDATE OR DELETE ON formal_round_signoff_outbox
FOR EACH ROW EXECUTE FUNCTION protect_formal_signoff_outbox_update();

CREATE OR REPLACE FUNCTION reject_formal_round_signoff_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Formal Round Signoff is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_round_signoff_append_only
BEFORE UPDATE OR DELETE ON formal_round_signoffs
FOR EACH ROW EXECUTE FUNCTION reject_formal_round_signoff_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (14, 'm2_formal_signoff_outbox')
ON CONFLICT (version) DO NOTHING;

ALTER TABLE operator_plan_previews
ADD CONSTRAINT operator_preview_identity_plan_unique
UNIQUE (preview_id, resolved_plan_hash);

CREATE TABLE operator_start_intents (
    intent_id UUID PRIMARY KEY,
    preview_id UUID NOT NULL UNIQUE,
    resolved_plan_hash TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    task_id UUID NOT NULL UNIQUE,
    round_id UUID NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    candidate_members JSONB NOT NULL,
    search_plan_hash TEXT,
    holdout_plan_commitment TEXT,
    holdout_commitment_scheme TEXT,
    holdout_plan_authority_id TEXT,
    holdout_plan_authority_hash TEXT,
    family_alpha DOUBLE PRECISION,
    candidate_family_hash TEXT,
    error_code TEXT,
    error_message TEXT,
    service_identity JSONB NOT NULL,
    synthetic BOOLEAN NOT NULL,
    automatic_release_allowed BOOLEAN NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    finalized_at TIMESTAMPTZ,
    CONSTRAINT operator_start_state_valid CHECK (
        state IN (
            'preparing', 'plans_frozen', 'round_created',
            'intake_closed', 'finalized', 'failed'
        )
    ),
    CONSTRAINT operator_start_members_array CHECK (
        jsonb_typeof(candidate_members) = 'array'
        AND jsonb_array_length(candidate_members) BETWEEN 2 AND 4
    ),
    CONSTRAINT operator_start_service_identity_object
        CHECK (jsonb_typeof(service_identity) = 'object'),
    CONSTRAINT operator_start_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT operator_start_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT operator_start_version_positive CHECK (version >= 1),
    CONSTRAINT operator_start_request_digest_valid
        CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT operator_start_plan_hash_valid
        CHECK (resolved_plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT operator_start_frozen_hashes_valid CHECK (
        (search_plan_hash IS NULL OR search_plan_hash ~ '^sha256:[0-9a-f]{64}$')
        AND (
            holdout_plan_commitment IS NULL
            OR holdout_plan_commitment ~ '^sha256:[0-9a-f]{64}$'
        )
        AND (
            holdout_plan_authority_hash IS NULL
            OR holdout_plan_authority_hash ~ '^sha256:[0-9a-f]{64}$'
        )
        AND (
            candidate_family_hash IS NULL
            OR candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT operator_start_plan_fields_atomic CHECK (
        (
            search_plan_hash IS NULL
            AND holdout_plan_commitment IS NULL
            AND holdout_commitment_scheme IS NULL
            AND holdout_plan_authority_id IS NULL
            AND holdout_plan_authority_hash IS NULL
            AND family_alpha IS NULL
        ) OR (
            search_plan_hash IS NOT NULL
            AND holdout_plan_commitment IS NOT NULL
            AND holdout_commitment_scheme = 'sha256-nonce-v1'
            AND holdout_plan_authority_id IS NOT NULL
            AND holdout_plan_authority_hash IS NOT NULL
            AND family_alpha > 0.0 AND family_alpha < 1.0
        )
    ),
    CONSTRAINT operator_start_advanced_requires_plans CHECK (
        state IN ('preparing', 'failed') OR search_plan_hash IS NOT NULL
    ),
    CONSTRAINT operator_start_family_matches_state CHECK (
        (state IN ('intake_closed', 'finalized')) =
        (candidate_family_hash IS NOT NULL)
    ),
    CONSTRAINT operator_start_error_pair CHECK (
        (error_code IS NULL) = (error_message IS NULL)
        AND ((state = 'failed') = (error_code IS NOT NULL))
    ),
    CONSTRAINT operator_start_finalized_time CHECK (
        (state = 'finalized') = (finalized_at IS NOT NULL)
    ),
    CONSTRAINT operator_start_time_order CHECK (
        updated_at >= created_at
        AND (finalized_at IS NULL OR finalized_at >= created_at)
    ),
    CONSTRAINT operator_start_preview_plan_fk
        FOREIGN KEY (preview_id, resolved_plan_hash)
        REFERENCES operator_plan_previews(preview_id, resolved_plan_hash)
);

CREATE INDEX operator_start_intents_state_idx
ON operator_start_intents (state, updated_at, intent_id);

INSERT INTO schema_migrations (version, name)
VALUES (11, 'operator_start_intent')
ON CONFLICT (version) DO NOTHING;

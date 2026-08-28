CREATE TABLE operator_plan_previews (
    preview_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    preview_request_digest TEXT NOT NULL,
    resolved_plan_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    synthetic BOOLEAN NOT NULL,
    automatic_release_allowed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT operator_preview_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT operator_preview_payload_id_matches
        CHECK ((payload ->> 'preview_id' = preview_id::TEXT) IS TRUE),
    CONSTRAINT operator_preview_payload_request_digest_matches
        CHECK ((payload ->> 'preview_request_digest' = preview_request_digest) IS TRUE),
    CONSTRAINT operator_preview_payload_plan_hash_matches
        CHECK ((payload ->> 'resolved_plan_hash' = resolved_plan_hash) IS TRUE),
    CONSTRAINT operator_preview_payload_safety_matches
        CHECK ((
            payload ->> 'synthetic' = 'true'
            AND payload ->> 'automatic_release_allowed' = 'false'
        ) IS TRUE),
    CONSTRAINT operator_preview_scripted_only CHECK (synthetic = TRUE),
    CONSTRAINT operator_preview_never_auto_releases
        CHECK (automatic_release_allowed = FALSE),
    CONSTRAINT operator_preview_expiry_after_creation CHECK (expires_at > created_at),
    CONSTRAINT operator_preview_request_digest_valid
        CHECK (preview_request_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT operator_preview_plan_hash_valid
        CHECK (resolved_plan_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX operator_plan_previews_expiry_idx
ON operator_plan_previews (expires_at, preview_id);

CREATE OR REPLACE FUNCTION reject_operator_plan_preview_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Operator Plan Previews are immutable; create a new Preview';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER operator_plan_previews_append_only
BEFORE UPDATE OR DELETE ON operator_plan_previews
FOR EACH ROW EXECUTE FUNCTION reject_operator_plan_preview_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (10, 'operator_plan_preview')
ON CONFLICT (version) DO NOTHING;

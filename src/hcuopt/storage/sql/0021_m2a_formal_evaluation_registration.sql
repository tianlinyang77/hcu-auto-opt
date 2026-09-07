CREATE TABLE formal_evaluation_start_registrations (
    registration_id UUID PRIMARY KEY,
    preview_id UUID NOT NULL UNIQUE,
    formal_authorization_hash TEXT NOT NULL,
    resolved_plan_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    registration JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT formal_evaluation_registration_hashes CHECK (
        formal_authorization_hash ~ '^sha256:[0-9a-f]{64}$'
        AND resolved_plan_hash ~ '^sha256:[0-9a-f]{64}$'
        AND content_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT formal_evaluation_registration_binding CHECK ((
        registration->>'schema_version' = 'm2a-formal-evaluation-start-registration-v1'
        AND registration->>'registration_id' = registration_id::text
        AND registration->>'preview_id' = preview_id::text
        AND registration->>'formal_authorization_hash' = formal_authorization_hash
        AND registration->>'resolved_plan_hash' = resolved_plan_hash
        AND registration->>'synthetic' = 'false'
        AND registration->>'automatic_release_allowed' = 'false'
    ) IS TRUE)
);

CREATE FUNCTION reject_formal_evaluation_registration_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Formal evaluation registrations are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_evaluation_registrations_append_only
BEFORE UPDATE OR DELETE ON formal_evaluation_start_registrations
FOR EACH ROW EXECUTE FUNCTION reject_formal_evaluation_registration_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (21, 'm2a_formal_evaluation_registration')
ON CONFLICT (version) DO NOTHING;

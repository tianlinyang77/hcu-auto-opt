CREATE OR REPLACE FUNCTION endpoint_uuid_array_is_distinct(values_to_check UUID[])
RETURNS BOOLEAN AS $$
    SELECT cardinality(values_to_check) = (
        SELECT count(DISTINCT value) FROM unnest(values_to_check) AS item(value)
    );
$$ LANGUAGE SQL IMMUTABLE STRICT;

CREATE TABLE endpoint_validation_campaigns (
    campaign_id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    signed_m1_task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    adapter_profile TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    endpoint_run_ids UUID[] NOT NULL,
    adjudication_request JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'awaiting_adjudication',
    adjudication_result JSONB,
    idempotency_key TEXT NOT NULL UNIQUE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT endpoint_campaign_name_nonempty CHECK (length(name) > 0),
    CONSTRAINT endpoint_campaign_eight_groups CHECK (cardinality(endpoint_run_ids) = 8),
    CONSTRAINT endpoint_campaign_distinct_groups CHECK (
        endpoint_uuid_array_is_distinct(endpoint_run_ids)
    ),
    CONSTRAINT endpoint_campaign_environment_hash_shape CHECK (
        environment_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT endpoint_campaign_state_known CHECK (
        state IN (
            'awaiting_adjudication', 'adjudicating', 'awaiting_signoff',
            'completed', 'rejected', 'invalid'
        )
    ),
    CONSTRAINT endpoint_campaign_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    )
);

CREATE INDEX endpoint_campaign_signed_m1_idx
ON endpoint_validation_campaigns (signed_m1_task_id, created_at);

CREATE OR REPLACE FUNCTION reject_endpoint_campaign_binding_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.campaign_id <> OLD.campaign_id
       OR NEW.name <> OLD.name
       OR NEW.signed_m1_task_id <> OLD.signed_m1_task_id
       OR NEW.target_snapshot_id <> OLD.target_snapshot_id
       OR NEW.adapter_profile <> OLD.adapter_profile
       OR NEW.environment_fingerprint <> OLD.environment_fingerprint
       OR NEW.endpoint_run_ids <> OLD.endpoint_run_ids
       OR NEW.adjudication_request <> OLD.adjudication_request
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.automatic_release_allowed <> OLD.automatic_release_allowed THEN
        RAISE EXCEPTION 'endpoint campaign bindings are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER endpoint_campaign_bindings_immutable
BEFORE UPDATE ON endpoint_validation_campaigns
FOR EACH ROW EXECUTE FUNCTION reject_endpoint_campaign_binding_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (24, 'endpoint_campaign_control_plane')
ON CONFLICT (version) DO NOTHING;

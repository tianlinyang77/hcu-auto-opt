CREATE TABLE endpoint_campaign_signoffs (
    signoff_id UUID PRIMARY KEY,
    campaign_id UUID NOT NULL UNIQUE
        REFERENCES endpoint_validation_campaigns(campaign_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    adjudication_result_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT endpoint_campaign_signoff_decision_known CHECK (
        decision IN ('accepted', 'rejected')
    ),
    CONSTRAINT endpoint_campaign_signoff_actor_nonempty CHECK (length(actor) > 0),
    CONSTRAINT endpoint_campaign_signoff_reason_nonempty CHECK (length(reason) > 0),
    CONSTRAINT endpoint_campaign_signoff_result_hash_valid CHECK (
        adjudication_result_sha256 ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT endpoint_campaign_signoff_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    )
);

CREATE OR REPLACE FUNCTION reject_endpoint_campaign_signoff_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'endpoint campaign signoffs are immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER endpoint_campaign_signoffs_immutable
BEFORE UPDATE OR DELETE ON endpoint_campaign_signoffs
FOR EACH ROW EXECUTE FUNCTION reject_endpoint_campaign_signoff_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (26, 'endpoint_campaign_signoff')
ON CONFLICT (version) DO NOTHING;

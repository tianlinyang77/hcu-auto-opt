ALTER TABLE endpoint_validation_campaigns
ADD COLUMN IF NOT EXISTS adjudication_job_id UUID
    REFERENCES jobs(job_id) ON DELETE RESTRICT;

ALTER TABLE endpoint_validation_campaigns
DROP CONSTRAINT IF EXISTS endpoint_campaign_state_known;

ALTER TABLE endpoint_validation_campaigns
ADD CONSTRAINT endpoint_campaign_state_known CHECK (
    state IN (
        'awaiting_adjudication', 'adjudicating', 'adjudication_failed',
        'awaiting_signoff', 'completed', 'rejected', 'invalid'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS endpoint_campaign_adjudication_job_idx
ON endpoint_validation_campaigns (adjudication_job_id)
WHERE adjudication_job_id IS NOT NULL;

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
       OR NEW.automatic_release_allowed <> OLD.automatic_release_allowed
       OR NEW.adjudication_job_id IS DISTINCT FROM OLD.adjudication_job_id THEN
        RAISE EXCEPTION 'endpoint campaign bindings are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO schema_migrations (version, name)
VALUES (25, 'endpoint_adjudication_jobs')
ON CONFLICT (version) DO NOTHING;

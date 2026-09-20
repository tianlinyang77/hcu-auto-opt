CREATE TABLE endpoint_validation_runs (
    endpoint_run_id UUID PRIMARY KEY,
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    job_id UUID UNIQUE,
    signed_m1_task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    target_snapshot_id UUID NOT NULL
        REFERENCES target_snapshots(target_snapshot_id) ON DELETE RESTRICT,
    adapter_profile TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    workload JSONB NOT NULL,
    plan JSONB NOT NULL,
    plan_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    request_payload JSONB NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT endpoint_validation_state_known CHECK (
        state IN ('queued', 'running', 'provisional_passed', 'failed')
    ),
    CONSTRAINT endpoint_validation_plan_hash_shape CHECK (
        plan_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT endpoint_validation_environment_hash_shape CHECK (
        environment_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT endpoint_validation_never_auto_releases CHECK (
        automatic_release_allowed = FALSE
    )
);

ALTER TABLE endpoint_validation_runs
    ADD CONSTRAINT endpoint_validation_job_fk
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE RESTRICT;

CREATE INDEX endpoint_validation_signed_m1_idx
ON endpoint_validation_runs (signed_m1_task_id, created_at);

CREATE OR REPLACE FUNCTION reject_endpoint_validation_binding_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.endpoint_run_id <> OLD.endpoint_run_id
       OR NEW.task_id <> OLD.task_id
       OR NEW.job_id <> OLD.job_id
       OR NEW.signed_m1_task_id <> OLD.signed_m1_task_id
       OR NEW.target_snapshot_id <> OLD.target_snapshot_id
       OR NEW.adapter_profile <> OLD.adapter_profile
       OR NEW.environment_fingerprint <> OLD.environment_fingerprint
       OR NEW.workload <> OLD.workload
       OR NEW.plan <> OLD.plan
       OR NEW.plan_hash <> OLD.plan_hash
       OR NEW.request_payload <> OLD.request_payload
       OR NEW.idempotency_key <> OLD.idempotency_key
       OR NEW.automatic_release_allowed <> OLD.automatic_release_allowed THEN
        RAISE EXCEPTION 'endpoint validation bindings are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER endpoint_validation_bindings_immutable
BEFORE UPDATE ON endpoint_validation_runs
FOR EACH ROW EXECUTE FUNCTION reject_endpoint_validation_binding_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (22, 'endpoint_validation_control_plane')
ON CONFLICT (version) DO NOTHING;

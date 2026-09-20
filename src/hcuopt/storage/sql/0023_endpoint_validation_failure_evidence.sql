ALTER TABLE endpoint_validation_runs
    ADD COLUMN failure_error JSONB,
    ADD COLUMN cleanup_evidence JSONB;

INSERT INTO schema_migrations (version, name)
VALUES (23, 'endpoint_validation_failure_evidence')
ON CONFLICT (version) DO NOTHING;

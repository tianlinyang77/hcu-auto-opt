ALTER TABLE execution_requests
    ADD COLUMN variant TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE execution_requests
    ADD CONSTRAINT execution_requests_variant_known
        CHECK (variant IN ('legacy', 'baseline', 'noop'));

ALTER TABLE execution_attempts
    ADD COLUMN variant TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE execution_attempts
    DROP CONSTRAINT execution_attempts_run_attempt_key;

ALTER TABLE execution_attempts
    ADD CONSTRAINT execution_attempts_variant_known
        CHECK (variant IN ('legacy', 'baseline', 'noop'));

ALTER TABLE execution_attempts
    ADD CONSTRAINT execution_attempts_run_variant_attempt_key
        UNIQUE (evaluation_run_id, variant, attempt_number);

INSERT INTO schema_migrations (version, name)
VALUES (4, 'framework_smoke_dual_execution')
ON CONFLICT (version) DO NOTHING;

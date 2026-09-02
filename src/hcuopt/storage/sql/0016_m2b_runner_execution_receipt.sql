ALTER TABLE agent_generator_attempts
    ADD COLUMN batch_uri TEXT,
    ADD COLUMN runner_receipt_id UUID,
    ADD COLUMN runner_receipt_uri TEXT,
    ADD COLUMN runner_receipt_hash TEXT,
    ADD COLUMN runner_receipt_schema_version TEXT,
    ADD COLUMN runner_provenance JSONB;

ALTER TABLE agent_generator_attempts
    ADD CONSTRAINT agent_generator_attempt_runner_receipt_atomic CHECK (
        (
            runner_receipt_id IS NULL
            AND runner_receipt_uri IS NULL
            AND runner_receipt_hash IS NULL
            AND runner_receipt_schema_version IS NULL
            AND runner_provenance IS NULL
        ) OR (
            runner_receipt_id IS NOT NULL
            AND runner_receipt_uri IS NOT NULL
            AND runner_receipt_hash ~ '^sha256:[0-9a-f]{64}$'
            AND runner_receipt_schema_version = 'm2b-runner-execution-receipt-v1'
            AND jsonb_typeof(runner_provenance) = 'object'
        )
    );

CREATE OR REPLACE FUNCTION protect_agent_generator_attempt_update()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Agent Generator Attempt cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
            'state', 'actual', 'worker_id', 'claim_token', 'lease_expires_at',
            'batch_id', 'batch_uri', 'batch_hash', 'batch_status', 'raw_output_uri',
            'raw_output_hash', 'adapter_provenance',
            'runner_receipt_id', 'runner_receipt_uri', 'runner_receipt_hash',
            'runner_receipt_schema_version', 'runner_provenance',
            'error_code', 'error_message', 'version', 'updated_at',
            'started_at', 'finished_at'
        ]::TEXT[])
        IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'state', 'actual', 'worker_id', 'claim_token', 'lease_expires_at',
            'batch_id', 'batch_uri', 'batch_hash', 'batch_status', 'raw_output_uri',
            'raw_output_hash', 'adapter_provenance',
            'runner_receipt_id', 'runner_receipt_uri', 'runner_receipt_hash',
            'runner_receipt_schema_version', 'runner_provenance',
            'error_code', 'error_message', 'version', 'updated_at',
            'started_at', 'finished_at'
        ]::TEXT[])
    THEN
        RAISE EXCEPTION 'Agent Generator Attempt immutable authority cannot change';
    END IF;
    IF NOT (
        OLD.state = NEW.state
        OR (OLD.state = 'pending' AND NEW.state IN ('running', 'cancelled'))
        OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'Agent Generator Attempt state transition is invalid';
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION 'Agent Generator Attempt version cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO schema_migrations (version, name)
VALUES (16, 'm2b_runner_execution_receipt')
ON CONFLICT (version) DO NOTHING;

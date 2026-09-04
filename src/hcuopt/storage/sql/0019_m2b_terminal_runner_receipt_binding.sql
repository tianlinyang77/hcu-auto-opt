ALTER TABLE agent_generator_attempts
    ADD CONSTRAINT agent_generator_attempt_runner_receipt_state_binding CHECK (
        runner_receipt_id IS NULL OR state IN ('succeeded', 'failed')
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

    IF OLD.state IN ('succeeded', 'failed', 'cancelled')
       AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)
    THEN
        RAISE EXCEPTION 'Agent Generator Attempt terminal evidence cannot change';
    END IF;

    IF OLD.runner_receipt_id IS NOT NULL AND (
        NEW.runner_receipt_id IS DISTINCT FROM OLD.runner_receipt_id
        OR NEW.runner_receipt_uri IS DISTINCT FROM OLD.runner_receipt_uri
        OR NEW.runner_receipt_hash IS DISTINCT FROM OLD.runner_receipt_hash
        OR NEW.runner_receipt_schema_version IS DISTINCT FROM OLD.runner_receipt_schema_version
        OR NEW.runner_provenance IS DISTINCT FROM OLD.runner_provenance
    ) THEN
        RAISE EXCEPTION 'Agent Generator Attempt Runner Receipt binding cannot change';
    END IF;

    IF OLD.runner_receipt_id IS NULL AND NEW.runner_receipt_id IS NOT NULL
       AND NOT (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed'))
    THEN
        RAISE EXCEPTION 'Agent Generator Attempt Runner Receipt can only bind at settlement';
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
VALUES (19, 'm2b_terminal_runner_receipt_binding')
ON CONFLICT (version) DO NOTHING;

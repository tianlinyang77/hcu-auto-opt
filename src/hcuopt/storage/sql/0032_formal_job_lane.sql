-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- Existing jobs remain on the general lane; formal jobs require a dedicated consumer.
ALTER TABLE jobs ADD COLUMN execution_lane TEXT NOT NULL DEFAULT 'general'
    CHECK (execution_lane IN ('general', 'formal'));
CREATE FUNCTION guard_job_execution_lane() RETURNS trigger AS $$
BEGIN
    IF NEW.execution_lane IS DISTINCT FROM OLD.execution_lane THEN
        RAISE EXCEPTION 'Job execution lane is immutable';
    END IF;
    IF OLD.execution_lane = 'formal' AND (
        NEW.task_id IS DISTINCT FROM OLD.task_id OR NEW.job_type IS DISTINCT FROM OLD.job_type
        OR NEW.payload IS DISTINCT FROM OLD.payload
        OR NEW.accepted_worker_type IS DISTINCT FROM OLD.accepted_worker_type
        OR NEW.adapter_profile IS DISTINCT FROM OLD.adapter_profile
        OR NEW.lease_scope IS DISTINCT FROM OLD.lease_scope
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
    ) THEN RAISE EXCEPTION 'Formal Job binding is immutable'; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER job_execution_lane_guard BEFORE UPDATE ON jobs
FOR EACH ROW EXECUTE FUNCTION guard_job_execution_lane();
CREATE FUNCTION guard_formal_build_job_binding() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM round_budget_reservations b JOIN jobs j ON j.job_id = b.job_id
        WHERE b.reservation_id = NEW.reservation_id AND b.candidate_id = NEW.candidate_id
            AND j.execution_lane = 'formal' AND j.state = 'running'
            AND j.job_type = 'manual_build' AND j.accepted_worker_type = 'build'
            AND j.attempts = 1 AND b.attempt = 1 AND j.max_attempts = 1
            AND j.lease_scope = 'none' AND j.claimed_by = NEW.worker_id
            AND j.claim_token = NEW.claim_token
            AND j.payload->>'intent_id' = NEW.intent_id::text
            AND j.payload->>'candidate_id' = NEW.candidate_id::text
    ) THEN RAISE EXCEPTION 'Formal build journal requires its isolated running Job'; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_build_job_binding_guard BEFORE INSERT ON formal_build_journal
FOR EACH ROW EXECUTE FUNCTION guard_formal_build_job_binding();
INSERT INTO schema_migrations(version, name) VALUES (32, 'formal_job_lane');

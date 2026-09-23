-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- A stop request is not a cleanup receipt or resource release.
ALTER TABLE formal_dispatch_claims ADD CONSTRAINT formal_claim_owner_unique
    UNIQUE (intent_id, claim_token, worker_id);

CREATE TABLE formal_dispatch_stop_requests (
    intent_id UUID PRIMARY KEY,
    claim_token UUID NOT NULL,
    worker_id TEXT NOT NULL,
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 128),
    reason TEXT NOT NULL CHECK (reason IN ('operator_request', 'deployment_shutdown')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (intent_id, claim_token, worker_id)
        REFERENCES formal_dispatch_claims(intent_id, claim_token, worker_id)
);

CREATE TRIGGER formal_stop_append_only
BEFORE UPDATE OR DELETE ON formal_dispatch_stop_requests
FOR EACH ROW EXECUTE FUNCTION reject_formal_start_event_mutation();

CREATE FUNCTION guard_formal_stop_request() RETURNS trigger AS $$
BEGIN
    -- Share the lock used by claims, timeout marking and pre-execution checks.
    PERFORM 1 FROM formal_operator_start_intents WHERE intent_id = NEW.intent_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM formal_dispatch_claims
        WHERE intent_id = NEW.intent_id AND claim_token = NEW.claim_token
          AND worker_id = NEW.worker_id AND claimed_at <= NEW.requested_at
    ) THEN
        RAISE EXCEPTION 'Formal stop request must bind the current claim';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_stop_binding BEFORE INSERT ON formal_dispatch_stop_requests
FOR EACH ROW EXECUTE FUNCTION guard_formal_stop_request();

INSERT INTO schema_migrations(version, name) VALUES (29, 'formal_dispatch_stop');

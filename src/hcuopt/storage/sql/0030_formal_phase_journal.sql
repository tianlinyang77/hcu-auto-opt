-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- 'invoking' means an external call MAY have started, not proof of HCU activity.
CREATE TABLE formal_phase_journal (
    execution_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL,
    claim_token UUID NOT NULL,
    worker_id TEXT NOT NULL,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id),
    phase TEXT NOT NULL CHECK (phase IN ('search', 'holdout')),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    state TEXT NOT NULL DEFAULT 'invoking'
        CHECK (state IN ('invoking', 'receipt_recorded', 'recovery_required')),
    receipt_ref JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    UNIQUE (intent_id, candidate_id, phase),
    FOREIGN KEY (intent_id, claim_token, worker_id)
        REFERENCES formal_dispatch_claims(intent_id, claim_token, worker_id),
    CHECK ((state = 'invoking') = (finished_at IS NULL)),
    CHECK ((state = 'receipt_recorded') = (receipt_ref IS NOT NULL)),
    CHECK (receipt_ref IS NULL OR jsonb_typeof(receipt_ref) = 'object'),
    CHECK (finished_at IS NULL OR finished_at >= created_at)
);
CREATE TABLE formal_phase_journal_events (
    execution_id UUID NOT NULL REFERENCES formal_phase_journal(execution_id),
    state TEXT NOT NULL CHECK (state IN ('invoking', 'receipt_recorded', 'recovery_required')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (execution_id, state)
);
CREATE TRIGGER formal_phase_events_append_only BEFORE UPDATE OR DELETE
ON formal_phase_journal_events FOR EACH ROW EXECUTE FUNCTION reject_formal_start_event_mutation();

CREATE FUNCTION guard_formal_phase_journal() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal phase journal cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        PERFORM 1 FROM formal_operator_start_intents WHERE intent_id = NEW.intent_id FOR UPDATE;
        IF NEW.state <> 'invoking' OR NOT EXISTS (
            SELECT 1 FROM formal_dispatch_claims c
            JOIN formal_round_dispatches d ON d.intent_id = c.intent_id
            WHERE c.intent_id = NEW.intent_id AND c.claim_token = NEW.claim_token
              AND c.worker_id = NEW.worker_id AND c.state = 'claimed'
              AND c.expires_at > clock_timestamp() AND d.state = 'queued'
              AND d.valid_from <= clock_timestamp() AND d.valid_until > clock_timestamp()
        ) OR EXISTS (SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = NEW.intent_id)
        THEN RAISE EXCEPTION 'Formal phase journal requires a live unstopped claim'; END IF;
    ELSE
        IF (to_jsonb(NEW) - ARRAY['state','receipt_ref','finished_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','receipt_ref','finished_at'])
           OR OLD.state <> 'invoking' OR NEW.state NOT IN ('receipt_recorded', 'recovery_required')
        THEN RAISE EXCEPTION 'Formal phase journal identity or terminal is immutable'; END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_phase_journal_guard BEFORE INSERT OR UPDATE OR DELETE
ON formal_phase_journal FOR EACH ROW EXECUTE FUNCTION guard_formal_phase_journal();

CREATE FUNCTION audit_formal_phase_journal() RETURNS trigger AS $$
BEGIN
    INSERT INTO formal_phase_journal_events(execution_id, state) VALUES (NEW.execution_id, NEW.state);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_phase_journal_audit AFTER INSERT OR UPDATE ON formal_phase_journal
FOR EACH ROW EXECUTE FUNCTION audit_formal_phase_journal();
INSERT INTO schema_migrations(version, name) VALUES (30, 'formal_phase_journal');

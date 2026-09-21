-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- A build slot is never automatically retried, including after a lost response.
CREATE TABLE formal_build_journal (
    intent_id UUID NOT NULL,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id),
    worker_id TEXT NOT NULL,
    claim_token UUID NOT NULL,
    reservation_id UUID NOT NULL UNIQUE REFERENCES round_budget_reservations(reservation_id),
    input_hash TEXT NOT NULL CHECK (input_hash ~ '^sha256:[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'invoking'
        CHECK (state IN ('invoking', 'result_recorded', 'recovery_required')),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (intent_id, candidate_id),
    FOREIGN KEY (intent_id, claim_token, worker_id)
        REFERENCES formal_dispatch_claims(intent_id, claim_token, worker_id),
    CHECK ((state = 'invoking') = (finished_at IS NULL)),
    CHECK ((state = 'result_recorded') = (result IS NOT NULL)),
    CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    CHECK (finished_at IS NULL OR finished_at >= created_at)
);
CREATE FUNCTION guard_formal_build_journal() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal build journal cannot be deleted';
    END IF;
    PERFORM 1 FROM formal_operator_start_intents WHERE intent_id = NEW.intent_id FOR UPDATE;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'invoking' OR NOT EXISTS (
            SELECT 1 FROM formal_dispatch_claims c
            JOIN formal_round_dispatches d ON d.intent_id = c.intent_id
            JOIN round_candidates m ON m.round_id = d.round_id
            JOIN round_budget_reservations b ON b.round_id = d.round_id
                AND b.candidate_id = m.candidate_id
            WHERE c.intent_id = NEW.intent_id AND c.claim_token = NEW.claim_token
                AND c.worker_id = NEW.worker_id AND c.state = 'claimed'
                AND c.expires_at > clock_timestamp() AND d.state = 'queued'
                AND d.valid_from <= clock_timestamp() AND d.valid_until > clock_timestamp()
                AND m.candidate_id = NEW.candidate_id AND m.state = 'intake_accepted'
                AND m.candidate_kind = 'business'
                AND b.reservation_id = NEW.reservation_id AND b.state = 'reserved'
                AND b.phase IS NULL AND b.planned->>'build_attempts' = '1'
        ) OR EXISTS (SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = NEW.intent_id)
        THEN RAISE EXCEPTION 'Formal build invocation requires live claim and reserved budget'; END IF;
    ELSE
        IF (to_jsonb(NEW) - ARRAY['state','result','finished_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','result','finished_at'])
           OR OLD.state <> 'invoking' OR NEW.state NOT IN ('result_recorded','recovery_required')
        THEN RAISE EXCEPTION 'Formal build journal identity or terminal is immutable'; END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_build_journal_guard BEFORE INSERT OR UPDATE OR DELETE
ON formal_build_journal FOR EACH ROW EXECUTE FUNCTION guard_formal_build_journal();
INSERT INTO schema_migrations(version, name) VALUES (31, 'formal_build_journal');

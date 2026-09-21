-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- Control-plane ownership only; this is NOT a hardware lease or execution permit.
CREATE TABLE formal_dispatch_claims (
    intent_id UUID PRIMARY KEY REFERENCES formal_round_dispatches(intent_id),
    claim_token UUID NOT NULL UNIQUE,
    worker_id TEXT NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 128),
    state TEXT NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed', 'recovery_required')),
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    recovery_at TIMESTAMPTZ,
    CHECK (expires_at > claimed_at),
    CHECK ((state = 'recovery_required') = (recovery_at IS NOT NULL)),
    CHECK (recovery_at IS NULL OR recovery_at >= expires_at)
);

CREATE TABLE formal_dispatch_claim_events (
    intent_id UUID NOT NULL REFERENCES formal_dispatch_claims(intent_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('claimed', 'recovery_required')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (intent_id, event_type)
);
CREATE TRIGGER formal_claim_events_append_only
BEFORE UPDATE OR DELETE ON formal_dispatch_claim_events
FOR EACH ROW EXECUTE FUNCTION reject_formal_start_event_mutation();

CREATE FUNCTION guard_formal_claim() RETURNS trigger AS $$
DECLARE dispatch_row formal_round_dispatches%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal claims cannot be deleted or requeued';
    END IF;
    IF TG_OP = 'INSERT' THEN
        -- Same first lock as cancellation; do not acquire Dispatch before Intent.
        PERFORM 1 FROM formal_operator_start_intents WHERE intent_id = NEW.intent_id FOR UPDATE;
        SELECT * INTO dispatch_row FROM formal_round_dispatches WHERE intent_id = NEW.intent_id;
        IF dispatch_row.state IS DISTINCT FROM 'queued'
           OR NEW.claimed_at < dispatch_row.valid_from
           OR NEW.expires_at > dispatch_row.valid_until
           OR clock_timestamp() >= NEW.expires_at
           OR NEW.state <> 'claimed' THEN
            RAISE EXCEPTION 'Formal claim is not eligible';
        END IF;
    ELSE
        IF (to_jsonb(NEW) - ARRAY['state','recovery_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','recovery_at'])
           OR OLD.state <> 'claimed' OR NEW.state <> 'recovery_required'
           OR clock_timestamp() < OLD.expires_at THEN
            RAISE EXCEPTION 'Formal claim mutation is not allowed';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER formal_claim_guard BEFORE INSERT OR UPDATE OR DELETE ON formal_dispatch_claims
FOR EACH ROW EXECUTE FUNCTION guard_formal_claim();

CREATE FUNCTION reject_claimed_intent_cancel() RETURNS trigger AS $$
BEGIN
    IF NEW.state = 'cancelled' AND EXISTS (
        SELECT 1 FROM formal_dispatch_claims WHERE intent_id = OLD.intent_id
    ) THEN
        RAISE EXCEPTION 'Claimed Formal intent requires controlled stop and recovery';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER claimed_intent_cancel_guard BEFORE UPDATE ON formal_operator_start_intents
FOR EACH ROW EXECUTE FUNCTION reject_claimed_intent_cancel();

INSERT INTO schema_migrations(version, name) VALUES (28, 'formal_dispatch_claim');

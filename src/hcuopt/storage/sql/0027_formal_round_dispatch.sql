-- Copyright (c) 2026 Hygon Information Technology Co., Ltd.
-- Formal creation outbox. No generic Job and no hardware execution is enabled.
CREATE TABLE formal_round_dispatches (
    intent_id UUID PRIMARY KEY REFERENCES formal_operator_start_intents(intent_id),
    task_id UUID NOT NULL UNIQUE REFERENCES tasks(task_id),
    round_id UUID NOT NULL UNIQUE REFERENCES search_rounds(round_id),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    request_digest TEXT NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    resolved_plan_hash TEXT NOT NULL CHECK (resolved_plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    candidate_family_hash TEXT NOT NULL CHECK (candidate_family_hash ~ '^sha256:[0-9a-f]{64}$'),
    service_identity JSONB NOT NULL CHECK (jsonb_typeof(service_identity) = 'object'),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'cancelled')),
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT automatic_release_allowed),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    cancelled_at TIMESTAMPTZ,
    CHECK (valid_until > valid_from),
    CHECK (created_at >= valid_from AND created_at < valid_until),
    CHECK ((state = 'cancelled') = (cancelled_at IS NOT NULL))
);

CREATE TABLE formal_round_dispatch_events (
    intent_id UUID NOT NULL REFERENCES formal_round_dispatches(intent_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('queued', 'cancelled')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (intent_id, event_type)
);

CREATE TRIGGER formal_dispatch_events_append_only
BEFORE UPDATE OR DELETE ON formal_round_dispatch_events
FOR EACH ROW EXECUTE FUNCTION reject_formal_start_event_mutation();

CREATE FUNCTION guard_formal_dispatch_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Formal dispatch records cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - 'state' - 'cancelled_at') IS DISTINCT FROM
       (to_jsonb(OLD) - 'state' - 'cancelled_at') THEN
        RAISE EXCEPTION 'Formal dispatch identity is immutable';
    END IF;
    IF OLD.state <> 'queued' OR NEW.state <> 'cancelled' THEN
        RAISE EXCEPTION 'Formal dispatch transition is not allowed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM formal_operator_start_intents
        WHERE intent_id = OLD.intent_id AND state = 'cancelled'
    ) THEN
        RAISE EXCEPTION 'Cancel dispatch through the locked Intent';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_dispatch_immutable
BEFORE UPDATE OR DELETE ON formal_round_dispatches
FOR EACH ROW EXECUTE FUNCTION guard_formal_dispatch_mutation();

-- Protect old writers as well: reconciliation may not rewrite a dispatched Intent.
CREATE FUNCTION guard_dispatched_formal_intent() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM formal_round_dispatches WHERE intent_id = OLD.intent_id) THEN
        IF NEW.state <> 'cancelled' OR OLD.state <> 'ready_for_round_creation' THEN
            RAISE EXCEPTION 'Dispatched Intent requires dispatch-aware recovery';
        END IF;
        IF (to_jsonb(NEW) - ARRAY['state','blocker_codes','ready_at','authority_ready',
            'cancelled_at','version','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','blocker_codes','ready_at','authority_ready',
            'cancelled_at','version','updated_at']) THEN
            RAISE EXCEPTION 'Dispatched Intent identity is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dispatched_formal_intent_guard
BEFORE UPDATE ON formal_operator_start_intents
FOR EACH ROW EXECUTE FUNCTION guard_dispatched_formal_intent();

CREATE FUNCTION cancel_queued_formal_dispatch() RETURNS trigger AS $$
DECLARE bound_round UUID;
DECLARE bound_task UUID;
BEGIN
    IF NEW.state = 'cancelled' AND OLD.state <> 'cancelled' THEN
        UPDATE formal_round_dispatches SET state = 'cancelled', cancelled_at = NEW.cancelled_at
        WHERE intent_id = NEW.intent_id AND state = 'queued'
        RETURNING round_id, task_id INTO bound_round, bound_task;
        IF bound_round IS NOT NULL THEN
            UPDATE search_rounds SET state = 'cancelled', version = version + 1,
                updated_at = clock_timestamp() WHERE round_id = bound_round;
            UPDATE tasks SET state = 'cancelled', version = version + 1,
                updated_at = clock_timestamp() WHERE task_id = bound_task;
            INSERT INTO formal_round_dispatch_events(intent_id, event_type)
            VALUES (NEW.intent_id, 'cancelled');
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_intent_cancels_dispatch
AFTER UPDATE ON formal_operator_start_intents
FOR EACH ROW EXECUTE FUNCTION cancel_queued_formal_dispatch();

INSERT INTO schema_migrations(version, name) VALUES (27, 'formal_round_dispatch');

CREATE TABLE formal_evidence_acceptance_snapshots (
    round_id UUID NOT NULL,
    readiness_audit_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE,
    snapshot JSONB NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (round_id, readiness_audit_id),
    UNIQUE (round_id, readiness_audit_id, snapshot_hash),
    CONSTRAINT formal_acceptance_snapshot_hash_valid CHECK (
        snapshot_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT formal_acceptance_snapshot_object CHECK (
        jsonb_typeof(snapshot) = 'object'
    ),
    CONSTRAINT formal_acceptance_snapshot_identity_matches CHECK (
        (snapshot->>'schema_version' = 'm2a-formal-acceptance-snapshot-v1'
        AND snapshot->>'round_id' = round_id::text
        AND snapshot->>'readiness_audit_id' = readiness_audit_id) IS TRUE
    ),
    CONSTRAINT formal_acceptance_snapshot_never_releases CHECK (
        (snapshot#>>'{evidence_root,synthetic}' = 'false'
        AND snapshot#>>'{evidence_root,automatic_release_allowed}' = 'false'
        AND snapshot#>>'{verifier,automatic_release_allowed}' = 'false') IS TRUE
    )
);

CREATE TABLE formal_evidence_acceptance_reviews (
    review_id TEXT PRIMARY KEY,
    review_hash TEXT NOT NULL UNIQUE,
    round_id UUID NOT NULL,
    readiness_audit_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    review JSONB NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (round_id, readiness_audit_id),
    FOREIGN KEY (round_id, readiness_audit_id, snapshot_hash)
        REFERENCES formal_evidence_acceptance_snapshots (
            round_id, readiness_audit_id, snapshot_hash
        ),
    CONSTRAINT formal_acceptance_review_hash_valid CHECK (
        review_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT formal_acceptance_review_object CHECK (
        (jsonb_typeof(review) = 'object'
        AND jsonb_typeof(review->'signature') = 'object') IS TRUE
    ),
    CONSTRAINT formal_acceptance_review_identity_matches CHECK (
        (review->>'schema_version' = 'm2a-formal-evidence-acceptance-review-v2'
        AND review->>'review_id' = review_id
        AND review->>'review_hash' = review_hash
        AND review->>'round_id' = round_id::text
        AND review->>'readiness_audit_id' = readiness_audit_id
        AND review->>'verification_input_digest' = snapshot_hash
        AND review->>'decision' = decision
        AND (review->>'reviewed_at')::timestamptz = reviewed_at) IS TRUE
    ),
    CONSTRAINT formal_acceptance_review_decision_valid CHECK (
        decision IN ('accepted_for_formal_window', 'blocked')
    ),
    CONSTRAINT formal_acceptance_review_never_authorizes CHECK (
        (review->>'owner_window_authorization' = 'not_granted'
        AND review->>'hcu_accessed' = 'false'
        AND review->>'automatic_release_allowed' = 'false') IS TRUE
    )
);

CREATE OR REPLACE FUNCTION reject_formal_evidence_acceptance_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Formal Evidence Acceptance records are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER formal_evidence_acceptance_snapshots_append_only
BEFORE UPDATE OR DELETE ON formal_evidence_acceptance_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_formal_evidence_acceptance_mutation();

CREATE TRIGGER formal_evidence_acceptance_reviews_append_only
BEFORE UPDATE OR DELETE ON formal_evidence_acceptance_reviews
FOR EACH ROW EXECUTE FUNCTION reject_formal_evidence_acceptance_mutation();

INSERT INTO schema_migrations (version, name)
VALUES (20, 'm2a_formal_evidence_acceptance')
ON CONFLICT (version) DO NOTHING;

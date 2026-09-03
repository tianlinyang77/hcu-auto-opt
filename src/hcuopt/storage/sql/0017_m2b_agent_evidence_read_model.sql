CREATE TABLE agent_generation_evidence_read_models (
    generation_run_id UUID PRIMARY KEY REFERENCES agent_generation_runs(generation_run_id)
        ON DELETE RESTRICT,
    verifier_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    publication JSONB NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    dev_only BOOLEAN NOT NULL DEFAULT TRUE,
    formal_readiness TEXT NOT NULL DEFAULT 'hold',
    performance_conclusion TEXT NOT NULL DEFAULT 'not_measured',
    formal_intake_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    hcu_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    measurement_access_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    automatic_release_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT agent_generation_evidence_verifier_version CHECK (
        verifier_version = 'm2b-proposal-verifier-v1'
    ),
    CONSTRAINT agent_generation_evidence_digest_valid CHECK (
        input_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT agent_generation_evidence_publication_object CHECK (
        jsonb_typeof(publication) = 'object'
        AND publication->>'schema_version' = 'm2b-agent-evidence-publication-v1'
        AND publication->>'generation_run_id' = generation_run_id::TEXT
        AND publication->>'verifier_version' = verifier_version
        AND publication->>'input_digest' = input_digest
    ),
    CONSTRAINT agent_generation_evidence_dev_only CHECK (dev_only = TRUE),
    CONSTRAINT agent_generation_evidence_hold CHECK (formal_readiness = 'hold'),
    CONSTRAINT agent_generation_evidence_not_measured CHECK (
        performance_conclusion = 'not_measured'
    ),
    CONSTRAINT agent_generation_evidence_never_formal CHECK (
        formal_intake_allowed = FALSE
        AND hcu_access_allowed = FALSE
        AND measurement_access_allowed = FALSE
        AND automatic_release_allowed = FALSE
    )
);

CREATE OR REPLACE FUNCTION protect_agent_generation_evidence_read_model()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Agent Generation Evidence Read Model is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_generation_evidence_read_model_immutable
BEFORE UPDATE OR DELETE ON agent_generation_evidence_read_models
FOR EACH ROW EXECUTE FUNCTION protect_agent_generation_evidence_read_model();

INSERT INTO schema_migrations (version, name)
VALUES (17, 'm2b_agent_evidence_read_model')
ON CONFLICT (version) DO NOTHING;

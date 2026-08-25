ALTER TABLE hotspots
    ADD COLUMN candidate_kind TEXT,
    ADD COLUMN actor TEXT,
    ADD COLUMN intake_hash TEXT,
    ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX hotspots_idempotency_idx
ON hotspots (idempotency_key)
WHERE idempotency_key IS NOT NULL;

ALTER TABLE candidates
    ADD COLUMN hotspot_id UUID
        REFERENCES hotspots(hotspot_id) ON DELETE RESTRICT;

INSERT INTO schema_migrations (version, name)
VALUES (7, 'm1_hotspot_overlay_pipeline')
ON CONFLICT (version) DO NOTHING;

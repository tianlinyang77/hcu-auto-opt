ALTER TABLE formal_round_barriers
    DROP CONSTRAINT formal_barrier_family_shape;

ALTER TABLE formal_round_barriers
    ADD CONSTRAINT formal_barrier_family_shape CHECK (
        (
            phase = 'search'
            AND parent_search_barrier_id IS NULL
            AND (
                (
                    outcome = 'members_promoted'
                    AND holdout_family_hash IS NOT NULL
                ) OR (
                    outcome = 'no_promotable_candidate'
                    AND holdout_family_hash IS NULL
                )
            )
        ) OR (
            phase = 'holdout'
            AND parent_search_barrier_id IS NOT NULL
            AND holdout_family_hash IS NOT NULL
            AND input_family_hash = holdout_family_hash
            AND outcome = 'completed'
            AND expected_member_count <= 2
        )
    );

CREATE OR REPLACE FUNCTION enforce_formal_barrier_context()
RETURNS TRIGGER AS $$
DECLARE
    authority formal_round_authority_contexts%ROWTYPE;
    parent formal_round_barriers%ROWTYPE;
    reveal RECORD;
    round_parent search_rounds%ROWTYPE;
BEGIN
    SELECT * INTO authority
    FROM formal_round_authority_contexts
    WHERE round_id = NEW.round_id
      AND authority_context_id = NEW.authority_context_id
      AND context_hash = NEW.authority_context_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Formal Barrier Authority Context does not exist';
    END IF;
    SELECT * INTO round_parent FROM search_rounds
    WHERE round_id = NEW.round_id FOR SHARE;
    IF NEW.phase = 'search' AND (
        NEW.input_family_hash <> authority.artifact_family_hash
        OR NEW.expected_member_count <> round_parent.declared_candidate_count
        OR round_parent.state <> 'search_barrier'
        OR round_parent.holdout_family_hash IS DISTINCT FROM NEW.holdout_family_hash
    ) THEN
        RAISE EXCEPTION 'Formal Search Barrier must atomically freeze its input and output Families';
    END IF;
    IF NEW.phase = 'holdout' THEN
        SELECT * INTO parent
        FROM formal_round_barriers
        WHERE round_id = NEW.round_id
          AND barrier_id = NEW.parent_search_barrier_id
          AND phase = 'search';
        IF NOT FOUND
            OR parent.authority_context_id <> NEW.authority_context_id
            OR parent.authority_context_hash <> NEW.authority_context_hash
            OR parent.outcome <> 'members_promoted'
            OR parent.holdout_family_hash <> NEW.holdout_family_hash
        THEN
            RAISE EXCEPTION 'Formal Holdout Barrier requires its frozen Search parent';
        END IF;
        SELECT * INTO reveal FROM formal_round_holdout_reveals
        WHERE round_id = NEW.round_id;
        IF NOT FOUND
            OR reveal.authority_context_id <> NEW.authority_context_id
            OR reveal.authority_context_hash <> NEW.authority_context_hash
            OR reveal.search_barrier_id <> NEW.parent_search_barrier_id
            OR reveal.holdout_family_hash <> NEW.holdout_family_hash
            OR round_parent.holdout_family_hash <> NEW.holdout_family_hash
        THEN
            RAISE EXCEPTION 'Formal Holdout Barrier requires its frozen Reveal Family';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

INSERT INTO schema_migrations (version, name)
VALUES (13, 'm2_formal_finalizer')
ON CONFLICT (version) DO NOTHING;

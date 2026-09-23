# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Agent-to-M1 provenance persistence and signoff checks in an isolated schema."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.contracts.platform_v1 import EvaluationRun, EvidenceBundle, MeasurementSeries
from hcuopt.contracts.v1 import ManualCandidateSignoffRequest
from hcuopt.domain.enums import (
    CandidateState,
    ManualCandidateDecision,
    ManualCandidateVerdict,
    TaskState,
)
from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import PostgresRepository
from tests.integration import test_m1_control_plane_postgres as m1_fixtures

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def isolated_dsn(request: pytest.FixtureRequest) -> Iterator[str]:
    base = os.environ["HCUOPT_DATABASE_URL"]
    schema = "m1_agent_origin_" + uuid4().hex
    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        dsn = make_conninfo(base, options=f"-c search_path={schema}")
        with psycopg.connect(dsn) as connection:
            assert connection.execute("SELECT current_schema()").fetchone()[0] == schema
        PostgresRepository(dsn).migrate()
        yield dsn
    finally:
        assert schema.startswith("m1_agent_origin_") and len(schema) == 48
        with psycopg.connect(base) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _origin(candidate_id, source_hash: str) -> dict[str, object]:
    return {
        "schema_version": "bw20-agent-m1-origin-v1",
        "generation_run_id": str(uuid4()),
        "proposal_id": str(uuid4()),
        "review_id": str(uuid4()),
        "review_record_hash": "sha256:" + "1" * 64,
        "proposal_review_evidence_uri": "file:///m1/proposal-review.json",
        "proposal_review_evidence_hash": "sha256:" + "2" * 64,
        "generation_review_evidence_uri": "file:///m1/generation-review.json",
        "generation_review_evidence_hash": "sha256:" + "3" * 64,
        "patch_hash": "sha256:" + "4" * 64,
        "candidate_id": str(candidate_id),
        "candidate_source_hash": source_hash,
        "source_package_hash": "sha256:" + "5" * 64,
        "manifest_hash": "sha256:" + "6" * 64,
        "decision": "approved",
        "synthetic": False,
        "automatic_release_allowed": False,
    }


def test_agent_origin_is_idempotent_hash_bound_and_required_for_bw20_approval(
    isolated_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The legacy fixture's TRUNCATE is safe here because isolated_dsn creates a
    # fresh random schema and drops it after the test. Never point this at a shared schema.
    monkeypatch.setattr(m1_fixtures, "DATABASE_URL", isolated_dsn)
    monkeypatch.setattr(m1_fixtures, "PROFILE", "bw20-m1-manual-v1")
    fixture = m1_fixtures.M1ControlPlanePostgresTests()
    fixture.setUp()
    try:
        repository = fixture.repository
        task = repository.create_manual_candidate_task(fixture._task_request())
        candidate = repository.create_manual_candidate(
            task["task_id"], fixture._candidate_request(task["task_id"])
        )
        for state in (
            CandidateState.BUILT,
            CandidateState.CORRECTNESS_RUNNING,
            CandidateState.PERFORMANCE_RUNNING,
            CandidateState.ADJUDICATING,
            CandidateState.AWAITING_SIGNOFF,
        ):
            repository.transition_candidate(candidate["candidate_id"], state)
        for state in (
            TaskState.MANUAL_CORRECTNESS,
            TaskState.MANUAL_PERFORMANCE,
            TaskState.MANUAL_ADJUDICATING,
            TaskState.AWAITING_SIGNOFF,
        ):
            repository.transition_task(task["task_id"], state)

        candidate = repository.get_candidate(candidate["candidate_id"])
        target = repository.get_target_snapshot(task["task_id"])
        measurement = MeasurementSeries(
            status="measured",
            metric_name="latency",
            unit="us",
            protocol_version="m1-single-candidate-v1",
            sample_count=20,
            raw_samples_uri="file:///m1/origin-signoff-samples.json",
            raw_samples_hash="sha256:" + "7" * 64,
            environment_fingerprint=target["target_fingerprint"],
            adapter_provenance=fixture.provenance,
        )
        evaluation = EvaluationRun(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            round_id=candidate["round_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            phase="performance",
            protocol_version="m1-single-candidate-v1",
            target_fingerprint=target["target_fingerprint"],
            idempotency_key="m1-agent-origin-evaluation",
            passed=None,
            metrics={"verdict": "inconclusive"},
            measurement=measurement,
            adapter_provenance=[fixture.provenance],
            synthetic=False,
        )
        repository.record_evaluation(evaluation)
        evidence = EvidenceBundle(
            task_id=task["task_id"],
            candidate_id=candidate["candidate_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            target_id=task["target_id"],
            evidence_type="m1_manual_candidate",
            protocol_version="m1-single-candidate-v1",
            measurement_ids=[measurement.measurement_id],
            summary={"verdict": "inconclusive", "automatic_release_allowed": False},
            raw_uris=[measurement.raw_samples_uri],
            adapter_provenance=[fixture.provenance],
            synthetic=False,
        )
        repository.record_evidence_bundle(
            evidence, evaluation.evaluation_run_id, "m1-agent-origin-evidence"
        )
        repository.set_manual_candidate_verdict(
            candidate["candidate_id"],
            ManualCandidateVerdict.INCONCLUSIVE,
            evidence.evidence_id,
        )
        signoff = ManualCandidateSignoffRequest(
            decision=ManualCandidateDecision.APPROVED,
            actor="m1-agent-origin-reviewer",
            reason="accept the frozen evidence only",
            evidence_bundle_id=evidence.evidence_id,
            idempotency_key="m1-agent-origin-signoff",
        )

        with pytest.raises(Conflict, match="Agent origin"):
            repository.signoff_manual_candidate_task(task["task_id"], signoff)

        details = _origin(candidate["candidate_id"], candidate["source_hash"])
        event = repository.record_manual_candidate_agent_origin(task["task_id"], details)
        replay = repository.record_manual_candidate_agent_origin(task["task_id"], details)
        assert event["event_id"] == replay["event_id"]
        summary = repository.manual_candidate_summary(task["task_id"])
        origins = [
            item for item in summary["events"]
            if item["event_type"] == "manual_candidate_agent_origin_recorded"
        ]
        assert origins == [event]

        changed = {**details, "candidate_source_hash": "sha256:" + "8" * 64}
        with pytest.raises(Conflict, match="does not bind"):
            repository.record_manual_candidate_agent_origin(task["task_id"], changed)

        approved = repository.signoff_manual_candidate_task(task["task_id"], signoff)
        assert approved["decision"] == ManualCandidateDecision.APPROVED.value
        assert repository.signoff_manual_candidate_task(task["task_id"], signoff) == approved
        assert repository.get_task(task["task_id"])["state"] == TaskState.COMPLETED.value
    finally:
        fixture.tearDown()

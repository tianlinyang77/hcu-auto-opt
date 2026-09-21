# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real Git/Overlay/DB composition with explicitly synthetic test source and authority.

No HCU, model, real SGLang workload, or performance conclusion is involved.
"""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.formal_candidate_builder import FormalRoundCandidateBuilder
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.contracts.m2 import (
    ArtifactFamilyFreezeRequest,
    BudgetUsage,
    RoundBudgetLedgerEntry,
    RoundBudgetReservation,
    RoundCandidate,
)
from hcuopt.contracts.v1 import WorkerRegister
from hcuopt.domain.errors import Conflict
from hcuopt.orchestrator.search_round import artifact_family_hash
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.storage.formal_build import PostgresFormalBuildStore
from hcuopt.storage.formal_build_jobs import PostgresFormalBuildJobs
from hcuopt.storage.formal_build_journal import PostgresFormalBuildJournal
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_correctness_jobs import PostgresFormalCorrectnessJobs
from hcuopt.workers.formal_build_consumer import FormalBuildConsumer
from tests.integration import test_formal_dispatch_postgres as dispatch_tests
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401
from tests.unit import f1c_helpers as source_helpers
from tests.unit import test_formal_operator_plans as plans

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
    pytest.mark.skipif(os.name == "nt", reason="real readonly publication requires Linux"),
]


@pytest.fixture
def real_sources(tmp_path, monkeypatch):
    profile = "nmz36-m2a-formal-v1"
    origin = tmp_path / "origin"
    origin.mkdir()
    git = source_helpers.git
    git(origin, "init")
    git(origin, "config", "user.name", "Test Builder")
    git(origin, "config", "user.email", "builder@example.invalid")
    target = origin / plans.OVERLAY_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(b"def free(value):\n    return value\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test source")
    output = tmp_path / "build-output"
    manager = GitSourceManager(profile)
    baseline = manager.prepare_baseline(source_helpers.target_for(
        origin, tmp_path / "baseline", git(origin, "rev-parse", "HEAD"),
    ), output)
    digests = {"baseline-source": baseline.source_hash}
    contents = [b"def free_v1(value):\n    return value\n",
                b"def free_v2(value):\n    return value + 0\n"]
    for label, candidate_id, content in zip(
        ["candidate-one", "candidate-two"],
        [plans.FIRST_CANDIDATE_ID, plans.SECOND_CANDIDATE_ID], contents, strict=True,
    ):
        snapshot = manager.create_candidate(baseline, candidate_id, output)
        try:
            worktree = file_uri_to_path(snapshot.worktree_uri)
            (worktree / plans.OVERLAY_PATH).write_bytes(content)
            digests[label] = canonical_source_hash(worktree)
        finally:
            manager.remove_candidate(baseline, snapshot, output)
    old_hash, old_seed = plans._hash, dispatch_tests.seed_authority
    monkeypatch.setattr(plans, "_hash", lambda value: digests.get(value) or old_hash(value))
    state = {"manager": manager, "output": output, "profile": profile, "contents": contents}

    def seed(repo, coordinator, memory):
        # Set up test authority before Intent signing/dispatch; never rewrite frozen intake.
        old_seed(repo, coordinator, memory)
        with repo.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_id FROM source_snapshots WHERE kind = 'baseline'"
            ).fetchone()
            state["baseline"] = baseline.model_copy(update={"snapshot_id": row["snapshot_id"]})
            conn.execute(
                "UPDATE source_snapshots SET repository = %s, commit = %s, tree_hash = %s, "
                "worktree_uri = %s, created_at = %s WHERE snapshot_id = %s",
                (baseline.repository, baseline.commit, baseline.tree_hash, baseline.worktree_uri,
                 baseline.created_at, row["snapshot_id"]),
            )
    monkeypatch.setattr(dispatch_tests, "seed_authority", seed)
    return state


def test_real_builder_to_budget_publication_and_job_completion(real_sources, request):
    # Resolve after real_sources has replaced only the test source/seed factories.
    dispatcher, intent_id = request.getfixturevalue("dispatch_case")
    dispatcher.create(intent_id)
    repo = dispatcher.repository
    claims = PostgresFormalClaimStore(dispatcher, enabled=True)
    claim = claims.claim(intent_id, "real-test-builder", ttl_seconds=300)
    token = claim["claim_token"]
    correctness_jobs = PostgresFormalCorrectnessJobs(
        claims, intent_id, "real-test-builder", token,
    )
    with pytest.raises(Conflict, match="frozen correctness"):
        correctness_jobs.enqueue(plans.FIRST_CANDIDATE_ID)
    for index, candidate_id in enumerate([plans.FIRST_CANDIDATE_ID, plans.SECOND_CANDIDATE_ID]):
        build_member(repo, claims, intent_id, token, real_sources, request, candidate_id, index)
        if index == 0:
            with repo.connection() as conn:
                partial = conn.execute("SELECT * FROM search_rounds").fetchone()
            with pytest.raises(Conflict, match="not ready"):
                repo.freeze_search_round_artifact_family(ArtifactFamilyFreezeRequest(
                    round_id=partial["round_id"],
                    candidate_family_hash=partial["candidate_family_hash"],
                    expected_artifact_family_hash="sha256:" + "0" * 64,
                ))
    with repo.connection() as conn:
        round_row = conn.execute("SELECT * FROM search_rounds").fetchone()
        members = conn.execute("SELECT * FROM round_candidates ORDER BY ordinal").fetchall()
    freeze = ArtifactFamilyFreezeRequest(
        round_id=round_row["round_id"], candidate_family_hash=round_row["candidate_family_hash"],
        expected_artifact_family_hash=artifact_family_hash(round_row, members),
    )
    frozen = repo.freeze_search_round_artifact_family(freeze)
    assert frozen["state"] == "correctness"
    assert frozen["automatic_release_allowed"] is False
    assert repo.freeze_search_round_artifact_family(freeze) == frozen
    with pytest.raises(Conflict, match="Hash"):
        repo.freeze_search_round_artifact_family(freeze.model_copy(update={
            "expected_artifact_family_hash": "sha256:" + "0" * 64,
        }))
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM task_events WHERE event_type = "
                            "'m2_round_artifact_family_frozen'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE state = 'succeeded'").fetchone()[
            "n"
        ] == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        simultaneous = list(pool.map(
            correctness_jobs.enqueue, [plans.FIRST_CANDIDATE_ID] * 2,
        ))
    job = simultaneous[0]
    assert simultaneous[1] == job
    assert correctness_jobs.enqueue(plans.FIRST_CANDIDATE_ID) == job
    assert job["execution_lane"] == "formal"
    assert job["job_type"] == "manual_correctness"
    assert job["state"] == "queued" and job["attempts"] == 0
    assert job["lease_scope"] == "shared" and job["max_attempts"] == 1
    assert job["payload"]["artifact_family_hash"] == frozen["artifact_family_hash"]
    repo.register_worker(WorkerRegister(
        worker_id="ordinary-correctness", worker_type="gpu",
        adapter_profile=frozen["adapter_profile"],
    ))
    assert repo.claim_job("ordinary-correctness") is None
    with pytest.raises(Conflict):
        PostgresFormalCorrectnessJobs(
            claims, intent_id, "real-test-builder", uuid4(),
        ).enqueue(plans.FIRST_CANDIDATE_ID)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(
            lambda _: correctness_jobs.reserve(plans.FIRST_CANDIDATE_ID, wall_seconds=30),
            range(2),
        ))
    assert reservations[0] == reservations[1]
    assert reservations[0]["reservation"]["planned"]["correctness_attempts"] == 1
    with pytest.raises(Conflict, match="other inputs"):
        correctness_jobs.reserve(plans.FIRST_CANDIDATE_ID, wall_seconds=31)
    with pytest.raises(Conflict, match="exhausted"):
        correctness_jobs.reserve(plans.SECOND_CANDIDATE_ID, wall_seconds=604801)
    with pytest.raises(Conflict):
        correctness_jobs.enqueue(uuid4())
    claims.request_stop(intent_id, requested_by="integration-test")
    with pytest.raises(Conflict, match="stop"):
        correctness_jobs.enqueue(plans.SECOND_CANDIDATE_ID)
    with repo.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE job_type = "
                            "'manual_correctness'").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM job_events WHERE event_type = "
                            "'formal_correctness_queued'").fetchone()["n"] == 2
        assert conn.execute(
            "SELECT count(*) AS n FROM round_budget_reservations "
            "WHERE planned->>'correctness_attempts' = '1'",
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM round_budget_ledger "
            "WHERE entry_type = 'reserve' AND reserved->>'correctness_attempts' = '1'",
        ).fetchone()["n"] == 1


def build_member(repo, claims, intent_id, token, real_sources, request, candidate_id, index):
    with repo.connection() as conn:
        round_ = repo._search_round_authority(
            conn.execute("SELECT * FROM search_rounds").fetchone()
        )
        row = conn.execute("SELECT * FROM round_candidates WHERE candidate_id = %s",
                           (candidate_id,)).fetchone()
        member = RoundCandidate.model_validate(
            {key: row[key] for key in RoundCandidate.model_fields}
        )
        hotspot = conn.execute("SELECT * FROM hotspots").fetchone()
    repo.register_worker(WorkerRegister(
        worker_id="real-test-builder", worker_type="build", adapter_profile=round_.adapter_profile,
    ))
    job = PostgresFormalBuildJobs(claims, intent_id, "real-test-builder", token).enqueue(
        member.candidate_id,
    )
    reservation = RoundBudgetReservation(
        reservation_id=uuid4(), round_id=round_.round_id, job_id=job["job_id"], attempt=1,
        candidate_id=member.candidate_id, planned=BudgetUsage(build_attempts=1, wall_seconds=60),
        state="reserved", idempotency_key=f"real-build-reserve:{job['job_id']}",
    )
    repo.reserve_round_budget(reservation, RoundBudgetLedgerEntry(
        ledger_entry_id=uuid4(), reservation_id=reservation.reservation_id,
        round_id=round_.round_id, entry_type="reserve", reserved=reservation.planned,
        actual=BudgetUsage(), lease_held_seconds=0, harness_active_seconds=0,
        raw_usage_evidence_hash=member.source_package_hash,
        idempotency_key=f"real-build-ledger:{job['job_id']}", created_at=datetime.now(timezone.utc),
    ))
    # The real source package root used by the compiler/verifier is the fixture's packages folder.
    package_root = request.getfixturevalue("tmp_path") / "packages"
    output, profile = real_sources["output"], real_sources["profile"]
    builder = FormalRoundCandidateBuilder(ManualOverlayCandidateBuilder(
        real_sources["manager"], CandidateSourcePackageStore(
            package_root, profile=profile, allowed_overlay_roots=("sglang",),
            approved_mount_targets={plans.REPLACEMENT_POINT: plans.MOUNT_TARGET},
        ), LocalArtifactStore(output / "artifacts", profile), LocalBuildCache(output / "cache"),
        profile=profile,
    ), store_id=member.source_package_store_id, store_hash=member.source_package_store_hash,
        enabled=True)
    consumer = FormalBuildConsumer(
        PostgresFormalBuildJournal(claims, intent_id, "real-test-builder", token), builder,
        PostgresFormalBuildStore(claims, enabled=True), enabled=True,
    )
    kwargs = dict(
        reservation_id=reservation.reservation_id, round_authority=round_, member=member,
        baseline=real_sources["baseline"], hotspot=hotspot["evidence"],
        hotspot_intake_hash=hotspot["intake_hash"], output_dir=output,
    )
    result = consumer.execute_once(**kwargs)
    assert consumer.execute_once(**kwargs) == result
    artifact = file_uri_to_path(result.build.artifact.uri)
    assert artifact.read_bytes() == real_sources["contents"][index]
    assert result.build.artifact.content_hash == "sha256:" + hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    assert artifact.stat().st_mode & 0o222 == 0
    assert not any((output / "worktrees").iterdir())
    assert canonical_source_hash(file_uri_to_path(real_sources["baseline"].worktree_uri)) == (
        real_sources["baseline"].source_hash
    )
    assert source_helpers.git(
        file_uri_to_path(real_sources["baseline"].worktree_uri), "status", "--porcelain=v1",
    ) == ""
    with repo.connection() as conn:
        assert conn.execute(
            "SELECT state FROM jobs WHERE job_id = %s", (job["job_id"],),
        ).fetchone()["state"] == "succeeded"
        assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == index + 1
        assert conn.execute("SELECT state FROM round_budget_reservations WHERE reservation_id = %s",
                            (reservation.reservation_id,)).fetchone()[
            "state"
        ] == "settled"
        assert conn.execute("SELECT count(*) AS n FROM round_budget_ledger "
                            "WHERE entry_type = 'settle'").fetchone()["n"] == index + 1

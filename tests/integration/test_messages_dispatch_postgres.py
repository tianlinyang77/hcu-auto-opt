# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Actual PostgreSQL + subprocess + local HTTP; no model or HCU claims."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.adapters.agent_promotion import (
    BaselineOverlaySource,
    CandidateSourcePackagePublisher,
    ProposalDecisionStore,
    ProposalPromotionService,
    ProposalReviewAuthority,
)
from hcuopt.adapters.agent_runner import AgentInputFile
from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.messages_generator import messages_profile_for_input, prepare_messages_input
from hcuopt.agent.authority import ApexGenerationCoordinator
from hcuopt.agent.identity import (
    candidate_generation_request_hash,
    candidate_proposal_promotion_receipt_hash,
    candidate_proposal_review_record_hash,
)
from hcuopt.agent.messages_dispatch import MessagesDispatchService
from hcuopt.agent.messages_worker import MessagesGenerationWorker
from hcuopt.contracts.agent_v1 import GenerationRunStartRequest
from hcuopt.contracts.agent_verification_v1 import (
    AgentEvidenceRef,
    AgentProposalVerificationContext,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.domain.errors import SourceArtifactError, StaleClaimToken
from hcuopt.evaluation.agent_generation_inspection import AgentGenerationInspectionService
from hcuopt.evaluation.agent_generation_read_model import (
    AgentGenerationEvidenceReadService,
    AgentGenerationReadModelError,
    build_agent_generation_evidence_publication,
)
from hcuopt.evaluation.agent_proposal_reporting import write_agent_generation_report
from hcuopt.evaluation.agent_proposal_verifier import (
    AgentProposalVerifier,
    build_agent_generation_read_model,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.generators import anthropic_messages
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_messages_generator import SOURCE_PATH, _hash, _response
from tests.unit.test_messages_generator import setup as messages_setup  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def repository():
    """Never truncate shared tables; migrate and drop only our generated schema."""
    url = os.environ["HCUOPT_DATABASE_URL"]
    schema = "messages_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(url, options=f"-csearch_path={schema}")
    try:
        repo = PostgresRepository(scoped_url)
        repo.migrate()
        yield repo
    finally:
        # Exact, locally generated name; no public/default schema is touched.
        assert schema.startswith("messages_test_") and len(schema) == 46
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _promotion_input(fixture):
    """A real temporary Git baseline; never a user checkout or real model output."""
    root = fixture["root"]
    fixture["source"].write_bytes(b"def forward(value):\n    return value\n")
    (root / "unchanged.txt").write_bytes(b"Included in the complete worktree Hash.\n")
    _git(root, "init")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "--", SOURCE_PATH, "unchanged.txt")
    _git(
        root,
        "-c",
        "user.name=HCU Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test(source): add isolated baseline",
    )
    baseline = BaselineOverlaySource(
        snapshot=SourceSnapshot(
            kind="baseline",
            repository=root.as_uri(),
            commit=_git(root, "rev-parse", "HEAD"),
            tree_hash=_git(root, "rev-parse", "HEAD^{tree}"),
            source_hash=canonical_source_hash(root),
            worktree_uri=root.as_uri(),
            clean=True,
        ),
        path=SOURCE_PATH,
    )
    request = fixture["request"].model_copy(
        update={
            "baseline_source_hash": baseline.snapshot.source_hash,
            "max_proposals": 2,
        }
    )
    item = prepare_messages_input(
        request,
        baseline=baseline,
        knowledge_store=fixture["knowledge"],
        settings=fixture["settings"],
        hotspot_summary="CPU integration fixture, not a real hotspot",
    )
    generator = fixture["plan"].generators[0].model_copy(update={"max_proposals": 2})
    plan = fixture["plan"].model_copy(
        update={
            "request_hash": candidate_generation_request_hash(request),
            "generators": (generator,),
            "budget": fixture["plan"].budget.model_copy(update={"max_proposals": 2}),
        }
    )
    fixture.update(baseline=baseline, request=request, item=item, plan=plan)
    proposals = []
    for expression in ("value + 0", "value * 1"):
        proposals.append(
            {
                "optimization_intent": "Exercise source handoff: " + expression,
                "rationale": "Local stub fixture only; no performance claim.",
                "risk_summary": "Test review is not approval for a real workload.",
                "patch": (
                    f"--- a/{SOURCE_PATH}\n+++ b/{SOURCE_PATH}\n@@ -1,2 +1,2 @@\n"
                    f" def forward(value):\n-    return value\n+    return {expression}\n"
                ),
            }
        )
    return _response(proposals)


@pytest.fixture
def dispatch_case(messages_setup, repository, tmp_path, request):  # noqa: F811
    fixture = messages_setup
    calls = []
    response = {"reply": _response(), "status": 200}
    if getattr(request, "param", None) == "promotion":
        response["reply"] = _promotion_input(fixture)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            raw = canonical_json_bytes(response["reply"])
            self.send_response(response["status"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    envelope = anthropic_messages.strict_json(fixture["item"].content)
    envelope["provider"].update(base_url=f"http://127.0.0.1:{server.server_port}", allow_http=True)
    item = AgentInputFile(
        path=anthropic_messages.INPUT_NAME, content=canonical_json_bytes(envelope)
    )
    generator = (
        fixture["plan"]
        .generators[0]
        .model_copy(update={"adapter_profile": messages_profile_for_input(item)})
    )
    plan = fixture["plan"].model_copy(update={"generators": (generator,)})
    start = GenerationRunStartRequest(
        request=fixture["request"], plan=plan, actor="postgres-test", idempotency_key=fixture["key"]
    )
    ApexGenerationCoordinator().start(start, repository)
    root = tmp_path / "dispatch-store"
    worker = MessagesGenerationWorker(root)
    service = MessagesDispatchService(repository, worker)
    try:
        yield locals()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _execute(case):
    return case["service"].run_once(
        case["start"].request.generation_run_id,
        case["item"],
        worker_id="messages-postgres-test",
        api_key="test-only-not-a-secret",
        lease_seconds=15,
    )


@pytest.mark.parametrize("mode", ["valid", "bad_patch", "provider_failure"])
def test_dispatch_settles_actual_receipt_and_replays_without_network(dispatch_case, mode):
    case = dispatch_case
    if mode == "bad_patch":
        proposals = json.loads(case["response"]["reply"]["content"][0]["text"])["proposals"]
        proposals[0]["patch"] = proposals[0]["patch"].replace("-return value", "-return other")
        case["response"]["reply"] = _response(proposals)
    elif mode == "provider_failure":
        case["response"]["status"] = 503
    status = _execute(case)
    attempt = status.attempts[0]
    assert attempt.runner_receipt_hash is not None
    assert attempt.state == ("succeeded" if mode == "valid" else "failed")
    assert len(status.proposals) == (1 if mode == "valid" else 0)
    assert attempt.actual.tokens == (1000 if mode == "provider_failure" else 100)
    assert status.automatic_release_allowed is False
    recovered = MessagesDispatchService(
        PostgresRepository(case["repository"].database_url), MessagesGenerationWorker(case["root"])
    ).recover(status.run.generation_run_id, attempt.attempt_id)
    assert recovered == status
    assert len(case["calls"]) == 1
    assert _execute(case) == status  # terminal Run does not claim again
    assert len(case["calls"]) == 1


@pytest.mark.skipif(os.name != "posix", reason="D requires POSIX openat/O_NOFOLLOW")
@pytest.mark.parametrize("mode", ["valid", "bad_patch", "provider_failure"])
@pytest.mark.parametrize("api_kind", ["control_plane", "readonly"])
def test_postgres_to_native_d_inspection(dispatch_case, mode, api_kind):
    case = dispatch_case
    if mode == "bad_patch":
        proposals = json.loads(case["response"]["reply"]["content"][0]["text"])["proposals"]
        proposals[0]["patch"] = proposals[0]["patch"].replace("-return value", "-return other")
        case["response"]["reply"] = _response(proposals)
    elif mode == "provider_failure":
        case["response"]["status"] = 503
    status = _execute(case)
    attempt = status.attempts[0]
    inspector = AgentGenerationInspectionService(case["repository"], case["root"])
    inspection = inspector.prepare(
        status.run.generation_run_id,
        knowledge_store=case["fixture"]["knowledge"],
        task_id=uuid4(),
        target_id="postgres-http-stub-not-hcu",
    )
    assert inspection.read_model.attempts[0].status == (
        "succeeded" if mode == "valid" else "failed"
    )
    assert inspection.read_model.attempts[0].output_tokens == (
        1000 if mode == "provider_failure" else 100
    )
    assert inspection.read_model.formal_intake_allowed is False
    assert inspection.read_model.human_review_status == "pending"
    assert case["repository"].generation_run_status(status.run.generation_run_id) == status
    assert (
        AgentGenerationInspectionService(
            PostgresRepository(case["repository"].database_url), case["root"]
        ).get(status.run.generation_run_id)
        == inspection
    )

    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app

    auth = None
    if api_kind == "readonly":
        from hcuopt.api.inspection_access import FileRunReadAccess, provision_run_read_access
        from hcuopt.api.inspection_server import create_inspection_app

        private = case["root"].parent / "private-access"
        private.mkdir(mode=0o700)
        access_file, credential_file = private / "access.json", private / "credential.txt"
        provision_run_read_access(
            access_file, credential_file, run_ids=(status.run.generation_run_id,)
        )
        password = credential_file.read_text().splitlines()[1].split(": ", 1)[1]
        auth = ("operator", password)
        app = create_inspection_app(
            repository=case["repository"],
            evidence_root=case["root"],
            access=FileRunReadAccess(access_file),
        )
    else:
        app = create_app(
            repository=case["repository"],
            agent_inspection_read_authorizer=(
                lambda _request, run_id: run_id == status.run.generation_run_id
            ),
        )
    with (
        patch.dict(
            os.environ, HCUOPT_AGENT_INSPECTION_ROOT=str(case["root"]), HCUOPT_AUTO_MIGRATE="false"
        ),
        TestClient(app, base_url="https://testserver") as client,
    ):
        url = f"/v1/operator/agent-generations/{status.run.generation_run_id}/inspection"
        if api_kind == "readonly":
            assert client.get(url).status_code == 401
            assert client.get(url, auth=("operator", "wrong")).status_code == 403
            assert (
                client.get(
                    f"/v1/operator/agent-generations/{uuid4()}/inspection", auth=auth
                ).status_code
                == 403
            )
            client.auth = auth
        response = client.get(
            f"/v1/operator/agent-generations/{status.run.generation_run_id}/inspection"
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == inspection.model_dump(mode="json")
        # Every read revalidates D evidence; a corrupted artifact is not displayed.
        path = case["root"] / "attempts" / str(attempt.attempt_id) / "receipt-ref.json"
        from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
        from hcuopt.source_hash import file_uri_to_path

        ref = RunnerExecutionReceiptRef.model_validate_json(path.read_bytes())
        file_uri_to_path(ref.uri).write_bytes(b"{}")
        assert (
            client.get(
                f"/v1/operator/agent-generations/{status.run.generation_run_id}/inspection"
            ).status_code
            != 200
        )


@pytest.mark.parametrize("expired", [False, True])
def test_crash_before_settlement_recovers_or_is_fenced(dispatch_case, monkeypatch, expired):
    case = dispatch_case
    repo = case["repository"]
    settle = repo.settle_generation_attempt

    def crash(*args, **kwargs):
        raise RuntimeError("simulated database disconnect after Receipt publication")

    monkeypatch.setattr(repo, "settle_generation_attempt", crash)
    with pytest.raises(RuntimeError, match="disconnect"):
        _execute(case)
    monkeypatch.setattr(repo, "settle_generation_attempt", settle)
    run_id = case["start"].request.generation_run_id
    status = repo.generation_run_status(run_id)
    attempt = status.attempts[0]
    assert attempt.state == "running"
    if expired:
        with repo.connection() as connection:
            connection.execute(
                "UPDATE agent_generator_attempts SET lease_expires_at = %s WHERE attempt_id = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), attempt.attempt_id),
            )
    service = MessagesDispatchService(
        PostgresRepository(repo.database_url), MessagesGenerationWorker(case["root"])
    )
    if expired:
        with pytest.raises(StaleClaimToken):
            service.recover(run_id, attempt.attempt_id)
        assert not repo.generation_run_status(run_id).proposals
    else:
        assert service.recover(run_id, attempt.attempt_id).attempts[0].state == "succeeded"
    assert len(case["calls"]) == 1


def test_incompatible_input_refused_before_database_claim(dispatch_case):
    case = dispatch_case
    run_id = case["start"].request.generation_run_id
    before = case["repository"].generation_run_status(run_id)
    with pytest.raises(SourceArtifactError, match="authority"):
        case["service"].run_once(
            run_id,
            AgentInputFile(path=anthropic_messages.INPUT_NAME, content=b"{}"),
            worker_id="test",
            api_key="test-only",
            lease_seconds=60,
        )
    assert case["repository"].generation_run_status(run_id) == before
    assert not case["calls"]


def test_recovery_refuses_cross_run_and_modified_input(dispatch_case):
    case = dispatch_case
    status = _execute(case)
    attempt_id = status.attempts[0].attempt_id
    with pytest.raises(SourceArtifactError, match="crosses"):
        case["service"].recover(uuid4(), attempt_id)
    (case["root"] / "attempts" / str(attempt_id) / "input.json").write_bytes(b"{}")
    with pytest.raises(SourceArtifactError, match="changed"):
        case["service"].recover(status.run.generation_run_id, attempt_id)
    assert len(case["calls"]) == 1


def test_started_without_receipt_cannot_be_reexecuted(dispatch_case, monkeypatch):
    from hcuopt.adapters.agent_runner import LocalCommandAgentRunner

    case = dispatch_case

    def crash(*args, **kwargs):
        raise RuntimeError("uncertain runner interruption")

    monkeypatch.setattr(LocalCommandAgentRunner, "run", crash)
    with pytest.raises(RuntimeError, match="interruption"):
        _execute(case)
    run_id = case["start"].request.generation_run_id
    state = case["repository"].generation_run_status(run_id)
    attempt = state.attempts[0]
    with pytest.raises(FileNotFoundError):
        case["service"].recover(run_id, attempt.attempt_id)
    assert _execute(case) == state  # no pending Attempt exists; no implicit retry
    assert not case["calls"]


def test_lease_larger_than_frozen_budget_is_rejected_before_claim(dispatch_case):
    case = dispatch_case
    run_id = case["start"].request.generation_run_id
    with pytest.raises(SourceArtifactError, match="lease"):
        case["service"].run_once(
            run_id,
            case["item"],
            worker_id="test",
            api_key="test-only",
            lease_seconds=60,
        )
    assert case["repository"].generation_run_status(run_id).attempts[0].state == "pending"
    assert not case["calls"]


def _promotion_services(case):
    # New instances read the dispatcher's actual Batch/Patch stores, not hand-built refs.
    worker = MessagesGenerationWorker(case["root"])
    decisions = ProposalDecisionStore(case["root"] / "test-decisions")
    authority = ProposalReviewAuthority(
        patch_store=worker.patches,
        batch_store=worker.batches,
        decision_store=decisions,
    )
    publisher = CandidateSourcePackagePublisher(
        case["root"] / "test-packages",
        profile="messages-promotion-integration-v1",
        source_manager=GitSourceManager(),
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={
            case["start"].request.replacement_point: "/opt/hcuopt/overlay/" + SOURCE_PATH,
        },
    )
    service = ProposalPromotionService(
        review_authority=authority,
        package_publisher=publisher,
        decision_store=decisions,
    )
    return decisions, authority, publisher, service


def _test_review(authority, status, proposal_id, decision="approved"):
    return authority.review(
        status,
        proposal_id,
        decision=decision,
        reviewer="test-fixture-reviewer",
        reason="Exercise the C interface only; not an actual human approval.",
        review_evidence=b'{"scope":"test-only-not-human-approval"}',
        idempotency_key=f"test-review-{proposal_id}",
        reviewed_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("dispatch_case", ["promotion"], indirect=True)
@pytest.mark.parametrize(
    "terminal_mode",
    ["pre_review"]
    + [
        pytest.param(
            mode,
            marks=pytest.mark.skipif(os.name != "posix", reason="D requires native POSIX reads"),
        )
        for mode in ("read", "promotion_receipt", "report", "status_snapshot")
    ],
)
def test_messages_to_git_packages_and_verified_family(dispatch_case, terminal_mode):
    case = dispatch_case
    status = _execute(case)
    assert status.run.state == "awaiting_review"
    assert len(status.proposals) == 2
    assert all(ref.disposition == "retained" for ref in status.proposals)
    assert len(case["calls"]) == 1
    # Simulate restart between generation and C intake, using PostgreSQL authority.
    status = PostgresRepository(case["repository"].database_url).generation_run_status(
        status.run.generation_run_id
    )
    decisions, authority, publisher, service = _promotion_services(case)
    baseline = case["fixture"]["baseline"]
    output = case["tmp_path"] / "promotion-work"
    with pytest.raises((SourceArtifactError, FileNotFoundError)):
        service.prepare(
            status,
            status.proposals[0].proposal_id,
            uuid4(),
            baseline=baseline,
            candidate_id=uuid4(),
            candidate_output_dir=output,
        )
    assert not publisher.root.exists()  # missing review cannot publish a Package
    prepared, reviews, promotions = [], [], []
    for ref in status.proposals:
        review = _test_review(authority, status, ref.proposal_id)
        reviews.append(review)
        prepared.append(
            service.prepare(
                status,
                ref.proposal_id,
                review.review_id,
                baseline=baseline,
                candidate_id=uuid4(),
                candidate_output_dir=output,
            )
        )
    assert len({item.source_package_ref.candidate_source_hash for item in prepared}) == 2
    assert not any((output / "worktrees").iterdir())
    baseline_root = file_uri_to_path(baseline.snapshot.worktree_uri)
    assert _git(baseline_root, "status", "--porcelain") == ""
    assert canonical_source_hash(baseline_root) == baseline.snapshot.source_hash
    # Reapply each Package with the real Source Manager, proving full source hashes.
    for item in prepared:
        manager = GitSourceManager()
        replay = manager.create_candidate(baseline.snapshot, uuid4(), output)
        try:
            package = publisher.source_packages.read(
                candidate_source_hash=item.source_package_ref.candidate_source_hash,
            )
            replay_root = file_uri_to_path(replay.worktree_uri)
            publisher.source_packages.apply(package, replay_root)
            assert (
                canonical_source_hash(replay_root) == item.source_package_ref.candidate_source_hash
            )
            assert (replay_root / "unchanged.txt").read_bytes() == (
                baseline_root / "unchanged.txt"
            ).read_bytes()
        finally:
            manager.remove_candidate(baseline.snapshot, replay, output)
    request = status.run.request
    store_id, store_hash = "messages-test-package-store", _hash(b"test-deployment-store")
    family = BusinessCandidateFamilyManifest(
        family_id="messages-test-family",
        source_package_store_id=store_id,
        source_package_store_hash=store_hash,
        target_snapshot_id=request.target_snapshot_id,
        stage0_run_id=request.stage0_run_id,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        profiler_evidence_uri=request.profiler_evidence_uri,
        profiler_evidence_hash=request.profiler_evidence_hash,
        overlay_mount_target="/opt/hcuopt/overlay/" + SOURCE_PATH,
        overlay_file_path=SOURCE_PATH,
        members=tuple(
            {
                "candidate_id": item.candidate_id,
                "source_package_ref": item.source_package_ref,
                "optimization_intent": item.resolved.proposal.optimization_intent,
            }
            for item in prepared
        ),
        reviewed_by="test-fixture-family-reviewer",
        reviewed_at=datetime.now(timezone.utc),
    )
    # Independent Store/Decision instances, no in-memory C publication shortcut.
    fresh_decisions, _, fresh_publisher, fresh_service = _promotion_services(case)
    verifier = BusinessCandidateFamilyVerifier(
        fresh_publisher.source_packages,
        store_id=store_id,
        store_hash=store_hash,
    )
    for item in prepared:
        receipt = fresh_service.finalize(
            item,
            family,
            verifier,
            promoted_by="test-fixture-promoter",
            promoted_at=datetime.now(timezone.utc),
            idempotency_key=f"test-promote-{item.candidate_id}",
        )
        promotions.append(receipt)
        assert ProposalDecisionStore(decisions.root).load_receipt(receipt.promotion_id) == receipt
        assert fresh_decisions.read_evidence(
            receipt.source_family_verification_evidence_uri,
            expected_hash=receipt.source_family_verification_evidence_hash,
        )
        assert receipt.formal_intake_allowed is False
        assert receipt.automatic_release_allowed is False
        assert receipt.performance_conclusion == "not_measured"
    assert not any((output / "worktrees").iterdir())
    assert _git(baseline_root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _git(baseline_root, "status", "--porcelain") == ""
    assert case["repository"].generation_run_status(request.generation_run_id) == status
    assert len(case["calls"]) == 1  # C never reexecutes the model
    if terminal_mode != "pre_review":
        _verify_terminal_handoff(case, status, reviews, promotions, decisions, terminal_mode)


def _verify_terminal_handoff(case, status, reviews, promotions, decisions, mode):
    """Test-only completion; never touch the deployed Run or simulate real signoff."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app

    run_id = status.run.generation_run_id
    repository = case["repository"]
    inspector = AgentGenerationInspectionService(repository, case["root"])
    before = inspector.prepare(
        run_id,
        knowledge_store=case["fixture"]["knowledge"],
        task_id=uuid4(),
        target_id="test-only-cpu-terminal-handoff",
    )
    assert before.read_model.human_review_status == "pending"
    context_path = case["root"] / "inspections" / str(run_id) / "context.json"
    old_context = AgentProposalVerificationContext.model_validate_json(context_path.read_bytes())
    overall_review = decisions.publish_evidence(
        "test-generation-review",
        canonical_json_bytes(
            {
                "scope": "test-only-not-human-approval",
                "generation_run_id": str(run_id),
                "review_ids": [str(item.review_id) for item in reviews],
                "promotion_ids": [str(item.promotion_id) for item in promotions],
            }
        ),
    )
    completed = repository.complete_generation_review(
        run_id,
        review_evidence_uri=overall_review.uri,
        review_evidence_hash=overall_review.content_hash,
        now=datetime.now(timezone.utc),
    )
    assert completed.state == "completed"
    # The old pre-review descriptor must not become an approved terminal result.
    with pytest.raises(AgentGenerationReadModelError) as stale:
        inspector.get(run_id)
    assert stale.value.code == "agent_inspection_not_settled"
    fresh_repository = PostgresRepository(repository.database_url)
    terminal_status = fresh_repository.generation_run_status(run_id)
    status_ref = decisions.publish_evidence(
        "test-terminal-status", canonical_json_bytes(terminal_status)
    )
    context = old_context.model_copy(
        update={
            "generation_status": AgentEvidenceRef(
                uri=status_ref.uri, content_hash=status_ref.content_hash
            ),
            "review_records": tuple(
                AgentEvidenceRef(
                    uri=(decisions.root / "reviews" / f"{item.review_id}.json").as_uri(),
                    content_hash=candidate_proposal_review_record_hash(item),
                )
                for item in reviews
            ),
            "promotion_receipts": tuple(
                AgentEvidenceRef(
                    uri=(decisions.root / "promotions" / f"{item.promotion_id}.json").as_uri(),
                    content_hash=candidate_proposal_promotion_receipt_hash(item),
                )
                for item in promotions
            ),
        }
    )
    result = AgentProposalVerifier(HashedEvidenceReader(case["root"])).verify(context)
    read_model = build_agent_generation_read_model(result)
    assert read_model.human_review_status == "approved"
    assert read_model.package_promotion_status == "promoted"
    assert len(read_model.proposals) == 2
    assert read_model.formal_readiness == "hold"
    assert read_model.performance_conclusion == "not_measured"
    assert read_model.formal_intake_allowed is False
    assert read_model.automatic_release_allowed is False
    report_root = case["root"] / "test-terminal-report"
    report_root.mkdir()
    artifacts = write_agent_generation_report(report_root, context, result)
    publication = build_agent_generation_evidence_publication(context, result, artifacts)
    assert repository.publish_agent_generation_evidence_publication(publication) == publication
    assert (
        fresh_repository.publish_agent_generation_evidence_publication(publication) == publication
    )
    reader = AgentGenerationEvidenceReadService(
        fresh_repository, HashedEvidenceReader(case["root"])
    )
    assert reader.get(run_id) == read_model
    # Existing control-plane API in-process only: no listener or viewer ACL change.
    with (
        patch.dict(
            os.environ, HCUOPT_AGENT_EVIDENCE_ROOT=str(case["root"]), HCUOPT_AUTO_MIGRATE="false"
        ),
        TestClient(create_app(repository=fresh_repository)) as client,
    ):
        url = f"/v1/operator/agent-generations/{run_id}/evidence"
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == read_model.model_dump(mode="json")
        if mode != "read":
            reference = {
                "promotion_receipt": context.promotion_receipts[0],
                "report": publication.report,
                "status_snapshot": context.generation_status,
            }[mode]
            path = file_uri_to_path(reference.uri)
            assert path.is_relative_to(case["root"])
            # Fault injection in pytest's private tree only. Production reports
            # are read-only; test the additional Hash check if bytes are altered.
            path.chmod(path.stat().st_mode | 0o200)
            path.write_bytes(b"{}")
            with pytest.raises(AgentGenerationReadModelError):
                reader.get(run_id)
            denied = client.get(url)
            assert denied.status_code == 422, denied.text
            assert "proposals" not in denied.json()
    assert fresh_repository.generation_run_status(run_id) == terminal_status
    assert len(case["calls"]) == 1  # terminal publication/read never invokes the model


@pytest.mark.parametrize("dispatch_case", ["promotion"], indirect=True)
@pytest.mark.parametrize("failure", ["rejected", "baseline_drift"])
def test_messages_promotion_denial_creates_no_package(dispatch_case, failure):
    case = dispatch_case
    status = _execute(case)
    _, authority, publisher, service = _promotion_services(case)
    proposal = status.proposals[0]
    review = _test_review(
        authority,
        status,
        proposal.proposal_id,
        decision="rejected" if failure == "rejected" else "approved",
    )
    if failure == "baseline_drift":
        case["fixture"]["source"].write_bytes(b"def forward(value):\n    return None\n")
    with pytest.raises(SourceArtifactError, match="rejected|drifted"):
        service.prepare(
            status,
            proposal.proposal_id,
            review.review_id,
            baseline=case["fixture"]["baseline"],
            candidate_id=uuid4(),
            candidate_output_dir=case["tmp_path"] / "promotion-work",
        )
    assert not publisher.root.exists()
    assert len(case["calls"]) == 1
    assert case["repository"].generation_run_status(status.run.generation_run_id) == status

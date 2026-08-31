# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    KnowledgeSnapshot,
)
from hcuopt.contracts.agent_verification_v1 import (
    AgentAttemptEvidence,
    AgentEvidenceRef,
    AgentProposalVerificationContext,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.agent_proposal_reporting import (
    AGENT_GENERATION_WARNING,
    build_agent_generation_evidence,
    write_agent_generation_report,
)
from hcuopt.evaluation.agent_proposal_verifier import (
    AgentProposalEvidenceError,
    AgentProposalVerifier,
    build_agent_generation_read_model,
    normalize_patch_v1,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("10000000-0000-0000-0000-000000000002")
PLAN_ID = UUID("10000000-0000-0000-0000-000000000003")


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _fixed_hash(character: str) -> str:
    return "sha256:" + character * 64


class PortableReader(HashedEvidenceReader):
    def _secure_read(self, path: Path) -> bytes:
        data = path.read_bytes()
        if len(data) > self.max_bytes:
            raise ValueError("too large")
        return data


def _write(path: Path, value: object | bytes) -> dict[str, str]:
    encoded = value if isinstance(value, bytes) else canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {"uri": path.as_uri(), "content_hash": _sha(encoded)}


def _provenance() -> AdapterProvenance:
    return AdapterProvenance(
        profile="m2b-scripted-test",
        capability="candidate_proposal_generation",
        adapter_name="ScriptedAgent",
        adapter_version="1.0.0",
        implementation_kind="fake",
    )


def _knowledge() -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000004"),
        sources=[
            {
                "knowledge_id": "skills/triton-guide",
                "source_kind": "skill",
                "version": "v1",
                "source_uri": "skill://triton-guide/v1",
                "content_hash": _fixed_hash("1"),
                "license_id": "MIT",
            }
        ],
        created_by="scripted-test",
        created_at=NOW,
    )


def _request(knowledge: KnowledgeSnapshot, *, max_proposals: int = 4) -> CandidateGenerationRequest:
    return CandidateGenerationRequest(
        request_id=REQUEST_ID,
        generation_run_id=RUN_ID,
        target_snapshot_id=UUID("10000000-0000-0000-0000-000000000005"),
        stage0_run_id=UUID("10000000-0000-0000-0000-000000000006"),
        baseline_epoch_id=UUID("10000000-0000-0000-0000-000000000007"),
        baseline_source_hash=_fixed_hash("2"),
        hotspot_id=UUID("10000000-0000-0000-0000-000000000008"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="m2b-scripted-workload",
        workload_hash=_fixed_hash("3"),
        configuration_hash=_fixed_hash("4"),
        image_digest=_fixed_hash("5"),
        profiler_evidence_uri="evidence:///profile.json",
        profiler_evidence_hash=_fixed_hash("6"),
        knowledge_snapshot_id=knowledge.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(knowledge),
        max_proposals=max_proposals,
    )


def _plan(
    request: CandidateGenerationRequest,
    *,
    max_attempts: int = 2,
    generator_max_proposals: int = 4,
    budget_max_proposals: int = 4,
    include_second_generator: bool = False,
) -> ApexGenerationPlan:
    generators = [
        {
            "generator_id": "agent-one",
            "adapter_profile": "m2b-agent-one",
            "max_attempts": max_attempts,
            "max_proposals": generator_max_proposals,
            "timeout_seconds": 30,
        }
    ]
    if include_second_generator:
        generators.append(
            {
                "generator_id": "agent-two",
                "adapter_profile": "m2b-agent-two",
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 30,
            }
        )
    return ApexGenerationPlan(
        plan_id=PLAN_ID,
        generation_run_id=RUN_ID,
        request_id=REQUEST_ID,
        request_hash=candidate_generation_request_hash(request),
        generators=generators,
        max_concurrency=1,
        budget={
            "max_generator_attempts": max_attempts + int(include_second_generator),
            "max_wall_seconds": 60,
            "max_total_output_bytes": 100_000,
            "max_total_tokens": 10_000,
            "max_proposals": budget_max_proposals,
        },
        created_by="scripted-apex",
        created_at=NOW,
    )


def _patch(body: str = "+    return x + 1") -> bytes:
    return (
        "diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/sglang/runtime/operator.py\n"
        "+++ b/sglang/runtime/operator.py\n"
        "@@ -1 +1 @@\n"
        "-    return x\n"
        f"{body}\n"
    ).encode()


def _build(
    root: Path,
    *,
    proposals: tuple[bytes, ...] = (_patch(),),
    attempt_status: str = "succeeded",
    cleanup_healthy: bool = True,
    request_max_proposals: int = 4,
    generator_max_proposals: int = 4,
    budget_max_proposals: int = 4,
    include_second_generator: bool = False,
) -> tuple[AgentProposalVerificationContext, AgentProposalVerifier]:
    knowledge = _knowledge()
    request = _request(knowledge, max_proposals=request_max_proposals)
    plan = _plan(
        request,
        max_attempts=1 if attempt_status != "succeeded" else 2,
        generator_max_proposals=generator_max_proposals,
        budget_max_proposals=budget_max_proposals,
        include_second_generator=include_second_generator,
    )
    knowledge_ref = _write(root / "knowledge.json", knowledge.model_dump(mode="json"))
    request_ref = _write(root / "request.json", request.model_dump(mode="json"))
    plan_ref = _write(root / "plan.json", plan.model_dump(mode="json"))
    proposal_models: list[CandidateProposal] = []
    for ordinal, raw in enumerate(proposals):
        patch_ref = _write(root / f"proposal-{ordinal}.diff", raw)
        proposal_models.append(
            CandidateProposal(
                proposal_id=UUID(f"10000000-0000-0000-0000-{ordinal + 20:012d}"),
                request_id=REQUEST_ID,
                generation_run_id=RUN_ID,
                generator_id="agent-one",
                ordinal=ordinal,
                optimization_intent=f"remove redundant conversion {ordinal}",
                rationale="Frozen evidence shows redundant work.",
                risk_summary="Requires independent correctness review.",
                patch_uri=patch_ref["uri"],
                patch_hash=patch_ref["content_hash"],
                normalized_patch_hash=_sha(normalize_patch_v1(raw)),
                touched_paths=["sglang/runtime/operator.py"],
                replacement_point=request.replacement_point,
            )
        )
    raw_output = canonical_json_bytes(
        {"proposal_ids": [str(item.proposal_id) for item in proposal_models]}
    )
    raw_ref = _write(root / "raw-output.json", raw_output)
    batch_ref = None
    if attempt_status == "succeeded":
        batch = CandidateProposalBatch(
            batch_id=UUID("10000000-0000-0000-0000-000000000030"),
            request_id=REQUEST_ID,
            generation_run_id=RUN_ID,
            generator_id="agent-one",
            adapter_provenance=_provenance(),
            status="succeeded",
            proposals=proposal_models,
            raw_output_uri=raw_ref["uri"],
            raw_output_hash=raw_ref["content_hash"],
            output_bytes=len(raw_output),
            attempt_count=1,
            wall_seconds=1.0,
            started_at=NOW,
            finished_at=NOW,
            synthetic=True,
        )
        batch_ref = _write(root / "batch.json", batch.model_dump(mode="json"))
    failure_code = (
        "runner_timeout"
        if attempt_status == "timed_out"
        else "runner_failed"
        if attempt_status == "failed"
        else None
    )
    cleanup = {
        "process_reaped": cleanup_healthy,
        "sandbox_removed": cleanup_healthy,
        "output_sealed": cleanup_healthy,
        "failure_codes": [] if cleanup_healthy else ["sandbox_cleanup_failed"],
    }
    attempt = AgentAttemptEvidence(
        attempt_id=UUID("10000000-0000-0000-0000-000000000031"),
        generation_run_id=RUN_ID,
        request_id=REQUEST_ID,
        plan_id=PLAN_ID,
        generator_id="agent-one",
        attempt_ordinal=0,
        status=attempt_status,
        failure_code=failure_code,
        batch=batch_ref,
        output_bytes=len(raw_output) if batch_ref else 0,
        output_tokens=10 if batch_ref else 0,
        wall_seconds=1.0,
        cleanup=cleanup,
        cleanup_evidence_hash=_sha(canonical_json_bytes(cleanup)),
        started_at=NOW,
        finished_at=NOW,
        adapter_provenance=_provenance(),
        synthetic=True,
    )
    attempt_ref = _write(root / "attempt.json", attempt.model_dump(mode="json"))
    context = AgentProposalVerificationContext(
        task_id=UUID("10000000-0000-0000-0000-000000000040"),
        target_id="nmz36-agent-scripted",
        baseline_epoch_id=request.baseline_epoch_id,
        generation_run_id=RUN_ID,
        knowledge={**knowledge_ref, "identity_hash": knowledge_snapshot_hash(knowledge)},
        request={**request_ref, "identity_hash": candidate_generation_request_hash(request)},
        plan={**plan_ref, "identity_hash": apex_generation_plan_hash(plan)},
        attempts=[attempt_ref],
    )
    return context, AgentProposalVerifier(PortableReader(root))


def _add_second_successful_generator(
    root: Path, context: AgentProposalVerificationContext
) -> AgentProposalVerificationContext:
    request = CandidateGenerationRequest.model_validate_json((root / "request.json").read_bytes())
    raw_patch = _patch("+    return x + 2")
    patch_ref = _write(root / "proposal-agent-two.diff", raw_patch)
    proposal = CandidateProposal(
        proposal_id=UUID("10000000-0000-0000-0000-000000000099"),
        request_id=REQUEST_ID,
        generation_run_id=RUN_ID,
        generator_id="agent-two",
        ordinal=0,
        optimization_intent="remove another redundant conversion",
        rationale="Frozen evidence shows a second redundant operation.",
        risk_summary="Requires independent correctness review.",
        patch_uri=patch_ref["uri"],
        patch_hash=patch_ref["content_hash"],
        normalized_patch_hash=_sha(normalize_patch_v1(raw_patch)),
        touched_paths=["sglang/runtime/operator.py"],
        replacement_point=request.replacement_point,
    )
    raw_output = canonical_json_bytes({"proposal_ids": [str(proposal.proposal_id)]})
    raw_ref = _write(root / "raw-output-agent-two.json", raw_output)
    batch = CandidateProposalBatch(
        batch_id=UUID("10000000-0000-0000-0000-000000000098"),
        request_id=REQUEST_ID,
        generation_run_id=RUN_ID,
        generator_id="agent-two",
        adapter_provenance=_provenance(),
        status="succeeded",
        proposals=[proposal],
        raw_output_uri=raw_ref["uri"],
        raw_output_hash=raw_ref["content_hash"],
        output_bytes=len(raw_output),
        attempt_count=1,
        wall_seconds=1.0,
        started_at=NOW,
        finished_at=NOW,
        synthetic=True,
    )
    batch_ref = _write(root / "batch-agent-two.json", batch.model_dump(mode="json"))
    cleanup = {
        "process_reaped": True,
        "sandbox_removed": True,
        "output_sealed": True,
        "failure_codes": [],
    }
    attempt = AgentAttemptEvidence(
        attempt_id=UUID("10000000-0000-0000-0000-000000000097"),
        generation_run_id=RUN_ID,
        request_id=REQUEST_ID,
        plan_id=PLAN_ID,
        generator_id="agent-two",
        attempt_ordinal=0,
        status="succeeded",
        batch=batch_ref,
        output_bytes=len(raw_output),
        output_tokens=10,
        wall_seconds=1.0,
        cleanup=cleanup,
        cleanup_evidence_hash=_sha(canonical_json_bytes(cleanup)),
        started_at=NOW,
        finished_at=NOW,
        adapter_provenance=_provenance(),
        synthetic=True,
    )
    attempt_ref = _write(root / "attempt-agent-two.json", attempt.model_dump(mode="json"))
    return context.model_copy(
        update={"attempts": (*context.attempts, AgentEvidenceRef.model_validate(attempt_ref))}
    )


def test_recomputes_all_hashes_and_exposes_read_only_model(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)

    result = verifier.verify(context)
    read_model = build_agent_generation_read_model(result)

    assert result.status == "ready_for_review"
    assert result.proposals[0].status == "kept"
    assert result.knowledge_hash == context.knowledge.identity_hash
    assert result.request_hash == context.request.identity_hash
    assert result.plan_hash == context.plan.identity_hash
    assert read_model.status == result.status
    assert read_model.formal_intake_allowed is False
    assert read_model.automatic_release_allowed is False
    assert read_model.performance_conclusion == "not_measured"
    assert read_model.human_review_status == "pending"
    assert read_model.package_promotion_status == "pending"


def test_exact_and_normalized_patch_duplicates_are_eliminated(tmp_path: Path) -> None:
    first = _patch()
    exact_context, verifier = _build(tmp_path / "exact", proposals=(first, first))
    exact = verifier.verify(exact_context)
    assert [item.reason_code for item in exact.proposals] == [None, "duplicate_exact_patch"]

    normalized_variant = first.replace(b"\n", b"\r\n").replace(
        b"index 1111111..2222222 100644\r\n", b"index aaa..bbb 100644\r\n"
    )
    context, verifier = _build(tmp_path / "normalized", proposals=(first, normalized_variant))
    result = verifier.verify(context)
    assert result.proposals[1].reason_code == "duplicate_normalized_patch"


def test_all_prior_duplicates_produces_no_valid_proposals(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    initial = verifier.verify(context)
    context = context.model_copy(
        update={"previous_exact_patch_hashes": frozenset({initial.proposals[0].exact_patch_hash})}
    )

    result = verifier.verify(context)

    assert result.status == "no_valid_proposals"
    assert result.proposals[0].reason_code == "duplicate_exact_patch"
    assert "zero_valid_proposals" in result.failure_codes


def test_runner_timeout_and_cleanup_failure_are_preserved(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "timeout", proposals=(), attempt_status="timed_out")
    timeout = verifier.verify(context)
    assert timeout.status == "no_valid_proposals"
    assert set(timeout.failure_codes) == {"runner_timeout", "zero_valid_proposals"}

    context, verifier = _build(tmp_path / "cleanup", cleanup_healthy=False)
    cleanup = verifier.verify(context)
    assert cleanup.status == "invalid"
    assert "cleanup_failed" in cleanup.failure_codes
    assert cleanup.attempts[0].status == "invalid"
    assert cleanup.proposals[0].status == "invalid"


def test_zero_proposal_failed_runner_is_not_success(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, proposals=(), attempt_status="failed")

    result = verifier.verify(context)

    assert result.status == "no_valid_proposals"
    assert set(result.failure_codes) == {"runner_failed", "zero_valid_proposals"}


@pytest.mark.parametrize("document", ["knowledge", "request", "plan"])
def test_tampered_domain_hash_is_rejected(tmp_path: Path, document: str) -> None:
    context, verifier = _build(tmp_path)
    reference = getattr(context, document)
    bad = reference.model_copy(update={"identity_hash": _fixed_hash("f")})
    context = context.model_copy(update={document: bad})

    with pytest.raises(AgentProposalEvidenceError, match="identity hash mismatch"):
        verifier.verify(context)


def test_tampered_patch_bytes_are_rejected(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    (tmp_path / "proposal-0.diff").write_bytes(_patch("+    return x + 999"))

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "evidence_hash_mismatch"


def test_evidence_and_report_are_deterministic_and_immutable(tmp_path: Path) -> None:
    evidence_root = tmp_path / "raw"
    report_root = tmp_path / "report"
    report_root.mkdir()
    context, verifier = _build(evidence_root)
    result = verifier.verify(context)

    first = write_agent_generation_report(report_root, context, result)
    second = write_agent_generation_report(report_root, context, result)
    bundle = build_agent_generation_evidence(context, result)

    assert first == second
    assert bundle.evidence_id == build_agent_generation_evidence(context, result).evidence_id
    assert bundle.synthetic is True
    assert bundle.summary["performance_conclusion"] == "not_measured"
    assert bundle.summary["automatic_release_allowed"] is False
    assert AGENT_GENERATION_WARNING in (report_root / "report.md").read_text("utf-8")
    assert set(first) == {
        "verification.json",
        "evidence-bundle.json",
        "read-model.json",
        "report.md",
        "sha256sums.json",
    }

    (report_root / "report.md").chmod(0o644)
    (report_root / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="different content"):
        write_agent_generation_report(report_root, context, result)


def test_attempt_order_does_not_change_verdict_or_digest(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)
    context = _add_second_successful_generator(tmp_path, context)

    forward = verifier.verify(context)
    reverse = verifier.verify(
        context.model_copy(update={"attempts": tuple(reversed(context.attempts))})
    )

    assert forward == reverse
    assert forward.input_digest == reverse.input_digest
    assert [item.generator_id for item in forward.proposals] == ["agent-one", "agent-two"]


def test_missing_planned_generator_fails_closed(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "attempt_barrier_incomplete"


def test_cross_baseline_context_is_rejected(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    context = context.model_copy(
        update={"baseline_epoch_id": UUID("20000000-0000-0000-0000-000000000001")}
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "baseline_binding_mismatch"


def test_request_and_generator_proposal_budgets_are_recomputed(tmp_path: Path) -> None:
    context, verifier = _build(
        tmp_path / "request",
        request_max_proposals=3,
        budget_max_proposals=4,
    )
    with pytest.raises(AgentProposalEvidenceError) as request_error:
        verifier.verify(context)
    assert request_error.value.code == "request_proposal_budget_exceeded"

    proposals = tuple(_patch(f"+    return x + {index}") for index in range(3))
    context, verifier = _build(
        tmp_path / "generator",
        proposals=proposals,
        request_max_proposals=4,
        generator_max_proposals=2,
        budget_max_proposals=4,
    )
    with pytest.raises(AgentProposalEvidenceError) as generator_error:
        verifier.verify(context)
    assert generator_error.value.code == "generator_proposal_budget_exceeded"


def test_lifecycle_cannot_be_declared_by_verifier_caller(tmp_path: Path) -> None:
    context, _ = _build(tmp_path)
    payload = context.model_dump(mode="json")
    payload["human_review"] = {"status": "accepted"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentProposalVerificationContext.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("batch_id", "duplicate_batch"),
        ("proposal_id", "duplicate_proposal_identity"),
    ],
)
def test_cross_batch_identities_are_rejected(
    tmp_path: Path, field: str, expected_code: str
) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)
    context = _add_second_successful_generator(tmp_path, context)
    second_attempt = json.loads((tmp_path / "attempt-agent-two.json").read_text("utf-8"))
    second_batch = json.loads((tmp_path / "batch-agent-two.json").read_text("utf-8"))
    if field == "batch_id":
        second_batch["batch_id"] = "10000000-0000-0000-0000-000000000030"
    else:
        second_batch["proposals"][0]["proposal_id"] = "10000000-0000-0000-0000-000000000020"
    batch_ref = _write(tmp_path / "batch-agent-two.json", second_batch)
    second_attempt["batch"] = batch_ref
    attempt_ref = _write(tmp_path / "attempt-agent-two.json", second_attempt)
    context = context.model_copy(
        update={
            "attempts": (
                context.attempts[0],
                AgentEvidenceRef.model_validate(attempt_ref),
            )
        }
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == expected_code


def test_attempt_and_batch_usage_must_match(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    attempt = json.loads((tmp_path / "attempt.json").read_text("utf-8"))
    attempt["wall_seconds"] = 2.0
    attempt_ref = _write(tmp_path / "attempt.json", attempt)
    context = context.model_copy(
        update={"attempts": (AgentEvidenceRef.model_validate(attempt_ref),)}
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "attempt_usage_mismatch"

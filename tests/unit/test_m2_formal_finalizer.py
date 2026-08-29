# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.contracts.m2 import RoundBudget, SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    formal_authority_context_ref,
    publish_formal_authority_context,
)
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.evaluation.m2_formal_authority import FormalHoldoutRevealPersistence
from hcuopt.evaluation.m2_formal_finalizer import (
    FormalM2EvidenceIndex,
    FormalM2EvidenceIndexEntry,
    M2FormalRoundFinalizer,
    build_formal_round_evidence,
    formal_round_evidence_requirements,
)
from hcuopt.evaluation.m2_models import (
    AdjustedCandidateResult,
    BarrierMemberResult,
    MultipleComparisonResult,
    RoundBarrierResult,
)
from hcuopt.evaluation.m2_statistics import (
    M2_FWER_PROTOCOL_VERSION,
    holdout_family_hash,
    m2_fwer_protocol_hash,
    recompute_multiple_comparison_result_hash,
)
from hcuopt.evaluation.m2_verifier import M2RoundEvidenceError
from hcuopt.measurement.evidence import EvidenceArtifact, canonical_json_bytes

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


class _FormalStore:
    def __init__(self) -> None:
        self._by_uri: dict[str, bytes] = {}
        self._by_hash: dict[str, EvidenceArtifact] = {}

    def publish(self, value: object) -> EvidenceArtifact:
        return self.publish_bytes(canonical_json_bytes(value))

    def publish_bytes(self, encoded: bytes) -> EvidenceArtifact:
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        artifact = self._by_hash.get(digest)
        if artifact is None:
            uri = f"file:///protected/m2/{len(self._by_hash):04d}-{digest[7:]}.json"
            artifact = EvidenceArtifact(uri=uri, sha256=digest, byte_count=len(encoded))
            self._by_hash[digest] = artifact
            self._by_uri[uri] = bytes(encoded)
        elif self._by_uri[artifact.uri] != encoded:
            raise AssertionError("test store collision")
        return artifact

    def artifact_for_hash(self, expected_hash: str) -> EvidenceArtifact:
        return self._by_hash[expected_hash]

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        encoded = self._by_uri[uri]
        actual = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual != expected_hash:
            raise M2RoundEvidenceError("evidence_hash_mismatch", "test evidence changed")
        return encoded

    def tamper(self, uri: str) -> None:
        self._by_uri[uri] = b'{"tampered":true}\n'


def _fixture() -> tuple[
    _FormalStore,
    object,
    SearchRound,
    RoundBarrierResult,
    str,
]:
    store = _FormalStore()
    refs = {
        label: store.publish({"label": label})
        for label in (
            "stage0",
            "target-profile",
            "workload-profile",
            "measurement-profile",
            "candidate-family",
            "artifact-family",
            "search-plan",
            "holdout-commitment",
            "holdout-authority",
            "selection-rule",
            "store",
            "store-policy",
            "verifier",
            "artifact-0",
            "artifact-1",
            "correctness-0",
            "correctness-1",
            "budget-0",
            "budget-1",
            "cleanup-0",
            "cleanup-1",
            "search-input-summary",
            "budget-ledger",
        )
    }
    context = publish_formal_authority_context(
        FormalAuthorityContextContent(
            authority_context_id=_uuid(1),
            round_id=_uuid(2),
            task_id=_uuid(3),
            target_snapshot_id=_uuid(4),
            stage0_run_id=_uuid(5),
            stage0_protocol_hash=refs["stage0"].sha256,
            baseline_epoch_id=_uuid(6),
            hotspot_id=_uuid(7),
            target_profile_hash=refs["target-profile"].sha256,
            workload_profile_hash=refs["workload-profile"].sha256,
            measurement_profile_hash=refs["measurement-profile"].sha256,
            candidate_family_hash=refs["candidate-family"].sha256,
            artifact_family_hash=refs["artifact-family"].sha256,
            search_plan_hash=refs["search-plan"].sha256,
            holdout_plan_commitment=refs["holdout-commitment"].sha256,
            holdout_plan_authority_id="d-holdout-authority-v1",
            holdout_plan_authority_hash=refs["holdout-authority"].sha256,
            selection_rule_hash=refs["selection-rule"].sha256,
            evidence_store=FormalEvidenceStoreRef(
                store_id="formal-evidence-v1",
                store_version=1,
                store_hash=refs["store"].sha256,
                access_policy_hash=refs["store-policy"].sha256,
            ),
            verifier=FormalVerifierRef(
                verifier_id="m2-d-verifier",
                verifier_version="m2-d-formal-v1",
                verifier_hash=refs["verifier"].sha256,
            ),
            sealed_by="operator-a",
            sealed_at=NOW,
        )
    )
    round_authority = SearchRound(
        round_id=context.round_id,
        task_id=context.task_id,
        idempotency_key="formal-finalizer-round",
        state=SearchRoundState.SEARCH_BARRIER,
        run_mode=SearchRoundRunMode.FORMAL,
        project_mode="degraded_manual_intake",
        target_snapshot_id=context.target_snapshot_id,
        stage0_run_id=context.stage0_run_id,
        stage0_protocol_hash=context.stage0_protocol_hash,
        baseline_epoch_id=context.baseline_epoch_id,
        hotspot_id=context.hotspot_id,
        replacement_point="sglang.formal.hotspot",
        workload_id="formal-workload",
        workload_hash=refs["workload-profile"].sha256,
        configuration_hash=store.publish({"label": "configuration"}).sha256,
        image_digest=store.publish({"label": "image"}).sha256,
        adapter_profile="formal-adapter-v1",
        declared_candidate_count=2,
        max_promoted=1,
        family_alpha=0.05,
        search_plan_hash=context.search_plan_hash,
        holdout_plan_commitment=context.holdout_plan_commitment,
        holdout_plan_authority_id=context.holdout_plan_authority_id,
        holdout_plan_authority_hash=context.holdout_plan_authority_hash,
        selection_rule_hash=context.selection_rule_hash,
        budget=RoundBudget(
            max_candidates=2,
            max_build_attempts=2,
            max_correctness_attempts=2,
            max_search_samples=20,
            max_holdout_samples=20,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=300,
        ),
        candidate_family_hash=context.candidate_family_hash,
        artifact_family_hash=context.artifact_family_hash,
        automatic_release_allowed=False,
        version=1,
        created_at=NOW,
    )
    members = tuple(
        BarrierMemberResult(
            round_candidate_id=_uuid(20 + ordinal),
            candidate_id=_uuid(30 + ordinal),
            candidate_state=RoundCandidateState.SEARCH_MEASURED,
            artifact_id=_uuid(40 + ordinal),
            artifact_hash=refs[f"artifact-{ordinal}"].sha256,
            correctness_evidence_hash=refs[f"correctness-{ordinal}"].sha256,
            round_measurement_ref_id=_uuid(50 + ordinal),
            budget_usage_evidence_hash=refs[f"budget-{ordinal}"].sha256,
            cleanup_evidence_hash=refs[f"cleanup-{ordinal}"].sha256,
            synthetic=False,
        )
        for ordinal in range(2)
    )
    search_barrier = RoundBarrierResult(
        barrier_id=_uuid(60),
        round_id=context.round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        phase=RoundPhase.SEARCH,
        input_family_hash=context.artifact_family_hash,
        expected_member_count=2,
        members=members,
        rule_version="m2-search-v1",
        rule_hash=context.selection_rule_hash,
        promoted_candidate_ids=(),
        outcome=RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE,
        input_summary_hash=refs["search-input-summary"].sha256,
        closed_by=context.verifier.verifier_id,
        closed_at=NOW,
        idempotency_key="formal-search-barrier-001",
    )
    return store, context, round_authority, search_barrier, refs["budget-ledger"].sha256


def _holdout_fixture():
    store, context, round_authority, zero_barrier, budget_hash = _fixture()
    promoted = zero_barrier.members[:1]
    family_hash = holdout_family_hash(
        round_authority=round_authority,
        members=promoted,
    )
    search_barrier = zero_barrier.model_copy(
        update={
            "promoted_candidate_ids": (promoted[0].candidate_id,),
            "outcome": RoundBarrierOutcome.MEMBERS_PROMOTED,
        }
    )
    plan = store.publish({"phase": "holdout", "restart_count": 5})
    reveal_evidence = store.publish({"phase": "holdout", "revealed": True})
    reveal = FormalHoldoutRevealPersistence(
        context=formal_authority_context_ref(context),
        search_barrier_id=search_barrier.barrier_id,
        reveal_lease_id=_uuid(61),
        fencing_token=7,
        holdout_family_hash=family_hash,
        holdout_plan_hash=plan.sha256,
        reveal_evidence_uri=reveal_evidence.uri,
        reveal_evidence_hash=reveal_evidence.sha256,
        revealed_by=context.verifier.verifier_id,
        revealed_at=NOW,
    )
    holdout_raw = store.publish({"phase": "holdout", "raw": True})
    holdout_baseline = store.publish({"phase": "holdout", "baseline": True})
    holdout_budget = store.publish({"phase": "holdout", "budget": True})
    holdout_cleanup = store.publish({"phase": "holdout", "cleanup": True})
    holdout_member = promoted[0].model_copy(
        update={
            "candidate_state": RoundCandidateState.HOLDOUT_MEASURED,
            "round_measurement_ref_id": _uuid(62),
            "budget_usage_evidence_hash": holdout_budget.sha256,
            "cleanup_evidence_hash": holdout_cleanup.sha256,
        }
    )
    holdout_barrier = RoundBarrierResult(
        barrier_id=_uuid(63),
        round_id=context.round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        phase=RoundPhase.HOLDOUT,
        input_family_hash=family_hash,
        expected_member_count=1,
        members=(holdout_member,),
        rule_version="m2-holdout-v1",
        rule_hash=context.selection_rule_hash,
        promoted_candidate_ids=(),
        outcome=RoundBarrierOutcome.COMPLETED,
        input_summary_hash=store.publish({"phase": "holdout", "summary": True}).sha256,
        closed_by=context.verifier.verifier_id,
        closed_at=NOW,
        idempotency_key="formal-holdout-barrier-001",
    )
    adjusted = AdjustedCandidateResult(
        candidate_id=holdout_member.candidate_id,
        round_measurement_ref_id=holdout_member.round_measurement_ref_id,
        synthetic=False,
        correctness_evidence_hash=holdout_member.correctness_evidence_hash,
        raw_evidence_hash=holdout_raw.sha256,
        baseline_sample_set_hash=holdout_baseline.sha256,
        verdict=ManualCandidateVerdict.INCONCLUSIVE,
        adjusted_ci_lower=-0.01,
        adjusted_ci_upper=0.01,
        stage0_mde_ratio=0.02,
        workload_mde_ratio=0.02,
        credible_threshold=0.02,
    )
    comparison = MultipleComparisonResult(
        multiple_comparison_id=_uuid(64),
        round_id=context.round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        holdout_barrier_id=holdout_barrier.barrier_id,
        holdout_family_hash=family_hash,
        protocol_version=M2_FWER_PROTOCOL_VERSION,
        protocol_hash=m2_fwer_protocol_hash(),
        family_alpha=0.05,
        m=1,
        alpha_candidate=0.05,
        candidate_results=(adjusted,),
        result_hash="sha256:" + "0" * 64,
        created_at=NOW,
    )
    comparison = comparison.model_copy(
        update={"result_hash": recompute_multiple_comparison_result_hash(comparison)}
    )
    round_authority = round_authority.model_copy(
        update={
            "state": SearchRoundState.HOLDOUT_BARRIER,
            "holdout_family_hash": family_hash,
            "holdout_plan_hash": plan.sha256,
            "holdout_reveal_lease_id": reveal.reveal_lease_id,
            "holdout_reveal_evidence_hash": reveal.reveal_evidence_hash,
        }
    )
    return (
        store,
        context,
        round_authority,
        search_barrier,
        reveal,
        holdout_barrier,
        comparison,
        budget_hash,
    )


def _publish_index(
    store: _FormalStore,
    context,
    requirements,
    *,
    drift_role: str | None = None,
) -> EvidenceArtifact:
    entries = []
    for requirement in requirements:
        if requirement.expected_bytes is not None:
            artifact = store.publish_bytes(requirement.expected_bytes)
            assert artifact.sha256 == requirement.sha256
        else:
            artifact = store.artifact_for_hash(requirement.sha256)
        producer_role = requirement.producer_role
        if requirement.role == drift_role:
            producer_role = "measurement_producer"
        entries.append(
            FormalM2EvidenceIndexEntry(
                role=requirement.role,
                evidence_type=requirement.evidence_type,
                uri=artifact.uri,
                sha256=artifact.sha256,
                producer_role=producer_role,
                producer_id=requirement.producer_id or f"producer-{requirement.producer_role}",
                producer_hash=requirement.producer_hash
                or store.publish({"producer": requirement.producer_role}).sha256,
                retention_owner="m2-formal-test",
                accessibility_checked_at=NOW,
            )
        )
    index = FormalM2EvidenceIndex(
        round_id=context.round_id,
        authority_context_id=context.authority_context_id,
        authority_context_hash=context.context_hash,
        evidence_store_id=context.evidence_store.store_id,
        evidence_store_hash=context.evidence_store.store_hash,
        entries=tuple(entries),
        created_at=NOW,
    )
    return store.publish(index)


def test_formal_finalizer_rebuilds_zero_promotion_bundle() -> None:
    store, context, round_authority, search_barrier, budget_hash = _fixture()
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
    )
    index = _publish_index(store, context, requirements)

    bundle = build_formal_round_evidence(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
        evidence_index_uri=index.uri,
        evidence_index_hash=index.sha256,
        evidence_reader=store,
    )
    M2FormalRoundFinalizer(store).verify(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        holdout_reveal=None,
        holdout_barrier=None,
        multiple_comparison=None,
        evidence_bundle=bundle,
    )

    assert bundle.run_mode is SearchRoundRunMode.FORMAL
    assert bundle.synthetic is False
    assert bundle.automatic_release_allowed is False
    assert bundle.summary["performance_conclusion"] == "formal_single_operation_only"


def test_measurement_producer_cannot_impersonate_independent_barrier_verifier() -> None:
    store, context, round_authority, search_barrier, budget_hash = _fixture()
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
    )
    index = _publish_index(store, context, requirements, drift_role="search/barrier")

    with pytest.raises(M2RoundEvidenceError, match="authority for search/barrier"):
        build_formal_round_evidence(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=budget_hash,
            evidence_index_uri=index.uri,
            evidence_index_hash=index.sha256,
            evidence_reader=store,
        )


def test_formal_finalizer_rejects_tampered_or_remote_evidence() -> None:
    store, context, round_authority, search_barrier, budget_hash = _fixture()
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
    )
    index = _publish_index(store, context, requirements)
    plan = next(entry for entry in requirements if entry.role == "search/plan")
    store.tamper(store.artifact_for_hash(plan.sha256).uri)

    with pytest.raises(M2RoundEvidenceError, match="test evidence changed"):
        build_formal_round_evidence(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=budget_hash,
            evidence_index_uri=index.uri,
            evidence_index_hash=index.sha256,
            evidence_reader=store,
        )
    with pytest.raises(ValueError, match="absolute local file URI"):
        build_formal_round_evidence(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=budget_hash,
            evidence_index_uri="https://remote.invalid/evidence-index.json",
            evidence_index_hash=index.sha256,
            evidence_reader=store,
        )


def test_formal_finalizer_preserves_structured_reader_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, context, round_authority, search_barrier, budget_hash = _fixture()
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
    )
    index = _publish_index(store, context, requirements)
    plan = next(entry for entry in requirements if entry.role == "search/plan")
    plan_uri = store.artifact_for_hash(plan.sha256).uri
    original_read = store.read_raw_bytes

    def read_with_hash_failure(uri: str, expected_hash: str) -> bytes:
        if uri == plan_uri:
            raise EvidenceReadError("evidence_hash_mismatch", "test reader mismatch")
        return original_read(uri, expected_hash)

    monkeypatch.setattr(store, "read_raw_bytes", read_with_hash_failure)

    with pytest.raises(M2RoundEvidenceError) as captured:
        build_formal_round_evidence(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=budget_hash,
            evidence_index_uri=index.uri,
            evidence_index_hash=index.sha256,
            evidence_reader=store,
        )

    assert captured.value.code == "evidence_hash_mismatch"


def test_formal_finalizer_rebuilds_completed_holdout_bundle() -> None:
    (
        store,
        context,
        round_authority,
        search_barrier,
        reveal,
        holdout_barrier,
        comparison,
        budget_hash,
    ) = _holdout_fixture()
    requirements = formal_round_evidence_requirements(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
        holdout_reveal=reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=comparison,
    )
    index = _publish_index(store, context, requirements)

    bundle = build_formal_round_evidence(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        budget_ledger_hash=budget_hash,
        evidence_index_uri=index.uri,
        evidence_index_hash=index.sha256,
        evidence_reader=store,
        holdout_reveal=reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=comparison,
    )
    M2FormalRoundFinalizer(store).verify(
        context=context,
        round_authority=round_authority,
        search_barrier=search_barrier,
        holdout_reveal=reveal,
        holdout_barrier=holdout_barrier,
        multiple_comparison=comparison,
        evidence_bundle=bundle,
    )

    assert bundle.holdout_family_hash == reveal.holdout_family_hash
    assert bundle.multiple_comparison_id == comparison.multiple_comparison_id
    assert bundle.summary["recommended_candidate_id"] is None
    assert bundle.summary["scope_warning"].startswith("not a model")


def test_formal_finalizer_rejects_phase_failure_without_cleanup_evidence() -> None:
    store, context, round_authority, search_barrier, budget_hash = _fixture()
    failed = search_barrier.members[0].model_copy(
        update={
            "candidate_state": RoundCandidateState.SEARCH_FAILED,
            "round_measurement_ref_id": None,
            "failure_evidence_hash": store.publish({"failure": True}).sha256,
            "cleanup_evidence_hash": None,
        }
    )
    search_barrier = search_barrier.model_copy(
        update={"members": (failed, search_barrier.members[1])}
    )

    with pytest.raises(M2RoundEvidenceError, match="terminal cleanup evidence"):
        formal_round_evidence_requirements(
            context=context,
            round_authority=round_authority,
            search_barrier=search_barrier,
            budget_ledger_hash=budget_hash,
        )


@pytest.mark.skipif(os.name != "posix", reason="secure openat/O_NOFOLLOW is POSIX-only")
def test_formal_reader_rejects_remote_escape_and_symlink(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    encoded = canonical_json_bytes({"protected": True})
    evidence = protected / "evidence.json"
    evidence.write_bytes(encoded)
    expected_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    reader = HashedEvidenceReader(protected)

    assert reader.read_raw_bytes(evidence.as_uri(), expected_hash) == encoded
    with pytest.raises(EvidenceReadError, match="only local file"):
        reader.read_raw_bytes("https://remote.invalid/evidence.json", expected_hash)

    outside = tmp_path / "outside.json"
    outside.write_bytes(encoded)
    with pytest.raises(EvidenceReadError, match="escapes allowed root"):
        reader.read_raw_bytes(outside.as_uri(), expected_hash)

    link = protected / "linked.json"
    link.symlink_to(outside)
    with pytest.raises(EvidenceReadError, match="symbolic link"):
        reader.read_raw_bytes(link.as_uri(), expected_hash)

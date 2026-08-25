"""Formal nmz36 driver for the single M1 allocator business Candidate.

This module is deployment code, not a general optimizer.  It binds one accepted
Formal Stage 0 run, one immutable SGLang baseline, one profiler trace, and one
reviewed startup-overlay Candidate.  The driver stops at human signoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from psycopg.types.json import Jsonb

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.adapters.profiles import (
    REAL_MANUAL_CANDIDATE_PROFILE,
    AdapterProfileCatalog,
    real_manual_candidate_profile,
)
from hcuopt.adapters.real_profile import (
    build_m1_adjudication_registry,
    build_m1_correctness_registry,
    build_m1_measurement_registry,
    compose_nmz36_m1_registry,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.api.app import create_app
from hcuopt.contracts.m1 import (
    CandidateOverlayFile,
    CandidateSourcePackageManifest,
)
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import (
    ManualCandidateCreate,
    ManualCandidateTaskCreate,
    ManualHotspotIntakeCreate,
)
from hcuopt.deployment.nmz36_m1_allocator import (
    ALLOCATOR_MOUNT_TARGET,
    ALLOCATOR_RELATIVE_PATH,
    ALLOCATOR_REPLACEMENT_POINT,
    Nmz36M1AllocatorCorrectnessEvidenceProducer,
    Nmz36M1AllocatorWorkloadFactory,
    Nmz36M1DeviceTimerFactory,
    build_m1_allocator_hotspot_spec,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobState,
    JobType,
    ManualCandidateKind,
    ProjectMode,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import (
    load_registered_m1_protocol,
    m1_hotspot_spec_sha256,
)
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence
from hcuopt.measurement.nmz36_runtime import (
    DockerProcessLifecycleRecorder,
    HySmiTelemetryCollector,
    ManagedProcessRegistry,
)
from hcuopt.source_hash import canonical_source_hash
from hcuopt.storage.migrations import migration_plan
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import target_fingerprint
from hcuopt.workers.sdk import Worker

LOGGER = logging.getLogger(__name__)

PROFILE = REAL_MANUAL_CANDIDATE_PROFILE
STAGE0_RUN_ID = UUID("dd50c381-75dd-5a64-9211-640c602dc817")
STAGE0_TASK_ID = UUID("f6bfb71f-8efa-5d1a-8e0e-9233268b604b")
TARGET_SNAPSHOT_ID = UUID("ffb9f94e-2035-5ff6-9b06-f1b5fb163196")
FORMAL_PERFORMANCE_JOB_ID = UUID("7a95738b-b4cd-41bb-88ba-66fa44ab4a1f")
TARGET_FINGERPRINT = (
    "sha256:20e91f499d2381fb4a7674da5446802a35f12a7e5195e233d50fe920bced0242"
)
BASELINE_COMMIT = "dad582f28458cd0e11e0be675fbe7fcc7ab65ac1"
BASELINE_TREE_HASH = "a883d7eb4b4667768c19f0c9b0456d41f52da91c"
BASELINE_SOURCE_HASH = (
    "sha256:215e9259a7afd87e93924487a8153e5e80f6f831e5a0056dce5350e031363c0b"
)
CANDIDATE_SOURCE_HASH = (
    "sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2"
)
CANDIDATE_FILE_HASH = (
    "sha256:93bd2a5aec5a01cb61ac8c6f2ddca7bd25d63ffe4aec8fed7f60199c3581da8a"
)
PROFILER_TRACE_HASH = (
    "sha256:17ff2c0aa23a5365ab7478f0b1cb0756d11509c135b5cf2ddb48baa20b77392a"
)
IMAGE_DIGEST = (
    "sha256:a959b1d27fa7fada705bcb619331b0bc9a41bd67a7c2462b515a77718f108f1c"
)

TASK_KEY = "m1-nmz36-allocator-free-20260825-v1"
HOTSPOT_KEY = "m1-nmz36-allocator-free-hotspot-20260825-v1"
CANDIDATE_KEY = "m1-nmz36-allocator-unique-consecutive-20260825-v1"
WORKLOAD_ID = "m1-qwen2.5-0.5b-prefill-4090-1-c1"

FORMAL_ROOT = Path("/home/github/hcu-auto-opt-m1-formal-v1")
TRUSTED_SOURCE_ROOT = FORMAL_ROOT / "candidate-source-intake"
BUILD_ROOT = FORMAL_ROOT / "build-worker"
ALLOWED_EVIDENCE_ROOT = Path("/home/github/lyt/Asari/hcuopt-s0-formal-EFhRuc")
EVIDENCE_ROOT = ALLOWED_EVIDENCE_ROOT / "trusted-evidence-v4"
PROFILER_TRACE = Path(
    "/home/github/hcu-auto-opt-m1-runs/20260825-prefill-v7-hcu7-uncached/"
    "traces/1787646066.6493704/"
    "m1-prefill-v2-1787646066.6515782-TP-0.trace.json.gz"
)
PROFILER_REPORT = Path(
    "/home/github/hcu-auto-opt-m1-runs/20260825-prefill-v7-hcu7-uncached/"
    "profiler_analysis_report/torch_profiler_analysis.md"
)
REVIEWED_CANDIDATE_ROOT = Path("/home/github/hcuopt-m1-dev-allocator-v1")
DATABASE_URL_DEFAULT = "postgresql://hcuopt:hcuopt@127.0.0.1:55432/hcuopt"
API_URL_DEFAULT = "http://127.0.0.1:8017"


@dataclass(frozen=True, slots=True)
class FormalIds:
    task_id: UUID
    baseline_snapshot_id: UUID
    baseline_epoch_id: UUID
    hotspot_id: UUID
    candidate_id: UUID


def formal_ids() -> FormalIds:
    task_id = uuid5(NAMESPACE_URL, f"hcuopt:m1-task:{TASK_KEY}")
    return FormalIds(
        task_id=task_id,
        baseline_snapshot_id=uuid5(
            NAMESPACE_URL,
            f"hcuopt:m1-baseline-source:{STAGE0_RUN_ID}:{BASELINE_SOURCE_HASH}",
        ),
        baseline_epoch_id=uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:m1-baseline:v1"),
        hotspot_id=uuid5(NAMESPACE_URL, f"hcuopt:m1-hotspot:{HOTSPOT_KEY}"),
        candidate_id=uuid5(NAMESPACE_URL, f"hcuopt:m1-candidate:{CANDIDATE_KEY}"),
    )


def performance_job_id() -> UUID:
    """Return the DB-assigned Job ID frozen after the Formal Performance enqueue."""

    return FORMAL_PERFORMANCE_JOB_ID


def workload_document() -> dict[str, Any]:
    return {
        "schema_version": "hcuopt-m1-workload-v1",
        "workload_id": WORKLOAD_ID,
        "model": "Qwen2.5-0.5B",
        "stage": "prefill",
        "input_tokens": 4090,
        "output_tokens": 1,
        "concurrency": 1,
        "page_size": 64,
        "radix_cache": False,
        "profile_prompt_count": 5,
        "selected_kernel_case": "target-4090-direct-nosort",
        "candidate_scope": "single_request_page_contiguous_allocator_free",
    }


def configuration_document(target: TargetSpec) -> dict[str, Any]:
    return {
        "schema_version": "hcuopt-m1-configuration-v1",
        "adapter_profile": PROFILE,
        "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
        "target_fingerprint": TARGET_FINGERPRINT,
        "image_digest": target.inference_image.registry_digest,
        "source_commit": BASELINE_COMMIT,
        "hcu_device": 7,
        "numa_node": 7,
        "cpu_affinity": "112-127",
        "correctness_protocol": "m1-kernel-correctness-v1",
        "measurement_protocol": "m1-kernel-performance-v1",
        "cache_policy": "fresh_namespace_per_acquisition",
        "process_policy": "fresh_process_per_acquisition",
        "acquisition_order": ["baseline", "candidate", "candidate", "baseline"] * 10,
        "warmup_count": 10,
        "samples_per_acquisition": 10,
        "batch_iterations": 5000,
    }


app = create_app(
    adapter_profiles=AdapterProfileCatalog((real_manual_candidate_profile(),)),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run(argv: tuple[str, ...], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
        shell=False,
    )
    return result


def _require_command(argv: tuple[str, ...], *, timeout: int = 60) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}; "
            f"stderr={result.stderr[:1000]}"
        )
    return result.stdout.strip()


def _require_formal_authority(repo: PostgresRepository) -> TargetSpec:
    summary = repo.stage0_run_summary(STAGE0_RUN_ID)
    run = summary["run"]
    task = summary["task"]
    if (
        run["stage0_run_id"] != STAGE0_RUN_ID
        or run["task_id"] != STAGE0_TASK_ID
        or run["target_snapshot_id"] != TARGET_SNAPSHOT_ID
        or run["mode"] != Stage0RunMode.FORMAL.value
        or run["state"] != Stage0RunState.FINALIZED.value
        or task["stage0_authority"] != "formal"
        or task["project_mode"] != ProjectMode.DEGRADED_MANUAL_INTAKE.value
        or task["automatic_release_allowed"] is not False
    ):
        raise RuntimeError("Formal Stage 0 authority differs from the frozen M1 intake")
    target = TargetSpec.model_validate(summary["target"])
    if (
        target_fingerprint(target) != TARGET_FINGERPRINT
        or target.source_baseline.commit != BASELINE_COMMIT
        or target.inference_image.registry_digest != IMAGE_DIGEST
        or target.execution_host.accelerator.device_index != 7
    ):
        raise RuntimeError("Formal Target Snapshot differs from the frozen M1 intake")
    return target


def _require_database_schema(repo: PostgresRepository) -> None:
    expected = {version for version, _ in migration_plan()}
    with repo.connection() as connection:
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    applied = {int(row["version"]) for row in rows}
    missing = sorted(expected - applied)
    if missing:
        raise RuntimeError(
            "Formal DB is missing packaged migrations "
            f"{missing}; run `hcuopt db-migrate` before M1"
        )


def _require_database_scope(repo: PostgresRepository) -> None:
    ids = formal_ids()
    with repo.connection() as connection:
        resource = connection.execute(
            "SELECT resource_id, state, fencing_token FROM resources WHERE resource_id=%s",
            ("hcu-7",),
        ).fetchone()
        tasks = connection.execute(
            "SELECT task_id FROM tasks WHERE workflow_type='manual_candidate'"
        ).fetchall()
        candidates = connection.execute("SELECT candidate_id FROM candidates").fetchall()
        hotspots = connection.execute("SELECT hotspot_id FROM hotspots").fetchall()
    if resource is None or resource["state"] != "available":
        raise RuntimeError(f"Formal resource hcu-7 is not available: {resource}")
    if {row["task_id"] for row in tasks} - {ids.task_id}:
        raise RuntimeError("Formal DB already contains another M1 Task")
    if {row["candidate_id"] for row in candidates} - {ids.candidate_id}:
        raise RuntimeError("Formal DB already contains another Candidate")
    if {row["hotspot_id"] for row in hotspots} - {ids.hotspot_id}:
        raise RuntimeError("Formal DB already contains another Hotspot")


def recover_terminal_telemetry_failure(repo: PostgresRepository) -> dict[str, Any]:
    """Authorize one audited retry for the exact terminal hy-smi infrastructure fault."""

    ids = formal_ids()
    job_id = performance_job_id()
    with repo.connection() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=%s FOR UPDATE", (ids.task_id,)
        ).fetchone()
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id=%s FOR UPDATE",
            (ids.candidate_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,)
        ).fetchone()
        jobs = connection.execute(
            "SELECT job_id, job_type, state FROM jobs WHERE task_id=%s ORDER BY created_at",
            (ids.task_id,),
        ).fetchall()
        signoff = connection.execute(
            "SELECT signoff_id FROM manual_candidate_signoffs WHERE task_id=%s",
            (ids.task_id,),
        ).fetchone()
        if task is None or candidate is None or job is None:
            raise RuntimeError("Formal M1 recovery cannot find the frozen Task/Candidate/Job")
        if (
            job["state"] == JobState.QUEUED.value
            and job["attempts"] == 3
            and job["max_attempts"] == 4
            and task["state"] == TaskState.MANUAL_PERFORMANCE.value
            and candidate["state"] == CandidateState.PERFORMANCE_RUNNING.value
        ):
            return {
                "status": "already_authorized",
                "job_id": str(job_id),
                "attempts": job["attempts"],
                "max_attempts": job["max_attempts"],
            }
        expected_jobs = {
            JobType.MANUAL_BUILD.value: JobState.SUCCEEDED.value,
            JobType.MANUAL_CORRECTNESS.value: JobState.SUCCEEDED.value,
            JobType.MANUAL_PERFORMANCE.value: JobState.FAILED.value,
        }
        observed_jobs = {row["job_type"]: row["state"] for row in jobs}
        error = job.get("last_error") or {}
        message = str(error.get("message", ""))
        if (
            task["workflow_type"] != "manual_candidate"
            or task["state"] != TaskState.REJECTED.value
            or task["automatic_release_allowed"] is not False
            or candidate["task_id"] != ids.task_id
            or candidate["state"] != CandidateState.REJECTED.value
            or candidate["verdict"] is not None
            or candidate["evidence_bundle_id"] is not None
            or signoff is not None
            or job["job_type"] != JobType.MANUAL_PERFORMANCE.value
            or job["attempts"] != 3
            or job["max_attempts"] != 3
            or job["result"] is not None
            or observed_jobs != expected_jobs
            or error.get("code") != "Nmz36RuntimeError"
            or "hy-smi --showpids" not in message
            or "free(): invalid pointer" not in message
        ):
            raise RuntimeError(
                "Formal M1 recovery is allowed only for the exact terminal hy-smi fault"
            )
        details = {
            "actor": "codex-assisted-formal-operator",
            "reason": (
                "one bounded retry after hy-smi --showpids aborted with "
                "free(): invalid pointer after 37 of 40 acquisitions completed"
            ),
            "job_id": str(job_id),
            "candidate_id": str(ids.candidate_id),
            "previous_attempts": job["attempts"],
            "previous_max_attempts": job["max_attempts"],
            "previous_error": error,
            "automatic_release_allowed": False,
        }
        connection.execute(
            """
            UPDATE jobs
            SET state='queued', max_attempts=4, available_at=now() + interval '1 second',
                claimed_by=NULL, claim_token=NULL, claimed_at=NULL, heartbeat_at=NULL,
                lease_id=NULL, resource_id=NULL, fencing_token=NULL,
                finished_at=NULL, updated_at=now()
            WHERE job_id=%s
            """,
            (job_id,),
        )
        connection.execute(
            """
            UPDATE tasks
            SET state=%s, version=version + 1, updated_at=now()
            WHERE task_id=%s
            """,
            (TaskState.MANUAL_PERFORMANCE.value, ids.task_id),
        )
        connection.execute(
            "UPDATE candidates SET state=%s, updated_at=now() WHERE candidate_id=%s",
            (CandidateState.PERFORMANCE_RUNNING.value, ids.candidate_id),
        )
        connection.execute(
            "INSERT INTO job_events (job_id, event_type, details) VALUES (%s, %s, %s)",
            (job_id, "manual_infrastructure_retry_authorized", Jsonb(details)),
        )
        connection.execute(
            "INSERT INTO task_events (task_id, event_type, details) VALUES (%s, %s, %s)",
            (
                ids.task_id,
                "manual_infrastructure_retry_authorized",
                Jsonb(details),
            ),
        )
    return {
        "status": "authorized",
        "job_id": str(job_id),
        "attempts": 3,
        "max_attempts": 4,
        "automatic_release_allowed": False,
    }


def _require_runner_idle() -> None:
    result = _run(("pgrep", "-af", "Runner.Worker"))
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"cannot inspect Runner.Worker: {result.stderr[:1000]}")
    if result.returncode == 0 and result.stdout.strip():
        raise RuntimeError(f"an external Runner.Worker is active: {result.stdout[:1000]}")


def _require_hcu_idle(target: TargetSpec) -> None:
    _require_runner_idle()
    managed = _require_command(
        (
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=io.hcuopt.managed=true",
            "--filter",
            "label=io.hcuopt.resource=hcu-7",
        )
    )
    if managed:
        raise RuntimeError(f"residual hcuopt-managed containers exist: {managed}")
    usage = _require_command(("hy-smi", "--showuse", "--showmemuse", "--device", "7"))
    use = re.search(r"HCU use \(%\):\s*([0-9.]+)", usage)
    memory = re.search(r"HCU memory use \(%\):\s*([0-9.]+)", usage)
    if use is None or memory is None:
        raise RuntimeError("hy-smi did not expose HCU 7 utilization and memory")
    if float(use.group(1)) != 0.0 or float(memory.group(1)) != 0.0:
        raise RuntimeError(f"HCU 7 is not idle: {usage[:2000]}")
    telemetry = HySmiTelemetryCollector(target, ManagedProcessRegistry()).collect()
    if telemetry["background_processes"]:
        raise RuntimeError(
            "HCU 7 has unmanaged accelerator processes: "
            f"{telemetry['background_processes']}"
        )


def _require_source_inputs(target: TargetSpec) -> None:
    baseline = Path(target.source_baseline.clean_checkout)
    if _require_command(("git", "-C", str(baseline), "rev-parse", "HEAD")) != BASELINE_COMMIT:
        raise RuntimeError("SGLang Baseline HEAD drifted")
    if _require_command(("git", "-C", str(baseline), "status", "--porcelain=v1")):
        raise RuntimeError("SGLang Baseline is not clean")
    if _require_command(("git", "-C", str(baseline), "rev-parse", "HEAD^{tree}")) != (
        BASELINE_TREE_HASH
    ):
        raise RuntimeError("SGLang Baseline tree Hash drifted")
    candidate = REVIEWED_CANDIDATE_ROOT
    if _require_command(("git", "-C", str(candidate), "rev-parse", "HEAD")) != BASELINE_COMMIT:
        raise RuntimeError("reviewed Candidate starts from another Commit")
    changed = tuple(
        item
        for item in _require_command(
            ("git", "-C", str(candidate), "diff", "--name-only", "--")
        ).splitlines()
        if item
    )
    untracked = _require_command(
        ("git", "-C", str(candidate), "ls-files", "--others", "--exclude-standard")
    )
    if changed != (ALLOCATOR_RELATIVE_PATH,) or untracked:
        raise RuntimeError(
            "reviewed Candidate must contain only the allocator replacement; "
            f"changed={changed}, untracked={untracked!r}"
        )
    replacement = candidate / ALLOCATOR_RELATIVE_PATH
    if _sha256(replacement) != CANDIDATE_FILE_HASH:
        raise RuntimeError("reviewed allocator replacement Hash drifted")
    if canonical_source_hash(candidate) != CANDIDATE_SOURCE_HASH:
        raise RuntimeError("reviewed Candidate Source Hash drifted")


def _require_image(target: TargetSpec) -> None:
    image_id = _require_command(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            target.inference_image.immutable_reference,
        )
    )
    if image_id != target.inference_image.image_id:
        raise RuntimeError(f"locked image ID differs on nmz36: {image_id}")


def _build_registries(target: TargetSpec) -> dict[str, AdapterRegistry]:
    source_root = _source_root()
    source_manager = GitSourceManager(PROFILE)
    source_packages = CandidateSourcePackageStore(
        TRUSTED_SOURCE_ROOT,
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={ALLOCATOR_REPLACEMENT_POINT: ALLOCATOR_MOUNT_TARGET},
    )
    artifact_store = LocalArtifactStore(FORMAL_ROOT / "artifacts", PROFILE)
    source_registry = AdapterRegistry(
        profile=PROFILE,
        candidate_builder=ManualOverlayCandidateBuilder(
            source_manager,
            source_packages,
            artifact_store,
            LocalBuildCache(FORMAL_ROOT / "build-cache"),
            profile=PROFILE,
        ),
        source_manager=source_manager,
        artifact_store=artifact_store,
    )
    protocol = load_registered_m1_protocol()
    reader = HashedEvidenceReader(ALLOWED_EVIDENCE_ROOT)
    cleaner = ContainerResourceCleaner(target, profile=PROFILE)
    correctness_registry = build_m1_correctness_registry(
        profile=PROFILE,
        protocol=protocol,
        reader=reader,
        producer=Nmz36M1AllocatorCorrectnessEvidenceProducer(
            target=target,
            source_root=source_root,
            protocol=protocol,
            cleaner=cleaner,
        ),
        evidence_root=EVIDENCE_ROOT,
    )
    process_registry = ManagedProcessRegistry()
    harness_provenance = AdapterProvenance(
        profile=PROFILE,
        capability="measurement_harness",
        adapter_name="M1TrustedMeasurementHarness",
        adapter_version="1",
        implementation_kind="real",
    )
    measurement_registry = build_m1_measurement_registry(
        profile=PROFILE,
        evidence_root=ALLOWED_EVIDENCE_ROOT,
        workload_factory=Nmz36M1AllocatorWorkloadFactory(
            target=target,
            source_root=source_root,
            harness_provenance=harness_provenance,
            registry=process_registry,
        ),
        telemetry=HySmiTelemetryCollector(target, process_registry),
        lifecycle_recorder=DockerProcessLifecycleRecorder(),
        cleaner=cleaner,
        device_timer_factory=Nmz36M1DeviceTimerFactory(
            target=target,
            source_root=source_root,
            registry=process_registry,
        ),
    )
    adjudication_registry = build_m1_adjudication_registry(
        profile=PROFILE,
        protocol=protocol,
        reader=reader,
        evidence_root=EVIDENCE_ROOT,
    )
    compose_nmz36_m1_registry(
        source_registry,
        correctness_registry,
        measurement_registry,
        adjudication_registry,
    )
    return {
        "build": source_registry,
        "correctness": correctness_registry,
        "performance": measurement_registry,
        "adjudication": adjudication_registry,
    }


def preflight(repo: PostgresRepository) -> tuple[TargetSpec, dict[str, AdapterRegistry]]:
    if os.name != "posix":
        raise RuntimeError("Formal M1 execution requires the nmz36 POSIX host")
    _require_database_schema(repo)
    target = _require_formal_authority(repo)
    _require_database_scope(repo)
    _require_source_inputs(target)
    _require_image(target)
    _require_hcu_idle(target)
    if _sha256(PROFILER_TRACE) != PROFILER_TRACE_HASH:
        raise RuntimeError("frozen profiler trace Hash drifted")
    if not PROFILER_REPORT.is_file():
        raise RuntimeError("derived profiler report is missing")
    registries = _build_registries(target)
    return target, registries


def _prepare_baseline(
    repo: PostgresRepository,
    target: TargetSpec,
) -> SourceSnapshot:
    ids = formal_ids()
    manager = GitSourceManager(PROFILE)
    snapshot = manager.prepare_baseline(target, FORMAL_ROOT / "baseline-intake")
    snapshot = snapshot.model_copy(update={"snapshot_id": ids.baseline_snapshot_id})
    if (
        snapshot.commit != BASELINE_COMMIT
        or snapshot.tree_hash != BASELINE_TREE_HASH
        or snapshot.source_hash != BASELINE_SOURCE_HASH
        or not snapshot.clean
    ):
        raise RuntimeError("real Baseline SourceSnapshot differs from the frozen intake")
    row = repo.record_source_snapshot(
        STAGE0_TASK_ID,
        snapshot,
        [manager.provenance.model_dump(mode="json")],
        False,
        f"stage0:{STAGE0_RUN_ID}:m1-baseline-source:v1",
    )
    if row["snapshot_id"] != ids.baseline_snapshot_id:
        raise RuntimeError("persisted Baseline SourceSnapshot ID differs")
    return snapshot


def _publish_document(kind: str, value: Any) -> tuple[str, str]:
    digest = _canonical_hash(value)
    destination = (
        EVIDENCE_ROOT / "m1-intake" / kind / f"sha256-{digest.removeprefix('sha256:')}.json"
    )
    published = write_evidence(destination, value)
    if published.sha256 != digest:
        raise RuntimeError(f"published {kind} evidence changed its canonical Hash")
    return published.uri, published.sha256


def _publish_correctness_spec(ids: FormalIds) -> tuple[str, str]:
    spec = build_m1_allocator_hotspot_spec(str(ids.hotspot_id))
    expected = m1_hotspot_spec_sha256(spec)
    uri, digest = _publish_document("correctness-spec", spec)
    if digest != expected:
        raise RuntimeError("published correctness specification changed its Hash")
    return uri, digest


def _publish_candidate_package(ids: FormalIds) -> None:
    replacement = REVIEWED_CANDIDATE_ROOT / ALLOCATOR_RELATIVE_PATH
    manifest = CandidateSourcePackageManifest(
        candidate_id=ids.candidate_id,
        hotspot_id=ids.hotspot_id,
        baseline_source_hash=BASELINE_SOURCE_HASH,
        candidate_source_hash=CANDIDATE_SOURCE_HASH,
        replacement_point=ALLOCATOR_REPLACEMENT_POINT,
        candidate_kind=ManualCandidateKind.BUSINESS,
        overlay_mount_target=ALLOCATOR_MOUNT_TARGET,
        files=[
            CandidateOverlayFile(
                path=ALLOCATOR_RELATIVE_PATH,
                content_hash=CANDIDATE_FILE_HASH,
            )
        ],
        profiler_evidence_uri=PROFILER_TRACE.resolve(strict=True).as_uri(),
        profiler_evidence_hash=PROFILER_TRACE_HASH,
        reviewed_by="codex-assisted-project-intake",
        reviewed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    digest = CANDIDATE_SOURCE_HASH.removeprefix("sha256:")
    destination = TRUSTED_SOURCE_ROOT / "sha256" / digest[:2] / digest[2:]
    store = CandidateSourcePackageStore(
        TRUSTED_SOURCE_ROOT,
        profile=PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={ALLOCATOR_REPLACEMENT_POINT: ALLOCATOR_MOUNT_TARGET},
    )
    if destination.exists():
        store.load(
            candidate_id=ids.candidate_id,
            hotspot_id=ids.hotspot_id,
            baseline_source_hash=BASELINE_SOURCE_HASH,
            candidate_source_hash=CANDIDATE_SOURCE_HASH,
            replacement_point=ALLOCATOR_REPLACEMENT_POINT,
            candidate_kind=ManualCandidateKind.BUSINESS,
            profiler_evidence_uri=PROFILER_TRACE.resolve(strict=True).as_uri(),
            profiler_evidence_hash=PROFILER_TRACE_HASH,
        )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".candidate-source-", dir=destination.parent))
    try:
        source = staging / "files" / ALLOCATOR_RELATIVE_PATH
        source.parent.mkdir(parents=True)
        shutil.copyfile(replacement, source)
        manifest_path = staging / "manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        source.chmod(0o444)
        manifest_path.chmod(0o444)
        os.replace(staging, destination)
        staging = Path()
        for directory in sorted(
            (item for item in destination.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        destination.chmod(0o555)
    finally:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging)
    store.load(
        candidate_id=ids.candidate_id,
        hotspot_id=ids.hotspot_id,
        baseline_source_hash=BASELINE_SOURCE_HASH,
        candidate_source_hash=CANDIDATE_SOURCE_HASH,
        replacement_point=ALLOCATOR_REPLACEMENT_POINT,
        candidate_kind=ManualCandidateKind.BUSINESS,
        profiler_evidence_uri=PROFILER_TRACE.resolve(strict=True).as_uri(),
        profiler_evidence_hash=PROFILER_TRACE_HASH,
    )


def _api_request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200, 201),
) -> dict[str, Any]:
    response = client.request(method, path, json=payload)
    if response.status_code not in expected:
        raise RuntimeError(
            f"control-plane request failed: {method} {path} -> "
            f"{response.status_code}: {response.text[:3000]}"
        )
    return response.json()


def _prepare_formal_intake(
    client: httpx.Client,
    baseline: SourceSnapshot,
    target: TargetSpec,
) -> FormalIds:
    ids = formal_ids()
    workload_uri, workload_hash = _publish_document("workload", workload_document())
    configuration_uri, configuration_hash = _publish_document(
        "configuration", configuration_document(target)
    )
    task_request = ManualCandidateTaskCreate(
        name="M1 allocator free manual startup-overlay Candidate",
        stage0_run_id=STAGE0_RUN_ID,
        adapter_profile=PROFILE,
        baseline_source_snapshot_id=baseline.snapshot_id,
        workload_id=WORKLOAD_ID,
        workload_hash=workload_hash,
        configuration_hash=configuration_hash,
        idempotency_key=TASK_KEY,
        budget={"max_wall_seconds": 7200, "max_samples": 400},
    )
    task = _api_request(
        client,
        "POST",
        "/v1/manual-candidate/tasks",
        payload=task_request.model_dump(mode="json"),
    )
    if UUID(task["task_id"]) != ids.task_id or task["workload_id"] != WORKLOAD_ID:
        raise RuntimeError("Formal M1 Task identity differs from the frozen intake")
    summary = _api_request(
        client,
        "GET",
        f"/v1/manual-candidate/tasks/{ids.task_id}/summary",
    )
    if UUID(summary["baseline"]["baseline_epoch_id"]) != ids.baseline_epoch_id:
        raise RuntimeError("Formal Baseline Epoch identity differs")

    correctness_uri, correctness_hash = _publish_correctness_spec(ids)
    profiler_provenance = AdapterProvenance(
        profile=PROFILE,
        capability="profiler",
        adapter_name="torch.profiler+profile-llm-torch",
        adapter_version="2026-08-25",
        implementation_kind="real",
    )
    hotspot_request = ManualHotspotIntakeCreate(
        baseline_epoch_id=ids.baseline_epoch_id,
        symbol="allocator.py:522 PagedTokenToKVPoolAllocator.free / Memcpy DtoH",
        operation_name="torch.unique page reclamation in allocator free",
        shape=[4090],
        dtype="int64",
        meta={
            "stage": "prefill",
            "page_size": 64,
            "concurrency": 1,
            "radix_cache": False,
            "gpu_time_share_label": "derived_hotspot_selection_evidence",
            "workload_uri": workload_uri,
            "workload_hash": workload_hash,
            "configuration_uri": configuration_uri,
            "configuration_hash": configuration_hash,
            "derived_report_uri": PROFILER_REPORT.resolve(strict=True).as_uri(),
            "formal_performance_conclusion": None,
        },
        implementation_location=(
            "python/sglang/srt/mem_cache/allocator.py:"
            "PagedTokenToKVPoolAllocator.free"
        ),
        replacement_point=ALLOCATOR_REPLACEMENT_POINT,
        call_path=[
            "SGLang prefill request",
            "PagedTokenToKVPoolAllocator.free",
            "torch.unique",
            "aten::_unique2",
            "Memcpy DtoH",
        ],
        profiler_raw_output_uri=PROFILER_TRACE.resolve(strict=True).as_uri(),
        profiler_raw_output_hash=PROFILER_TRACE_HASH,
        correctness_spec_uri=correctness_uri,
        correctness_spec_hash=correctness_hash,
        share_ratio=0.089,
        opportunity_score=0.089,
        upstream_dedup_status="no_match",
        upstream_reference=(
            "https://github.com/sgl-project/sglang/blob/main/"
            "python/sglang/srt/mem_cache/allocator.py#L502-L568"
        ),
        patchability="patchable",
        selection_reason=(
            "The frozen trace attributes about 8.9% of GPU time to allocator free / "
            "Memcpy DtoH, with 99% of that path attributed to free(). The one-line "
            "startup Overlay removes the sorting requirement for the frozen "
            "page-contiguous workload. Dynamic output may remain, so faster, slower, "
            "and inconclusive are all accepted formal outcomes."
        ),
        candidate_kind=ManualCandidateKind.BUSINESS,
        actor="codex-assisted-manual-intake",
        adapter_provenance=[profiler_provenance],
        synthetic=False,
        idempotency_key=HOTSPOT_KEY,
    )
    hotspot = _api_request(
        client,
        "POST",
        f"/v1/manual-candidate/tasks/{ids.task_id}/hotspots",
        payload=hotspot_request.model_dump(mode="json"),
    )
    if UUID(hotspot["hotspot_id"]) != ids.hotspot_id:
        raise RuntimeError("Formal Hotspot identity differs from the frozen intake")

    _publish_candidate_package(ids)
    candidate_request = ManualCandidateCreate(
        hotspot_id=ids.hotspot_id,
        baseline_epoch_id=ids.baseline_epoch_id,
        source_hash=CANDIDATE_SOURCE_HASH,
        optimization_intent=(
            "Replace torch.unique with torch.unique_consecutive for the frozen "
            "page-contiguous allocator free workload"
        ),
        replacement_point=ALLOCATOR_REPLACEMENT_POINT,
        track="triton",
        release_mode="overlay",
        candidate_kind=ManualCandidateKind.BUSINESS,
        idempotency_key=CANDIDATE_KEY,
    )
    candidate = _api_request(
        client,
        "POST",
        f"/v1/manual-candidate/tasks/{ids.task_id}/candidates",
        payload=candidate_request.model_dump(mode="json"),
    )
    if UUID(candidate["candidate_id"]) != ids.candidate_id:
        raise RuntimeError("Formal Candidate identity differs from the frozen intake")
    return ids


def _start_api(api_url: str, database_url: str) -> tuple[subprocess.Popen[str], Any]:
    parsed = urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Formal M1 API must bind only to loopback HTTP")
    port = parsed.port or 80
    with socket.socket() as probe:
        if probe.connect_ex((parsed.hostname or "127.0.0.1", port)) == 0:
            raise RuntimeError(f"refusing to reuse an occupied API port: {port}")
    FORMAL_ROOT.mkdir(parents=True, exist_ok=True)
    log = (FORMAL_ROOT / "control-plane.log").open("a", encoding="utf-8")
    environment = {
        **os.environ,
        "HCUOPT_DATABASE_URL": database_url,
        "HCUOPT_AUTO_MIGRATE": "false",
        "PYTHONPATH": str(_source_root() / "src"),
    }
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "uvicorn",
            "hcuopt.deployment.nmz36_m1_formal:app",
            "--host",
            parsed.hostname or "127.0.0.1",
            "--port",
            str(port),
        ),
        cwd=_source_root(),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        with httpx.Client(base_url=api_url, timeout=5.0) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "Formal control plane exited early; "
                        f"log={FORMAL_ROOT / 'control-plane.log'}"
                    )
                try:
                    if client.get("/healthz").status_code == 200:
                        return process, log
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
        raise RuntimeError("Formal control plane did not become ready")
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        log.close()
        raise


def _stop_api(process: subprocess.Popen[str], log: Any) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    log.close()


def _summary(client: httpx.Client, task_id: UUID) -> dict[str, Any]:
    return _api_request(
        client,
        "GET",
        f"/v1/manual-candidate/tasks/{task_id}/summary",
    )


def _run_job_stage(
    *,
    client: httpx.Client,
    ids: FormalIds,
    job_type: JobType,
    worker_type: WorkerType,
    registry: AdapterRegistry,
    output_dir: Path,
    api_url: str,
    resource_id: str | None = None,
) -> dict[str, Any]:
    before = _summary(client, ids.task_id)
    previous = [job for job in before["jobs"] if job["job_type"] == job_type.value]
    if previous and previous[-1]["state"] == JobState.SUCCEEDED.value:
        return before
    if previous and previous[-1]["state"] in {
        JobState.RUNNING.value,
        JobState.CANCELLED.value,
        JobState.FAILED.value,
    }:
        raise RuntimeError(
            f"cannot continue {job_type.value} from state {previous[-1]['state']}"
        )
    capabilities: dict[str, Any] = {}
    if resource_id is not None:
        capabilities["resource_id"] = resource_id
    worker = Worker(
        f"nmz36-{job_type.value}-{ids.candidate_id.hex[:12]}",
        worker_type,
        api_url,
        capabilities=capabilities,
        heartbeat_seconds=10.0,
        adapters=registry,
        output_dir=output_dir,
    )
    if not worker.run_once():
        failed = _summary(client, ids.task_id)
        jobs = [job for job in failed["jobs"] if job["job_type"] == job_type.value]
        detail = jobs[-1] if jobs else {"state": "missing"}
        raise RuntimeError(f"Formal {job_type.value} did not succeed: {detail}")
    after = _summary(client, ids.task_id)
    jobs = [job for job in after["jobs"] if job["job_type"] == job_type.value]
    if not jobs or jobs[-1]["state"] != JobState.SUCCEEDED.value:
        raise RuntimeError(f"Formal {job_type.value} has no succeeded durable Job")
    return after


def _final_record(summary: dict[str, Any]) -> dict[str, Any]:
    candidate = summary["candidate"]
    jobs = [
        {
            "job_id": job["job_id"],
            "job_type": job["job_type"],
            "state": job["state"],
            "attempts": job["attempts"],
            "lease_id": job.get("lease_id"),
            "resource_id": job.get("resource_id"),
            "fencing_token": job.get("fencing_token"),
        }
        for job in summary["jobs"]
    ]
    evidence = next(
        (
            event["details"]
            for event in reversed(summary["events"])
            if event["event_type"] == "manual_candidate_adjudicated"
        ),
        None,
    )
    return {
        "schema_version": "hcuopt-m1-formal-run-summary-v1",
        "task_id": summary["task"]["task_id"],
        "task_state": summary["task"]["state"],
        "candidate_id": candidate["candidate_id"],
        "candidate_state": candidate["state"],
        "verdict": candidate.get("verdict"),
        "evidence_bundle_id": candidate.get("evidence_bundle_id"),
        "automatic_release_allowed": False,
        "signoff": None,
        "jobs": jobs,
        "adjudication": evidence,
    }


def run_formal(database_url: str, api_url: str) -> dict[str, Any]:
    repo = PostgresRepository(database_url)
    target, registries = preflight(repo)
    FORMAL_ROOT.mkdir(parents=True, exist_ok=True)
    baseline = _prepare_baseline(repo, target)
    process, log = _start_api(api_url, database_url)
    try:
        with httpx.Client(base_url=api_url, timeout=60.0) as client:
            ids = _prepare_formal_intake(client, baseline, target)
            _run_job_stage(
                client=client,
                ids=ids,
                job_type=JobType.MANUAL_BUILD,
                worker_type=WorkerType.BUILD,
                registry=registries["build"],
                output_dir=BUILD_ROOT,
                api_url=api_url,
            )
            _require_database_scope(repo)
            _require_hcu_idle(target)
            _run_job_stage(
                client=client,
                ids=ids,
                job_type=JobType.MANUAL_CORRECTNESS,
                worker_type=WorkerType.GPU,
                registry=registries["correctness"],
                output_dir=EVIDENCE_ROOT / "m1-workers" / "correctness",
                api_url=api_url,
                resource_id="hcu-7",
            )
            current = _summary(client, ids.task_id)
            if current["task"]["state"] == TaskState.REJECTED.value:
                final = _final_record(current)
            else:
                _require_database_scope(repo)
                _require_hcu_idle(target)
                _run_job_stage(
                    client=client,
                    ids=ids,
                    job_type=JobType.MANUAL_PERFORMANCE,
                    worker_type=WorkerType.GPU,
                    registry=registries["performance"],
                    output_dir=EVIDENCE_ROOT / "m1-workers" / "performance",
                    api_url=api_url,
                    resource_id="hcu-7",
                )
                _require_database_scope(repo)
                _require_hcu_idle(target)
                current = _run_job_stage(
                    client=client,
                    ids=ids,
                    job_type=JobType.MANUAL_ADJUDICATE,
                    worker_type=WorkerType.EVALUATION,
                    registry=registries["adjudication"],
                    output_dir=EVIDENCE_ROOT / "m1-workers" / "adjudication",
                    api_url=api_url,
                )
                final = _final_record(current)
            if final["task_state"] not in {
                TaskState.AWAITING_SIGNOFF.value,
                TaskState.REJECTED.value,
            }:
                raise RuntimeError(f"Formal M1 stopped in unexpected state: {final}")
            _require_database_scope(repo)
            _require_hcu_idle(target)
            baseline_path = Path(target.source_baseline.clean_checkout)
            if _require_command(
                ("git", "-C", str(baseline_path), "status", "--porcelain=v1")
            ):
                raise RuntimeError("SGLang Baseline is dirty after Formal M1")
            published = write_evidence(FORMAL_ROOT / "formal-summary.json", final)
            return {
                **final,
                "summary_uri": published.uri,
                "summary_hash": published.sha256,
            }
    finally:
        _stop_api(process, log)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hcuopt-nmz36-m1-formal")
    parser.add_argument("command", choices=("preflight", "recover-telemetry", "run"))
    parser.add_argument(
        "--database-url",
        default=os.getenv("HCUOPT_DATABASE_URL", DATABASE_URL_DEFAULT),
    )
    parser.add_argument("--api-url", default=API_URL_DEFAULT)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    repo = PostgresRepository(args.database_url)
    if args.command == "preflight":
        target, registries = preflight(repo)
        print(
            json.dumps(
                {
                    "status": "ready",
                    "target_id": target.target_id,
                    "formal_ids": {
                        "task_id": str(formal_ids().task_id),
                        "baseline_snapshot_id": str(
                            formal_ids().baseline_snapshot_id
                        ),
                        "baseline_epoch_id": str(formal_ids().baseline_epoch_id),
                        "hotspot_id": str(formal_ids().hotspot_id),
                        "candidate_id": str(formal_ids().candidate_id),
                    },
                    "registries": {
                        name: list(registry.available())
                        for name, registry in registries.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "recover-telemetry":
        preflight(repo)
        print(
            json.dumps(
                recover_terminal_telemetry_failure(repo),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    result = run_formal(args.database_url, args.api_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["task_state"] == TaskState.AWAITING_SIGNOFF.value else 2


if __name__ == "__main__":
    raise SystemExit(main())

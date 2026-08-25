from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.execution import ContainerExecutionAdapter
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.platform_v1 import MountSpec, SourceSnapshot
from hcuopt.measurement.evidence import write_evidence
from hcuopt.runtime_probes.m1_overlay import (
    ManualCandidateOverlayRuntime,
    ManualOverlayRuntimeProfile,
)
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import (
    OverlayPhaseConfiguration,
    RuntimeProbeProfile,
)
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

PROFILE = "nmz36-m1-manual-v1"
SOURCE_MOUNT_TARGET = "/opt/hcuopt/source"
WORKLOAD_MOUNT_TARGET = "/opt/hcuopt/workload.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the M1-C fixture through build, import attestation and recovery"
    )
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--s0-runtime-profile", required=True, type=Path)
    parser.add_argument("--fixture-source", required=True, type=Path)
    parser.add_argument("--relative-source-path", required=True)
    parser.add_argument("--logical-replacement-point", required=True)
    parser.add_argument("--workload-spec", required=True, type=Path)
    parser.add_argument("--project-source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--trusted-source-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True, type=UUID)
    parser.add_argument("--hotspot-id", required=True, type=UUID)
    parser.add_argument("--hotspot-intake-hash", required=True)
    parser.add_argument("--lease-id", required=True, type=UUID)
    parser.add_argument("--resource-id", default="hcu-7")
    parser.add_argument("--fencing-token", required=True, type=int)
    parser.add_argument("--profiler-evidence-uri", required=True)
    parser.add_argument("--profiler-evidence-hash", required=True)
    parser.add_argument("--reviewed-by", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.fencing_token < 1:
        raise SystemExit("fencing-token must be positive")
    target = load_target(args.target)
    s0_profile = RuntimeProbeProfile.model_validate_json(
        args.s0_runtime_profile.read_text(encoding="utf-8")
    )
    hotpatch = s0_profile.hotpatch
    mount_target = hotpatch.overlay_mount_target
    fixture = args.fixture_source.resolve(strict=True)
    if fixture.is_symlink() or not fixture.is_file():
        raise SystemExit("fixture-source must be a regular file")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manager = GitSourceManager(PROFILE)
    baseline = manager.prepare_baseline(target, output_dir)
    replacement = fixture.read_bytes()
    candidate_hash = _candidate_source_hash(
        manager,
        baseline,
        args.candidate_id,
        args.relative_source_path,
        replacement,
        output_dir,
    )
    manifest = _publish_source_package(
        args.trusted_source_root.resolve(),
        candidate_id=args.candidate_id,
        hotspot_id=args.hotspot_id,
        baseline=baseline,
        candidate_hash=candidate_hash,
        relative_source_path=args.relative_source_path,
        replacement=replacement,
        logical_replacement_point=args.logical_replacement_point,
        mount_target=mount_target,
        profiler_evidence_uri=args.profiler_evidence_uri,
        profiler_evidence_hash=args.profiler_evidence_hash,
        reviewed_by=args.reviewed_by,
    )
    hotspot_intake_hash = args.hotspot_intake_hash
    artifact_store = LocalArtifactStore(output_dir / "artifacts", PROFILE)
    packages = CandidateSourcePackageStore(
        args.trusted_source_root,
        profile=PROFILE,
        allowed_overlay_roots=(str(Path(args.relative_source_path).parent),),
        approved_mount_targets={args.logical_replacement_point: mount_target},
    )
    builder = ManualOverlayCandidateBuilder(
        manager,
        packages,
        artifact_store,
        LocalBuildCache(output_dir / "build-cache"),
        profile=PROFILE,
    )
    build = builder.build_candidate(
        {
            "candidate_id": str(args.candidate_id),
            "hotspot_id": str(args.hotspot_id),
            "baseline_source": baseline.model_dump(mode="json"),
            "candidate_source_hash": candidate_hash,
            "replacement_point": args.logical_replacement_point,
            "candidate_kind": "fixture",
            "hotspot_intake_hash": hotspot_intake_hash,
            "hotspot": {
                "profiler_raw_output_uri": args.profiler_evidence_uri,
                "profiler_raw_output_hash": args.profiler_evidence_hash,
            },
        },
        output_dir,
    )
    if build.artifact.content_hash != _sha256(fixture):
        raise SystemExit("built M1 Artifact differs from the reviewed fixture bytes")

    project_source = args.project_source.resolve(strict=True)
    workload_spec = args.workload_spec.resolve(strict=True)
    phases = {
        name: _phase_configuration(
            getattr(hotpatch, name),
            name=name,
            project_source=project_source,
            workload_spec=workload_spec,
            evidence_dir=output_dir / "overlay-evidence" / name,
            artifact_hash=build.artifact.content_hash,
            implementation_source=(
                file_uri_to_path(build.artifact.uri)
                if name == "candidate"
                else file_uri_to_path(baseline.worktree_uri) / args.relative_source_path
            ),
        )
        for name in ("baseline", "candidate", "recovery")
    }
    runtime_profile = ManualOverlayRuntimeProfile(
        profile=PROFILE,
        target_id=target.target_id,
        target_fingerprint=target_fingerprint(target),
        activation_marker=hotpatch.activation_marker,
        replacement_points={args.logical_replacement_point: mount_target},
        baseline=phases["baseline"],
        candidate=phases["candidate"],
        recovery=phases["recovery"],
    )
    executor = ContainerExecutionAdapter(profile=PROFILE)
    cleaner = ContainerResourceCleaner(target, profile=PROFILE)
    runtime = ManualCandidateOverlayRuntime(
        manager,
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        runtime_profile,
        evidence_root=args.evidence_root,
    )
    result = runtime.verify(
        {
            "candidate_id": str(args.candidate_id),
            "hotspot_id": str(args.hotspot_id),
            "hotspot_intake_hash": hotspot_intake_hash,
            "baseline_source": baseline.model_dump(mode="json"),
            "candidate_source": build.source.model_dump(mode="json"),
            "artifact": build.artifact.model_dump(mode="json"),
            "replacement_point": args.logical_replacement_point,
            "candidate_kind": "fixture",
            "target": target.model_dump(mode="json"),
            "target_fingerprint": target_fingerprint(target),
            "budget": {"max_wall_seconds": 1800},
            "_job_context": {
                "lease_id": str(args.lease_id),
                "resource_id": args.resource_id,
                "fencing_token": args.fencing_token,
            },
        },
        output_dir,
    )
    summary = {
        "candidate_kind": "fixture",
        "business_optimization_claim": False,
        "candidate_id": str(args.candidate_id),
        "hotspot_id": str(args.hotspot_id),
        "candidate_source_hash": manifest.candidate_source_hash,
        "artifact_hash": build.artifact.content_hash,
        **result,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def _candidate_source_hash(
    manager: GitSourceManager,
    baseline: SourceSnapshot,
    candidate_id: UUID,
    relative_source_path: str,
    replacement: bytes,
    output_dir: Path,
) -> str:
    preview = manager.create_candidate(baseline, candidate_id, output_dir)
    try:
        destination = file_uri_to_path(preview.worktree_uri) / relative_source_path
        _replace_regular_file(destination, replacement)
        return canonical_source_hash(file_uri_to_path(preview.worktree_uri))
    finally:
        manager.remove_candidate(baseline, preview, output_dir)


def _publish_source_package(
    root: Path,
    *,
    candidate_id: UUID,
    hotspot_id: UUID,
    baseline: SourceSnapshot,
    candidate_hash: str,
    relative_source_path: str,
    replacement: bytes,
    logical_replacement_point: str,
    mount_target: str,
    profiler_evidence_uri: str,
    profiler_evidence_hash: str,
    reviewed_by: str,
) -> CandidateSourcePackageManifest:
    digest = candidate_hash.removeprefix("sha256:")
    package = root / "sha256" / digest[:2] / digest[2:]
    source = package / "files" / relative_source_path
    if source.exists():
        if source.read_bytes() != replacement:
            raise SystemExit("trusted Candidate package already contains different bytes")
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        _write_once(source, replacement)
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline.source_hash,
        candidate_source_hash=candidate_hash,
        replacement_point=logical_replacement_point,
        overlay_mount_target=mount_target,
        candidate_kind="fixture",
        files=[{"path": relative_source_path, "content_hash": _sha256(source)}],
        profiler_evidence_uri=profiler_evidence_uri,
        profiler_evidence_hash=profiler_evidence_hash,
        reviewed_by=reviewed_by,
        reviewed_at=datetime.now(timezone.utc),
    )
    write_evidence(package / "manifest.json", manifest)
    return manifest


def _phase_configuration(
    original: OverlayPhaseConfiguration,
    *,
    name: str,
    project_source: Path,
    workload_spec: Path,
    evidence_dir: Path,
    artifact_hash: str,
    implementation_source: Path,
) -> OverlayPhaseConfiguration:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    argv = list(original.argv)
    if "--expected-artifact-hash" in argv:
        argv[argv.index("--expected-artifact-hash") + 1] = artifact_hash
    mounts: list[MountSpec] = []
    for mount in original.mounts:
        if mount.target == SOURCE_MOUNT_TARGET:
            mounts.append(
                MountSpec(source=project_source.as_posix(), target=mount.target)
            )
        elif mount.target == WORKLOAD_MOUNT_TARGET:
            mounts.append(
                MountSpec(source=workload_spec.as_posix(), target=mount.target)
            )
        elif not mount.read_only:
            mounts.append(
                MountSpec(
                    source=evidence_dir.as_posix(),
                    target=mount.target,
                    read_only=False,
                )
            )
        else:
            mounts.append(mount)
    return original.model_copy(
        update={
            "argv": tuple(argv),
            "mounts": tuple(mounts),
            "evidence_directory_uri": evidence_dir.as_uri(),
            "implementation_source_uri": implementation_source.resolve(strict=True).as_uri(),
        }
    )


def _replace_regular_file(destination: Path, replacement: bytes) -> None:
    if destination.is_symlink() or not destination.is_file():
        raise SystemExit(f"replacement point is not a regular Baseline file: {destination}")
    mode = destination.stat().st_mode & 0o777
    descriptor, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_once(destination: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != content:
                raise SystemExit("immutable Candidate package collision") from None
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(value: Path | bytes) -> str:
    hasher = hashlib.sha256()
    if isinstance(value, bytes):
        hasher.update(value)
    else:
        with value.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())

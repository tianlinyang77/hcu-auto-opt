# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Prepare an offline BW20 pair from a published no-op artifact; never execute.

Inputs come from the trusted control plane. Provenance metadata is checked for
consistency, not treated as a cryptographic proof or an execution grant.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from hcuopt.adapters.bw20_execution import (
    RESOURCE_ID,
    RUN_PARENT,
    SMOKE_ARGV,
    BW20SmokeExecutionAdapter,
    smoke_mounts,
)
from hcuopt.adapters.execution import LocalCommandRunner
from hcuopt.adapters.noop_builder import NOOP_BUILD_RECIPE_VERSION
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    ExecutionRequest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.deployment.bw20_smoke_preflight import MODEL, MODEL_HASHES
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.evaluation.sglang_smoke import (
    SGLangWorkloadSpec,
    SmokeVariantSpec,
    build_execution_request,
    load_workload_spec,
    validate_variant_pair,
)
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint


@dataclass(frozen=True)
class PreparedPair:
    directory: Path
    plan_sha256: str


def regular_file(path: Path) -> Path:
    absolute = path.absolute()
    if absolute != absolute.resolve(strict=True) or not absolute.is_file():
        raise ValueError(f"expected regular file without redirected path: {path}")
    return absolute


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")


def _copy_verified(source: Path, destination: Path, expected: str) -> None:
    source = regular_file(source)
    if file_hash(source) != expected:
        raise ValueError(f"input hash mismatch: {source.name}")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    if file_hash(destination) != expected or file_hash(source) != expected:
        raise ValueError(f"copy hash mismatch: {source.name}")
    destination.chmod(0o444)


def _validate_artifact(artifact: ArtifactManifest, snapshot: SourceSnapshot,
                       target: TargetSpec) -> None:
    if (artifact.synthetic or artifact.kind != "noop-source-archive"
            or artifact.candidate_id is None or artifact.source_snapshot_id != snapshot.snapshot_id
            or artifact.metadata.get("source_hash") != snapshot.source_hash
            or artifact.build_recipe.get("name") != NOOP_BUILD_RECIPE_VERSION
            or artifact.metadata.get("immutable") is not True):
        raise ValueError("published no-op artifact/source identity is incomplete or mismatched")
    if (snapshot.kind != "candidate" or not snapshot.clean
            or snapshot.commit != target.source_baseline.commit
            or snapshot.repository != target.source_baseline.repository):
        raise ValueError("candidate snapshot does not match target source baseline")
    provenance = [AdapterProvenance.model_validate(item)
                  for item in artifact.metadata.get("adapter_provenance", [])]
    if (any(item.implementation_kind != "real" for item in provenance)
            or not {"builder", "artifact_store"}.issubset({p.capability for p in provenance})):
        raise ValueError("artifact requires real builder and artifact-store provenance")


def prepare_pair(*, target: TargetSpec, workload: SGLangWorkloadSpec,
                 artifact: ArtifactManifest, snapshot: SourceSnapshot,
                 runner_path: Path, expected_runner_sha256: str, destination: Path,
                 fencing_token: int, run_id: UUID | None = None,
                 artifact_transport_path: Path | None = None,
                 request_ids: tuple[UUID, UUID] | None = None) -> PreparedPair:
    """Local file staging only. A new lease/window review is still required remotely."""
    run_root = f"{RUN_PARENT}/{run_id or uuid4()}"
    policy = BW20SmokeExecutionAdapter(LocalCommandRunner(), run_root=run_root)
    _validate_artifact(artifact, snapshot, target)
    if (workload.workload_id != "bw20-sglang-smoke-v1" or workload.model_path != MODEL
            or workload.tensor_parallel_size != 1 or workload.max_new_tokens != 8):
        raise ValueError("workload is outside the bounded BW20 smoke scope")
    runner_path = regular_file(runner_path)
    # A transport copy never rewrites the original published manifest URI.
    artifact_path = regular_file(artifact_transport_path or file_uri_to_path(artifact.uri))
    if file_hash(artifact_path) != artifact.content_hash:
        raise ValueError("artifact hash mismatch")
    if file_hash(runner_path) != expected_runner_sha256:
        raise ValueError("runner hash mismatch")

    # Relocation is explicit: keep the original manifest as well as a staged view.
    staged = artifact.model_copy(update={
        "uri": f"file://{run_root}/input/noop-source.tar",
        "metadata": {**artifact.metadata, "staged_from_uri": artifact.uri},
    })
    variants = [SmokeVariantSpec(
        name=name, runner_host_path=f"{run_root}/input/sglang_smoke_runner.py",
        spec_host_path=f"{run_root}/input/spec.json", evidence_host_dir=f"{run_root}/{name}",
        artifact_id=artifact.artifact_id if name == "noop" else None,
        mounts=smoke_mounts(run_root, "noop")[-1:] if name == "noop" else [],
    ) for name in ("baseline", "noop")]
    validate_variant_pair(*variants, staged)
    requests = []
    if request_ids is not None and request_ids[0] == request_ids[1]:
        raise ValueError("baseline and no-op request IDs must differ")
    for index, variant in enumerate(variants):
        original = build_execution_request(
            target, workload, variant, staged if variant.name == "noop" else None,
            resource_id=RESOURCE_ID, fencing_token=fencing_token,
            **({"request_id": request_ids[index]} if request_ids is not None else {}),
        )
        # Explicit conversion, never a relaxation of the executor whitelist.
        request = ExecutionRequest.model_validate({
            **original.model_dump(), "argv": list(SMOKE_ARGV), "environment": {},
            "mounts": smoke_mounts(run_root, variant.name),
        })
        policy.validate_scope(request, target)
        requests.append(request)

    destination = destination.absolute()
    parent = destination.parent
    if parent != parent.resolve(strict=True):
        raise ValueError("destination parent must not be a redirected path")
    destination.mkdir(exist_ok=False)
    inputs = destination / "input"
    inputs.mkdir()
    # Incomplete failures are retained for diagnosis; plan.json is written last.
    for name in ("baseline", "noop"):
        (destination / name).mkdir()
    _copy_verified(runner_path, inputs / "sglang_smoke_runner.py", expected_runner_sha256)
    _copy_verified(artifact_path, inputs / "noop-source.tar", artifact.content_hash)
    _write_json(inputs / "spec.json", workload.model_dump(mode="json"))
    (inputs / "spec.json").chmod(0o444)
    payload = {
        "schema": "bw20-pair-preparation-v1", "remote_run_root": run_root,
        "target_fingerprint": target_fingerprint(target),
        "target": target.model_dump(mode="json"),
        "source_snapshot": snapshot.model_dump(mode="json"),
        "original_artifact": artifact.model_dump(mode="json"),
        "artifact_transport_path": str(artifact_path),
        "staged_artifact": staged.model_dump(mode="json"),
        "requests": [r.model_dump(mode="json") for r in requests],
        "input_sha256": {p.name: file_hash(p) for p in sorted(inputs.iterdir())},
        "expected_model_sha256": MODEL_HASHES,
        "remote_files_verified": False, "resource_window_verified": False,
        "execution_performed": False, "framework_pair_accepted": False,
        "candidate_activation": "archive_mount_only",
        "automatic_release_allowed": False,
    }
    _write_json(destination / "plan.json", payload)
    return PreparedPair(destination, file_hash(destination / "plan.json"))


def verify_prepared_pair(directory: Path, *, expected_plan_sha256: str) -> dict[str, object]:
    """Detect bundle mutation against a digest retained outside the bundle.

    Does NOT inspect the remote model, reserve a resource or authorize execution.
    """
    plan_path = regular_file(directory / "plan.json")
    if file_hash(plan_path) != expected_plan_sha256:
        raise ValueError("plan hash mismatch")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan["schema"] != "bw20-pair-preparation-v1":
        raise ValueError("unsupported preparation schema")
    inputs = directory / "input"
    expected_names = {"spec.json", "noop-source.tar", "sglang_smoke_runner.py"}
    if (set(plan["input_sha256"]) != expected_names
            or {p.name for p in inputs.iterdir()} != expected_names):
        raise ValueError("unexpected input inventory")
    for name, digest in plan["input_sha256"].items():
        if file_hash(regular_file(inputs / name)) != digest:
            raise ValueError(f"staged hash mismatch: {name}")
    for name in ("baseline", "noop"):
        path = (directory / name).absolute()
        if path != path.resolve(strict=True) or not path.is_dir() or any(path.iterdir()):
            raise ValueError(f"output must be a fresh non-redirected directory: {name}")
    return {"local_bundle_verified": True, "plan_sha256": expected_plan_sha256,
            "remote_files_verified": False, "execution_performed": False,
            "automatic_release_allowed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare local inputs; no SSH/Docker")
    verify = commands.add_parser("verify", help="verify against an externally retained digest")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--plan-sha256", required=True)
    for name in ("target", "workload", "artifact", "snapshot", "runner", "output"):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    prepare.add_argument("--runner-sha256", required=True)
    prepare.add_argument("--fencing-token", type=int, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify_prepared_pair(
            args.directory, expected_plan_sha256=args.plan_sha256), indent=2))
        return
    prepared = prepare_pair(
        target=load_target(args.target), workload=load_workload_spec(args.workload),
        artifact=ArtifactManifest.model_validate_json(args.artifact.read_bytes()),
        snapshot=SourceSnapshot.model_validate_json(args.snapshot.read_bytes()),
        runner_path=args.runner, expected_runner_sha256=args.runner_sha256,
        destination=args.output, fencing_token=args.fencing_token,
    )
    print(json.dumps({"directory": str(prepared.directory), **verify_prepared_pair(
        prepared.directory, expected_plan_sha256=prepared.plan_sha256)}, indent=2))


if __name__ == "__main__":
    main()

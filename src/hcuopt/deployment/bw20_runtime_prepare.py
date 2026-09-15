# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Prepare an actual marker-only C candidate and frozen config without running HCU.

No default profile registration, blocker resolution or performance claim.  The
candidate is finalized as a deterministic local commit before it can be used by
the runtime overlay probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.contracts.platform_v1 import ArtifactManifest
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

RELATIVE_MODULE = "python/sglang/srt/layers/rotary_embedding/base.py"
RUNTIME_MODULE = (
    "/usr/local/lib/python3.10/dist-packages/sglang/srt/layers/rotary_embedding/base.py"
)
IMPORT_MARKER = b"""

# HCUOPT Stage0 import observation only; no numerical implementation change.
def _hcuopt_stage0_import_observation():
    import hashlib as _h
    import json as _j
    import os as _o
    import pathlib as _p
    import tempfile as _t
    _dest = _o.environ.get("HCUOPT_OVERLAY_IMPORT_MARKER")
    if not _dest:
        return
    _path = _p.Path(_dest)
    if _path != _p.Path("/evidence/candidate-import.json"):
        raise RuntimeError("unexpected Stage0 import observation destination")
    _module = _p.Path(__file__).resolve(strict=True)
    _value = dict(protocol_version="hcuopt-sglang-overlay-import-v1",
                  process_id=_o.getpid(), process_group_id=_o.getpgrp(),
                  module_file=str(_module),
                  module_sha256="sha256:" + _h.sha256(_module.read_bytes()).hexdigest())
    _fd, _name = _t.mkstemp(dir=str(_path.parent), prefix=".import-")
    try:
        with _o.fdopen(_fd, "w") as _f:
            _j.dump(_value, _f, sort_keys=True)
            _f.flush()
            _o.fsync(_f.fileno())
        _o.replace(_name, _path)
    finally:
        _p.Path(_name).unlink(missing_ok=True)
_hcuopt_stage0_import_observation()
del _hcuopt_stage0_import_observation
"""


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def mount(source, target, read_only=True):
    return dict(
        source=OverlayCapabilityProbe._mount_source(source), target=target, read_only=read_only
    )


def build_configuration(*, target, baseline, candidate, artifact, controller, root, model, hyhal):
    """Use real snapshots/artifact; all output paths are rebased again for each Job."""
    base = file_uri_to_path(baseline.worktree_uri) / RELATIVE_MODULE
    base_hash = digest(base)
    common = [
        mount(controller / "src", "/opt/hcuopt/src"),
        mount(root / "workload.json", "/opt/hcuopt/workload.json"),
        mount(model, model.as_posix()),
        mount(hyhal, "/opt/hyhal"),
    ]
    env = dict(
        PYTHONPATH="/opt/hcuopt/src",
        PYTHONDONTWRITEBYTECODE="1",
        HOME="/tmp",
        OMP_NUM_THREADS="8",
        HIP_VISIBLE_DEVICES="0",
        ROCR_VISIBLE_DEVICES="0",
        HSA_VISIBLE_DEVICES="0",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    entry = ["python", "-m", "hcuopt.deployment.bw20_capability_entrypoint"]

    def phase(name):
        is_candidate = name == "candidate"
        impl_hash = artifact.content_hash if is_candidate else base_hash
        output = root / "outputs" / name
        output.mkdir(parents=True)
        # Recovery must prove a return to the same baseline namespace.  Only
        # the candidate receives a distinct cache; otherwise D correctly
        # rejects the three-phase overlay as non-recovering.
        cache_role = "candidate" if is_candidate else "baseline"
        cache = "/tmp/hcuopt-cache-" + cache_role
        environment = dict(
            env,
            HCUOPT_CANDIDATE_CACHE_DIR=cache,
            TRITON_CACHE_DIR=cache + "/triton",
            XDG_CACHE_HOME=cache,
        )
        argv = entry + [
            "--kind",
            "overlay",
            "--module",
            RUNTIME_MODULE,
            "--expected-hash",
            impl_hash,
            "--",
            "--phase",
            name,
            "--smoke-runner",
            "/opt/hcuopt/src/hcuopt/evaluation/sglang_smoke_runner.py",
            "--spec",
            "/opt/hcuopt/workload.json",
            "--evidence-dir",
            "/evidence",
            "--replacement-point",
            RUNTIME_MODULE,
            "--activation-marker",
            "bw20-stage0-import-v1",
            "--formal-evidence",
        ]
        if is_candidate:
            environment["HCUOPT_OVERLAY_IMPORT_MARKER"] = "/evidence/candidate-import.json"
            argv += [
                "--candidate-import-marker",
                "/evidence/candidate-import.json",
                "--expected-artifact-hash",
                artifact.content_hash,
            ]
        return dict(
            argv=argv,
            working_directory="/tmp",
            environment=environment,
            mounts=common + [mount(output, "/evidence", False)],
            timeout_seconds=600,
            evidence_directory_uri=output.as_uri(),
            implementation_source_uri=artifact.uri if is_candidate else base.as_uri(),
        )

    profiler_output = root / "outputs/profiler"
    profiler_output.mkdir(parents=True)
    return RuntimeProbeProfile.model_validate(
        dict(
            profile=PROFILE,
            target_id=target.target_id,
            target_fingerprint=target_fingerprint(target),
            profiler=dict(
                profile_workload="prefill",
                warmup_steps=1,
                num_steps=1,
                prefill_input_len=128,
                prefill_output_len=8,
                working_directory="/tmp",
                environment=env,
                mounts=common + [mount(profiler_output, "/evidence", False)],
                tool_candidates=[
                    dict(
                        name="profile-llm-torch",
                        version_argv=entry + ["--version"],
                        profile_argv=entry
                        + [
                            "--kind",
                            "profiler",
                            "--module",
                            RUNTIME_MODULE,
                            "--expected-hash",
                            base_hash,
                            "--",
                            "--model-path",
                            str(model),
                            "--input-len",
                            "128",
                            "--output-len",
                            "8",
                            "--output",
                            "/evidence/prefill.trace.json.gz",
                        ],
                        output_format="torch_trace",
                        output_path="/evidence/prefill.trace.json.gz",
                        output_host_uri=(profiler_output / "prefill.trace.json.gz").as_uri(),
                        timeout_seconds=600,
                    )
                ],
            ),
            hotpatch=dict(
                workload_kind="sglang_python_triton",
                replacement_point=RUNTIME_MODULE,
                baseline_source=baseline,
                candidate_source=candidate,
                artifact=artifact,
                overlay_mount_target=RUNTIME_MODULE,
                activation_marker="bw20-stage0-import-v1",
                baseline=phase("baseline"),
                candidate=phase("candidate"),
                recovery=phase("recovery"),
            ),
        )
    )


def prepare(*, target, workload, controller: Path, root: Path):
    build_probe_plan(target)
    if root.exists() or root.resolve(strict=False) != root:
        raise ValueError("preparation requires a fresh nonredirected directory")
    controller = controller.resolve(strict=True)
    spec = load_workload_spec(workload).model_dump(mode="json")
    if spec["target_id"] != target.target_id:
        raise ValueError("workload target mismatch")
    model = Path(spec["model_path"])
    if model.resolve(strict=True) != model or not model.is_dir():
        raise ValueError("model directory must be present and nonredirected")
    root.mkdir(parents=True, mode=0o700)
    (root / "workload.json").write_bytes(canonical_json_bytes(spec))
    manager = GitSourceManager(profile=PROFILE)
    baseline = manager.prepare_baseline(target, root / "source-logs")
    candidate_id = uuid4()
    candidate = manager.create_candidate(baseline, candidate_id, root / "source-logs")
    candidate_path = file_uri_to_path(candidate.worktree_uri)
    implementation = candidate_path / RELATIVE_MODULE
    original = implementation.read_bytes()
    if b"_hcuopt_stage0_import_observation" in original:
        raise ValueError("baseline already contains observation marker")
    implementation.write_bytes(original + IMPORT_MARKER)
    compile(implementation.read_bytes(), str(implementation), "exec")
    reviewed_candidate = manager._snapshot(
        kind="candidate",
        repository=baseline.repository,
        path=candidate_path,
        parent_snapshot_id=baseline.snapshot_id,
    )
    candidate = manager.finalize_candidate(
        baseline,
        reviewed_candidate,
        candidate_id,
        (RELATIVE_MODULE,),
        reviewed_candidate.source_hash,
        root / "source-logs",
    )
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="python_overlay",
        uri=implementation.as_uri(),
        content_hash=digest(implementation),
        source_snapshot_id=candidate.snapshot_id,
        build_recipe=dict(
            name="stage0-import-marker-v1",
            relative_path=RELATIVE_MODULE,
            algorithm_changed=False,
            source_finalized=True,
        ),
        metadata=dict(
            source_hash=candidate.source_hash,
            adapter_provenance=[manager.provenance.model_dump(mode="json")],
        ),
    )
    artifact = LocalArtifactStore(root / "store", profile=PROFILE).publish(artifact, implementation)
    configuration = build_configuration(
        target=target,
        baseline=baseline,
        candidate=candidate,
        artifact=artifact,
        controller=controller,
        root=root,
        model=model,
        hyhal=Path("/opt/hyhal").resolve(strict=True),
    )
    OverlayCapabilityProbe._validate_sources(baseline, candidate, artifact)
    manager._assert_snapshot_unchanged(baseline, file_uri_to_path(baseline.worktree_uri))
    for name, value in (
        ("target.json", target),
        ("baseline.json", baseline),
        ("candidate.json", candidate),
        ("artifact.json", artifact),
        ("runtime-profile.json", configuration),
    ):
        path = root / name
        path.write_bytes(canonical_json_bytes(value))
        path.chmod(0o444)
    # Model files are hashed as inputs, not benchmarked. Do not silently omit weights.
    inventory = {}
    for path in sorted(model.rglob("*")):
        if path.is_symlink():
            raise ValueError("model inventory contains a redirected member")
        if path.is_file():
            inventory[str(path.relative_to(model))] = dict(
                sha256=digest(path), size=path.stat().st_size
            )
    (root / "model-inventory.json").write_bytes(canonical_json_bytes(inventory))
    report = dict(
        schema_version="bw20-runtime-preparation-v1",
        hcu_used=False,
        stage0_accepted=False,
        automatic_release_allowed=False,
        source_finalized=True,
        candidate_kind="import_observation_only_not_optimization",
        baseline_clean=True,
        profile_sha256=digest(root / "runtime-profile.json"),
        model_inventory_sha256=digest(root / "model-inventory.json"),
        controller_manifest_sha256=digest(controller / "controller-manifest.json"),
        target_fingerprint=target_fingerprint(target),
        input_hashes={
            name: digest(root / name)
            for name in (
                "target.json",
                "baseline.json",
                "candidate.json",
                "artifact.json",
                "workload.json",
            )
        },
        unresolved_blockers=[b.id for b in target.blockers if b.status == "open"],
        runtime_module_verification="required_in_container_before_workload",
        original_marker_sha256="sha256:" + hashlib.sha256(IMPORT_MARKER).hexdigest(),
    )
    (root / "preparation.json").write_bytes(canonical_json_bytes(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                target=load_target(args.target),
                workload=args.workload,
                controller=args.controller,
                root=args.output,
            )
        )
    )


if __name__ == "__main__":
    main()

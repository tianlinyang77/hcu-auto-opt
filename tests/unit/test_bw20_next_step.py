# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.contracts.platform_v1 import ArtifactManifest, MountSpec
from hcuopt.deployment.rotary_launch_triage import analyze
from hcuopt.evaluation.sglang_smoke import (
    SmokeVariantSpec,
    build_execution_request,
    load_workload_spec,
    validate_variant_pair,
)
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
WARNING = (b"Launch params (448, 1, 1) are larger than launch bounds (256) "
           b"for kernel _Z23rotary_embedding_kernelIN3c108BFloat16ELb1EEvPKlPT_S5_PKS4_illliii ")


@pytest.mark.parametrize("log", [WARNING, b"normal startup"])
def test_triage_never_treats_warning_or_its_absence_as_correctness(log):
    report = analyze(log, json.dumps({
        "model_type": "qwen2", "num_attention_heads": 14, "hidden_size": 896,
    }).encode())
    assert report["correctness"] == "not_validated"
    assert report["release_allowed"] is False
    assert report["source_hypothesis"]["predicted_block_x"] == 448
    assert report["source_hypothesis"]["matches_observed_block"] == (log == WARNING)
    assert report["source_hypothesis"]["installed_binary_source_binding"] == "not_verified"
    if log == WARNING:
        assert report["observations"][0]["reported_compiled_bound"] == 256
        assert report["observations"][0]["threads_per_block"] == 448


@pytest.mark.parametrize("config", [{}, {"model_type": "llama"},
                                   {"model_type": "qwen2", "num_attention_heads": 0}])
def test_triage_does_not_guess_unknown_model_shapes(config):
    assert analyze(WARNING, json.dumps(config).encode())["source_hypothesis"] is None


def test_bw20_pair_uses_existing_contracts_without_running_or_registering():
    target = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
    workload = load_workload_spec(ROOT / "config/workloads/bw20-sglang-smoke-v1.yaml")
    old = load_workload_spec(ROOT / "config/workloads/nmz36-sglang-smoke-v1.yaml")
    assert workload.request_payload() == old.request_payload()
    assert workload.server_argv() == old.server_argv()
    assert workload.target_id != old.target_id
    work = target.execution_host.work_root
    artifact = ArtifactManifest(
        candidate_id=uuid4(), kind="noop-source-archive",
        uri=f"file://{work}/artifacts/noop-source.tar",
        content_hash="sha256:" + "a" * 64,  # Typed fixture only, NOT a built artifact.
    )
    baseline = SmokeVariantSpec(
        name="baseline", runner_host_path=f"{work}/runner.py",
        spec_host_path=f"{work}/pair/input/spec.json",
        evidence_host_dir=f"{work}/pair/baseline",
    )
    noop = SmokeVariantSpec(
        name="noop", runner_host_path=baseline.runner_host_path,
        spec_host_path=baseline.spec_host_path, evidence_host_dir=f"{work}/pair/noop",
        artifact_id=artifact.artifact_id,
        mounts=[MountSpec(source=f"{work}/artifacts/noop-source.tar",
                          target="/opt/hcuopt/artifacts/noop-source.tar", read_only=True)],
    )
    validate_variant_pair(baseline, noop, artifact)
    lease = {"resource_id": "fixture-bw20-hcu7", "fencing_token": 1}
    left = build_execution_request(target, workload, baseline, **lease)
    right = build_execution_request(target, workload, noop, artifact, **lease)
    assert left.request_id != right.request_id
    assert left.argv == right.argv
    assert left.container_image == right.container_image
    assert len(right.mounts) == len(left.mounts) + 1
    assert left.mounts[2].source != right.mounts[2].source
    assert target.stage0_status == "pending" and target.automatic_release_allowed is False
    with pytest.raises(ValueError, match="workload target mismatch"):
        build_execution_request(target, old, baseline, **lease)

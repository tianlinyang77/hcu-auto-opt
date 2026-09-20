# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import sys
from pathlib import Path

import pytest

from hcuopt.deployment.bw20_capability_entrypoint import main, verify_module
from hcuopt.deployment.bw20_capability_execution import BW20CapabilityExecutionAdapter
from hcuopt.deployment.bw20_runtime_prepare import (
    IMPORT_MARKER,
    RELATIVE_MODULE,
    RUNTIME_MODULE,
    build_configuration,
)
from hcuopt.deployment.bw20_stage0_capabilities import isolate_outputs
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.domain.enums import LeaseScope
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.source_hash import file_uri_to_path
from tests.unit.test_bw20_stage0_adapter import Host
from tests.unit.test_bw20_stage0_capabilities import case  # noqa: F401


def test_entrypoint_checks_bytes_before_importing_torch(tmp_path, monkeypatch):
    path = tmp_path / "module.py"
    path.write_bytes(b"VALUE=1\n")
    expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    verify_module(path, expected)
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(ValueError, match="differs"):
        main(["--kind", "overlay", "--module", str(path), "--expected-hash", "sha256:" + "0" * 64])


def test_marker_preserves_numerical_code_without_activation_environment(monkeypatch):
    monkeypatch.delenv("HCUOPT_OVERLAY_IMPORT_MARKER", raising=False)
    namespace = {}
    exec(compile(b"VALUE=42\n" + IMPORT_MARKER, "fixture.py", "exec"), namespace)
    assert namespace["VALUE"] == 42
    assert "_hcuopt_stage0_import_observation" not in namespace


def test_marker_refuses_non_deployment_output(monkeypatch):
    monkeypatch.setenv("HCUOPT_OVERLAY_IMPORT_MARKER", "/unapproved.json")
    with pytest.raises(RuntimeError, match="destination"):
        exec(compile(IMPORT_MARKER, "fixture.py", "exec"), {})


def test_version_does_not_initialize_hardware(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert "bw20-sglang-prefill-v1" in capsys.readouterr().out


def test_configuration_passes_original_request_contract_and_bw20_executor(case, tmp_path):  # noqa: F811
    payload, target, old, _, _, _ = case
    source = file_uri_to_path(old.hotpatch.baseline_source.worktree_uri) / RELATIVE_MODULE
    source.parent.mkdir(parents=True)
    source.write_bytes(b"VALUE=1\n")
    config = build_configuration(
        target=target,
        baseline=old.hotpatch.baseline_source,
        candidate=old.hotpatch.candidate_source,
        artifact=old.hotpatch.artifact,
        controller=tmp_path / "controller",
        root=tmp_path / "prepared",
        model=Path("/models/qwen"),
        hyhal=Path("/usr/local/hyhal"),
    )
    config = isolate_outputs(config, tmp_path / "job")
    executor = BW20CapabilityExecutionAdapter(
        runner=Host(),
        target=target,
        configuration=config,
        context=payload["_job_context"],
        evidence_root=tmp_path,
    )
    for phase in ("baseline", "candidate", "recovery"):
        request = OverlayCapabilityProbe._execution_request(
            getattr(config.hotpatch, phase),
            target,
            RESOURCE,
            7,
            artifact=config.hotpatch.artifact if phase == "candidate" else None,
            overlay_target=RUNTIME_MODULE if phase == "candidate" else None,
            lease_scope=LeaseScope.EXCLUSIVE,
        )
        executor._validate_request(request, target)
        OverlayCapabilityProbe._validate_formal_phase_mount(
            getattr(config.hotpatch, phase), request
        )
        assert request.environment["HIP_VISIBLE_DEVICES"] == "0"
    assert config.profiler.profile_workload == "prefill"
    assert config.profiler.warmup_steps == config.profiler.num_steps == 1
    assert config.profiler.tool_candidates[0].output_host_uri.endswith("prefill.trace.json.gz")
    assert config.hotpatch.candidate_source == old.hotpatch.candidate_source
    baseline_cache = config.hotpatch.baseline.environment["HCUOPT_CANDIDATE_CACHE_DIR"]
    candidate_cache = config.hotpatch.candidate.environment["HCUOPT_CANDIDATE_CACHE_DIR"]
    recovery_cache = config.hotpatch.recovery.environment["HCUOPT_CANDIDATE_CACHE_DIR"]
    assert baseline_cache == recovery_cache
    assert candidate_cache != baseline_cache
    assert config.hotpatch.baseline.environment["TRITON_CACHE_DIR"] == (
        config.hotpatch.recovery.environment["TRITON_CACHE_DIR"]
    )
    assert config.hotpatch.baseline.environment["XDG_CACHE_HOME"] == (
        config.hotpatch.recovery.environment["XDG_CACHE_HOME"]
    )

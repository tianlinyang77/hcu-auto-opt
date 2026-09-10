# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hcuopt.deployment.bw20_admission import BW20SmokeAdmission
from hcuopt.deployment.bw20_profile import compose_framework_profile
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.domain.errors import TargetNotReady
from hcuopt.targets import target_fingerprint
from tests.unit.test_bw20_execution import TARGET
from tests.unit.test_bw20_profile import parts
from tests.unit.test_bw20_runtime_binding import fixture_record


def fixture_admission(tmp_path):
    target = TARGET.model_copy(deep=True)
    for blocker in target.blockers:
        if "framework_smoke" in blocker.blocks:
            blocker.status = "resolved"  # Synthetic admission fixture, not deployment evidence.
    evidence = tmp_path / "runtime.json"
    evidence.write_text(json.dumps(fixture_record()), encoding="utf-8")
    workload = tmp_path / "workload.yaml"
    workload.write_bytes((Path(__file__).resolve().parents[2]
        / "config/workloads/bw20-sglang-smoke-v1.yaml").read_bytes())
    return target, BW20SmokeAdmission(target_fingerprint(target), evidence,
        file_hash(evidence), workload, file_hash(workload))


def test_complete_operator_pin_allows_only_f1(tmp_path):
    target, admission = fixture_admission(tmp_path)
    build, registry = parts(tmp_path)
    profile = compose_framework_profile(build_handler=build, gpu_registry=registry,
                                        admission=admission)
    profile.validate_target(target)
    with pytest.raises(TargetNotReady, match="cannot grant"):
        profile.validate_target(target, scope="optimization")


def test_flipping_blockers_without_evidence_cannot_admit(tmp_path):
    target, _ = fixture_admission(tmp_path)
    build, registry = parts(tmp_path)
    with pytest.raises(TargetNotReady, match="operator-pinned"):
        profile = compose_framework_profile(build_handler=build, gpu_registry=registry)
        profile.validate_target(target)


@pytest.mark.parametrize("field", ["runtime_evidence", "workload"])
def test_pinned_files_rechecked_each_time(tmp_path, field):
    target, admission = fixture_admission(tmp_path)
    admission.validate(target)
    with getattr(admission, field).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="evidence changed"):
        admission.validate(target)


def test_target_revision_drift_rejected(tmp_path):
    target, admission = fixture_admission(tmp_path)
    target.measurement_constraints.append("changed")
    with pytest.raises(ValueError, match="revision"):
        admission.validate(target)


def test_repinning_fabricated_boolean_does_not_help(tmp_path):
    target, admission = fixture_admission(tmp_path)
    admission.runtime_evidence.write_text('{"passed": true}', encoding="utf-8")
    admission = replace(admission, runtime_evidence_sha256=file_hash(admission.runtime_evidence))
    with pytest.raises(ValueError, match="identity"):
        admission.validate(target)

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_endpoint_execution import (
    CANDIDATE_MODULE_HASH,
    SIGNED_ARTIFACT_PATH,
    TARGET_MODULE_PATH,
)
from hcuopt.deployment import bw20_endpoint_staging as staging
from hcuopt.deployment.bw20_smoke_preflight import MODEL_HASHES
from hcuopt.targets import load_target

REPOSITORY = Path(__file__).parents[2]
TARGET = load_target(REPOSITORY / "config/targets/bw20-sglang-0.5.12.yaml")


def _prepare(tmp_path: Path) -> staging.PreparedEndpointRun:
    return staging.prepare_endpoint_run(
        repository=REPOSITORY,
        destination=tmp_path / "prepared",
        run_id=uuid4(),
        fencing_token=73,
        target=TARGET,
    )


def test_prepare_freezes_minimal_sources_specs_and_exact_abba(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    plan = staging.verify_prepared_endpoint_run(prepared)

    assert plan["acquisition_order"] == ["baseline", "candidate", "candidate", "baseline"]
    assert plan["hcu_accessed"] is False
    assert plan["producer_verdict"] is None
    assert plan["automatic_release_allowed"] is False
    assert set(plan["input_sha256"]) == {
        "src/hcuopt/evaluation/sglang_endpoint_runner.py",
        "src/hcuopt/evaluation/sglang_smoke_runner.py",
        "activation/sitecustomize.py",
        "0000-baseline-spec.json",
        "0001-candidate-spec.json",
        "0002-candidate-spec.json",
        "0003-baseline-spec.json",
    }
    for ordinal, arm in enumerate(plan["acquisition_order"]):
        spec = json.loads(
            (prepared.directory / "input" / f"{ordinal:04d}-{arm}-spec.json").read_text()
        )
        assert spec["arm"] == arm
        assert spec["acquisition_ordinal"] == ordinal
        assert spec["warmup_requests"] == 1
        assert spec["measured_requests"] == 2
        assert spec["activation_attestation"]["expected_sha256"] == (
            CANDIDATE_MODULE_HASH
            if arm == "candidate"
            else "sha256:ef09cd90dd03a542e586c70b4baa805b9d9ab24f84e4e8c20b6fc313f6f5fe27"
        )
    candidate = plan["requests"][1]
    assert any(
        mount["source"] == SIGNED_ARTIFACT_PATH
        and mount["target"] == TARGET_MODULE_PATH
        and mount["read_only"] is True
        for mount in candidate["mounts"]
    )


def test_prepared_hash_drift_is_rejected_before_remote_contact(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    spec = prepared.directory / "input/0000-baseline-spec.json"
    spec.chmod(0o644)
    spec.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="input hash drift"):
        staging.stage_endpoint_run(prepared=prepared, runner=object())


def test_remote_staging_checks_acl_and_exact_receipt(tmp_path: Path, monkeypatch) -> None:
    prepared = _prepare(tmp_path)
    plan = staging.verify_prepared_endpoint_run(prepared)

    class Transport:
        instance = None

        def __init__(self, runner) -> None:
            self.commands = []
            self.copies = []
            type(self).instance = self

        def copy(self, source, destination, *, download=False) -> None:
            assert not download
            self.copies.append((Path(source), destination))

        def checked(self, argv, timeout=60):
            self.commands.append((tuple(argv), timeout))
            if argv[0] == "getfacl":
                return b"user::rwx\nuser:65534:rwx\n"
            if argv[:3] == ("python3", "-c", staging.VERIFY_RUN):
                return json.dumps(
                    {
                        "schema_version": "bw20-endpoint-staging-receipt-v1",
                        "run": prepared.remote_run_root,
                        "plan_sha256": prepared.plan_sha256,
                        "input_sha256": plan["input_sha256"],
                        "candidate_artifact_sha256": CANDIDATE_MODULE_HASH,
                        "model_hashes": MODEL_HASHES,
                        "acquisition_outputs": [
                            f"{index:04d}-{arm}"
                            for index, arm in enumerate(staging.ACQUISITION_ORDER)
                        ],
                        "output_acl_uid": 65534,
                    }
                ).encode()
            return b""

    monkeypatch.setattr(staging, "BW20Transport", Transport)
    result = staging.stage_endpoint_run(prepared=prepared, runner=object())

    assert result.receipt["model_hashes"] == MODEL_HASHES
    assert len(Transport.instance.copies) == len(plan["input_sha256"]) + 1
    assert sum(command[0][0] == "setfacl" for command in Transport.instance.commands) == 4
    assert Transport.instance.commands[-1][0][:3] == ("python3", "-c", staging.VERIFY_RUN)


def test_host_preflight_programs_remain_python36_compatible() -> None:
    assert staging.host_scripts_are_python36_compatible()

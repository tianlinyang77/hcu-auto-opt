# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.deployment import bw20_m1_adjudication_worker as deployment
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError


def test_build_worker_binds_hcu_free_independent_adjudicator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    calls = {}
    registry = SimpleNamespace(profile="bw20-m1-manual-v1")

    def build_registry(**kwargs):
        calls["registry"] = kwargs
        return registry

    class Worker:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr(deployment, "build_m1_adjudication_registry", build_registry)
    monkeypatch.setattr(deployment, "load_registered_m1_protocol", lambda: "protocol")
    monkeypatch.setattr(deployment, "HashedEvidenceReader", lambda root: ("reader", root))
    monkeypatch.setattr(deployment, "Worker", Worker)

    worker = deployment.build_worker(
        worker_id="bw20-adjudicator",
        api_url="http://127.0.0.1:8000",
        trusted_evidence_root=trusted,
        output_dir=trusted / "adjudication",
    )

    assert worker.args[:2] == ("bw20-adjudicator", WorkerType.EVALUATION)
    assert worker.kwargs["adapters"] is registry
    assert worker.kwargs["capabilities"] == {
        "adapter_profile": "bw20-m1-manual-v1",
        "adapters": ["candidate_adjudicator"],
        "hcu_required": False,
        "automatic_release_allowed": False,
    }
    assert calls["registry"]["profile"] == "bw20-m1-manual-v1"
    assert calls["registry"]["protocol"] == "protocol"
    assert calls["registry"]["reader"] == ("reader", trusted.resolve())
    assert calls["registry"]["evidence_root"] == (
        trusted / "adjudication"
    ).resolve()


def test_build_worker_rejects_output_outside_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    with pytest.raises(ExecutionSafetyError, match="trusted evidence root"):
        deployment.build_worker(
            worker_id="bw20-adjudicator",
            api_url="http://127.0.0.1:8000",
            trusted_evidence_root=trusted,
            output_dir=tmp_path / "outside",
        )

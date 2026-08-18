from __future__ import annotations

import json
import os
import platform
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets.loader import load_target

RUN_TARGET_LOCK = os.environ.get("HCUOPT_RUN_TARGET_LOCK") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = PROJECT_ROOT / "config/targets/nmz36-sglang-0.5.12.yaml"


@pytest.mark.target_lock
@pytest.mark.skipif(not RUN_TARGET_LOCK, reason="requires explicit Target Lock execution")
def test_real_f1c_pipeline_on_locked_nmz36_target() -> None:
    target = load_target(TARGET_PATH)
    assert socket.gethostname() == target.execution_host.name
    assert os.environ["HCUOPT_IMAGE_REFERENCE"] == target.inference_image.immutable_reference
    assert platform.python_version().startswith(target.inference_image.python_version + ".")
    assert os.sched_getaffinity(0) == set(range(112, 128))
    assert Path("/sys/devices/system/node/node7/cpulist").read_text().strip() == "112-127"
    assert os.environ["ROCR_VISIBLE_DEVICES"] == "7"
    assert os.environ["HIP_VISIBLE_DEVICES"] == "7"
    assert Path("/dev/kfd").exists()
    assert Path("/dev/dri").is_dir()

    output_dir = Path(os.environ["HCUOPT_F1C_OUTPUT_DIR"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile = "nmz36-framework-smoke-v1"
    manager = GitSourceManager(profile)
    builder = NoopBuilder(profile)
    store = LocalArtifactStore(output_dir / "artifacts", profile)
    baseline = manager.prepare_baseline(target, output_dir)
    candidate = None

    try:
        candidate_id = uuid4()
        candidate = manager.create_candidate(baseline, candidate_id, output_dir)
        payload = {
            "candidate_id": str(candidate_id),
            "source_snapshot": candidate.model_dump(mode="json"),
            "adapter_provenance": [manager.provenance.model_dump(mode="json")],
        }
        first = builder.build(payload, output_dir)
        second = builder.build(payload, output_dir)
        assert first.content_hash == second.content_hash
        published = store.publish(first, file_uri_to_path(first.uri))
    finally:
        if candidate is not None and file_uri_to_path(candidate.worktree_uri).exists():
            manager.remove_candidate(baseline, candidate, output_dir)

    baseline_path = file_uri_to_path(baseline.worktree_uri)
    assert canonical_source_hash(baseline_path) == baseline.source_hash
    evidence = {
        "target_id": target.target_id,
        "host": socket.gethostname(),
        "image_reference": os.environ["HCUOPT_IMAGE_REFERENCE"],
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "numa_node": target.execution_host.accelerator.numa_node,
        "hcu_device_index": target.execution_host.accelerator.device_index,
        "baseline": baseline.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "artifact": published.model_dump(mode="json"),
        "repeat_content_hash": second.content_hash,
        "baseline_clean_after_cleanup": True,
    }
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )

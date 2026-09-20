# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import copy
import zipfile

import pytest

from hcuopt.deployment import bw20_runtime_binding as binding


def fixture_record():
    # Synthetic manifests exercise verification logic; never acceptance evidence.
    source = {f"fixture_{i}.py": "a" * 64 for i in range(1971)}
    source.update(binding.OMITTED)
    source[binding.QWEN] = binding.QWEN_SOURCE
    wheel = {k: v for k, v in source.items() if k not in binding.OMITTED}
    wheel[binding.QWEN] = binding.QWEN_RUNTIME
    wheel["_version.py"] = binding.GENERATED_VERSION
    return {
        "schema": "bw20-python-runtime-binding-v1", "version": binding.VERSION,
        "wheel_sha256": binding.WHEEL_SHA256, "source_commit": binding.COMMIT,
        "source_tree": binding.TREE, "source_status": "",
        "source_python_sha256": source, "wheel_python_sha256": wheel,
        "installed_python_sha256": dict(wheel),
    }


def test_bounded_relation_does_not_grant_acceptance():
    result = binding.validate_binding(fixture_record())
    assert result["python_runtime_relationship_verified"] is True
    for key in ("native_build_provenance_verified", "candidate_activation",
                "framework_smoke_accepted", "stage0_accepted", "automatic_release_allowed"):
        assert result[key] is False


def test_non_object_evidence_rejected():
    with pytest.raises(ValueError, match="object"):
        binding.validate_binding([])


@pytest.mark.parametrize("field", ["wheel_sha256", "version", "source_commit", "source_tree",
                                  "source_status"])
def test_identity_drift_rejected(field):
    record = fixture_record()
    record[field] = "changed"
    with pytest.raises(ValueError, match="identity"):
        binding.validate_binding(record)


@pytest.mark.parametrize("field", ["wheel_python_sha256", "installed_python_sha256",
                                  "source_python_sha256"])
def test_missing_manifest_cannot_be_replaced_by_boolean(field):
    record = fixture_record()
    del record[field]
    record["all_python_files_equal"] = True
    record["validation"] = {"python_runtime_relationship_verified": True}
    with pytest.raises(ValueError, match="complete Python"):
        binding.validate_binding(record)


def test_installed_extra_file_rejected():
    record = fixture_record()
    record["installed_python_sha256"]["extra.py"] = "a" * 64
    with pytest.raises(ValueError, match="complete frozen wheel"):
        binding.validate_binding(record)


def test_matching_wheel_and_installed_drift_still_rejected():
    record = fixture_record()
    for field in ("wheel_python_sha256", "installed_python_sha256"):
        record[field]["fixture_0.py"] = "b" * 64
    with pytest.raises(ValueError, match="four declared"):
        binding.validate_binding(record)


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "a//b.py", "a\\b.py"])
def test_unsafe_inventory_paths(path):
    record = fixture_record()
    record["source_python_sha256"][path] = "a" * 64
    with pytest.raises(ValueError, match="path/hash"):
        binding.validate_binding(record)


def test_unexpected_packaging_difference():
    record = copy.deepcopy(fixture_record())
    record["source_python_sha256"][next(iter(binding.OMITTED))] = "b" * 64
    with pytest.raises(ValueError, match="omitted"):
        binding.validate_binding(record)


def test_wheel_member_hashes(tmp_path):
    path = tmp_path / "test.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("sglang/a.py", "example")
        wheel.writestr("sglang/data.json", "{}")
    assert binding.wheel_inventory(path) == {"a.py": binding.sha256(b"example")}


def test_duplicate_wheel_member_rejected(tmp_path):
    path = tmp_path / "test.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("sglang/a.py", "first")
        with pytest.warns(UserWarning):
            wheel.writestr("sglang/a.py", "second")
    with pytest.raises(ValueError, match="duplicate"):
        binding.wheel_inventory(path)


def test_inventory_regular_files(tmp_path):
    (tmp_path / "test.py").write_bytes(b"# test")
    assert binding.inventory(tmp_path) == {"test.py": binding.sha256(b"# test")}

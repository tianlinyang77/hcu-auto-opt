# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read installed package metadata and binary hashes without importing kernel code.

Run this standalone file using the pinned image's Python after container-scope
review, with no HCU devices and no network. stdout is the evidence output. This
does not identify loaded dispatch paths or prove source/build equivalence.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import re
import sys
import sysconfig
from pathlib import Path

# The frozen DAS source declares distribution "sglang-kernel" while importing
# module "sgl_kernel". Retain the upstream spelling as a separate observation.
PACKAGES = ("sgl-kernel", "sglang-kernel", "sglang", "aiter")


def installed_distribution(name: str) -> metadata.Distribution:
    """Discover site metadata without executing site/.pth startup hooks under -S."""
    if not sys.flags.no_site:
        return metadata.distribution(name)
    paths = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    return discover_distribution(name, paths)


def discover_distribution(name: str, paths: list[str]) -> metadata.Distribution:
    def normalize(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    for dist in metadata.distributions(path=paths):
        if normalize(dist.metadata.get("Name", "")) == normalize(name):
            return dist
    raise metadata.PackageNotFoundError(name)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def inspect_distribution(name: str) -> dict[str, object]:
    try:
        dist = installed_distribution(name)
    except metadata.PackageNotFoundError:
        return {"requested_name": name, "status": "not_installed", "libraries": []}
    root = Path(dist.locate_file("")).resolve()
    result: dict[str, object] = {
        "requested_name": name, "version": dist.version, "root": str(root),
        "status": "metadata_only", "libraries": [], "errors": [],
        "record_sha256": None,
    }
    libraries, errors = [], []
    files = dist.files
    if files is None:
        errors.append("distribution_file_manifest_unavailable")
    for entry in sorted(files or (), key=str):
        relative = str(entry)
        is_library = re.search(r"\.so(?:\.\d+)*$", relative) is not None
        is_record = relative.endswith(".dist-info/RECORD")
        if not (is_library or is_record):
            continue
        path = Path(dist.locate_file(entry))
        resolved = path.resolve()
        # Refuse editable/escaping manifests and symlink paths; record the gap.
        if (not resolved.is_relative_to(root) or path.absolute() != resolved
                or not resolved.is_file()):
            errors.append(f"unsafe_or_missing_manifest_file:{relative}")
            continue
        try:
            size_before = resolved.stat().st_size
            digest = file_hash(resolved)
            if size_before != resolved.stat().st_size:
                raise OSError("size_changed_while_hashing")
        except OSError as exc:
            errors.append(f"hash_failed:{relative}:{type(exc).__name__}")
            continue
        if is_record:
            result["record_sha256"] = digest
        else:
            libraries.append({"relative_path": relative, "path": str(resolved),
                              "bytes": size_before, "sha256": digest})
    result.update(libraries=libraries, errors=errors,
                  status="incomplete" if errors else "inventory_collected")
    return result


def collect() -> dict[str, object]:
    return {
        "schema": "kernel-binary-inventory-v1", "python": sys.version,
        "packages": [inspect_distribution(name) for name in PACKAGES],
        "kernel_modules_imported": any(
            key.split(".")[0] in {"sgl_kernel", "sglang", "aiter"} for key in sys.modules),
        "site_startup_disabled": bool(sys.flags.no_site),
        "device_execution_performed": False,
        "loaded_dispatch_identity": "not_verified", "build_source_binding": "not_verified",
        "correctness": "not_validated", "automatic_release_allowed": False,
    }


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))

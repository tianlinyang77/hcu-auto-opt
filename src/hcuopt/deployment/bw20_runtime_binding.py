# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only Python source/installed-wheel binding for the fixed BW20 image.

Standalone stdlib-only collector: invoke with Python -I -S in a CPU container.
This proves a bounded Python packaging relationship, NOT native build provenance,
candidate activation, performance, or permission to clear a Target blocker.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sysconfig
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "0.5.12+das.opt1.dtk2604.torch2100.2606021957.gdad582.hcuopt1"
WHEEL_SHA256 = "20f3fef4bf87e9afd44d71ffabf4b26ec34934e68a8fad934d06d3c8634949aa"
COMMIT = "dad582f28458cd0e11e0be675fbe7fcc7ab65ac1"
TREE = "a883d7eb4b4667768c19f0c9b0456d41f52da91c"
QWEN = "srt/models/qwen2.py"
QWEN_SOURCE = "bb5899b3d94bbb0455c78d939aaa1ae48aee91a70d12bbab34921d0bfb3f270f"
QWEN_RUNTIME = "fc93b92e4ff7b44376e4d445ecba8fc907a83f147a482dad7c823fe52269b48d"
GENERATED_VERSION = "05d805e75f14e1980771ae639298eedc160ba239b935599d03144553cf082a51"
SCRIPT_PREFIX = "multimodal_gen/.claude/skills/sglang-diffusion-benchmark-profile/scripts/"
OMITTED = {
    SCRIPT_PREFIX + "bench_diffusion_denoise.py":
        "2a4cf17c4ce3bcf27f7b2e9dff1dcd03ace37d544a6acf134f064113c8616378",
    SCRIPT_PREFIX + "diffusion_skill_env.py":
        "6e33eceb3f170aec98937c63a7815d8132c0fc3565b20f5a6dee4ed5a8e3f050",
}
SOURCE_NON_PYTHON_LINKS = {
    "srt/mem_cache/cpp_radix_tree/.clang-format": "../../../../../sgl-kernel/.clang-format",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path, *, allowed_non_python_links: dict[str, str] | None = None
              ) -> dict[str, str]:
    if root.resolve(strict=True) != root.absolute() or not root.is_dir():
        raise ValueError("inventory root is redirected or not a directory")
    result = {}
    for path in sorted(root.rglob("*")):
        # Reject symlink directories too: rglob need not traverse them.
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if ((allowed_non_python_links or {}).get(relative) == path.readlink().as_posix()
                    and path.name == ".clang-format"):
                continue  # Fixed formatting metadata; never follow/read its target.
            raise ValueError("symlink in package inventory")
        if path.suffix != ".py":
            continue
        if not path.is_file():
            raise ValueError("nonregular Python file")
        result[path.relative_to(root).as_posix()] = sha256(path.read_bytes())
    return result


def wheel_inventory(path: Path) -> dict[str, str]:
    result = {}
    with zipfile.ZipFile(path) as wheel:
        for entry in wheel.infolist():
            name = entry.filename
            if not (name.startswith("sglang/") and name.endswith(".py")):
                continue
            relative = name[len("sglang/"):]
            pure = PurePosixPath(relative)
            if (pure.is_absolute() or ".." in pure.parts or str(pure) != relative
                    or "\\" in relative or relative in result
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("unsafe or duplicate wheel Python member")
            result[relative] = sha256(wheel.read(entry))
    return dict(sorted(result.items()))


def validate_binding(record: dict) -> dict:
    """Recompute from complete inventories, never trust a caller's passed flag."""
    if not isinstance(record, dict):
        raise ValueError("runtime binding must be an object")
    if (record.get("schema") != "bw20-python-runtime-binding-v1"
            or record.get("version") != VERSION
            or record.get("wheel_sha256") != WHEEL_SHA256
            or record.get("source_commit") != COMMIT
            or record.get("source_tree") != TREE
            or record.get("source_status") != ""):
        raise ValueError("runtime/source identity mismatch")
    source, wheel, installed = (record.get(key) for key in (
        "source_python_sha256", "wheel_python_sha256", "installed_python_sha256"))
    for manifest in (source, wheel, installed):
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("complete Python inventories are required")
        for name, digest in manifest.items():
            if (not isinstance(name, str) or not name.endswith(".py")
                    or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                    or str(PurePosixPath(name)) != name or "\\" in name
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("invalid inventory path/hash")
    if len(source) != 1974 or len(wheel) != 1973 or wheel != installed:
        raise ValueError("installed Python does not match the complete frozen wheel")
    expected = dict(source)
    for name, digest in OMITTED.items():
        if expected.pop(name, None) != digest:
            raise ValueError("unexpected omitted development script")
    if "_version.py" in source or expected.get(QWEN) != QWEN_SOURCE:
        raise ValueError("source does not match the fixed packaging transform")
    expected["_version.py"] = GENERATED_VERSION
    expected[QWEN] = QWEN_RUNTIME
    if expected != wheel:
        raise ValueError("Python difference outside the four declared packaging changes")
    return {
        "python_runtime_relationship_verified": True,
        "relationship": "frozen_source_plus_fixed_import_repair_and_packaging",
        "source_count": len(source), "installed_count": len(installed),
        "native_build_provenance_verified": False, "candidate_activation": False,
        "framework_smoke_accepted": False, "stage0_accepted": False,
        "automatic_release_allowed": False,
    }


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), *args],
        timeout=60, text=True).strip()


def collect(source: Path, site: Path, wheel: Path) -> dict:
    if source.resolve(strict=True) != source.absolute():
        raise ValueError("redirected source")
    if wheel.resolve(strict=True) != wheel.absolute() or not wheel.is_file():
        raise ValueError("redirected wheel")
    distributions = [d for d in importlib.metadata.distributions(path=[str(site)])
                     if d.metadata.get("Name", "").lower() == "sglang"]
    if len(distributions) != 1 or distributions[0].version != VERSION:
        raise ValueError("SGLang distribution identity mismatch")
    before = (git(source, "rev-parse", "HEAD"), git(source, "rev-parse", "HEAD^{tree}"),
              git(source, "status", "--porcelain", "--untracked-files=all"))
    record = {
        "schema": "bw20-python-runtime-binding-v1", "version": VERSION,
        "source_commit": before[0], "source_tree": before[1], "source_status": before[2],
        "wheel_sha256": sha256(wheel.read_bytes()),
        "source_python_sha256": inventory(source / "python/sglang",
            allowed_non_python_links=SOURCE_NON_PYTHON_LINKS),
        "excluded_non_python_symlinks": SOURCE_NON_PYTHON_LINKS,
        "wheel_python_sha256": wheel_inventory(wheel),
        "installed_python_sha256": inventory(site / "sglang"),
    }
    after = (git(source, "rev-parse", "HEAD"), git(source, "rev-parse", "HEAD^{tree}"),
             git(source, "status", "--porcelain", "--untracked-files=all"))
    if before != after:
        raise ValueError("source changed during collection")
    record["validation"] = validate_binding(record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/source"))
    parser.add_argument("--site", type=Path, default=Path(sysconfig.get_path("purelib")))
    parser.add_argument("--wheel", type=Path, default=Path(
        f"/opt/hcuopt-image/sglang-{VERSION}-cp310-cp310-linux_x86_64.whl"))
    args = parser.parse_args()
    print(json.dumps(collect(args.source, args.site, args.wheel), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

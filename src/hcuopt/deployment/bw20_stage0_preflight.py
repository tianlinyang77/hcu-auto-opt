# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only BW20 host observations, NOT Stage 0 evidence or run authorization.

Uses only the standard library and sysfs. Does not import torch, create containers,
set clocks, reserve devices, or infer that an instantaneous idle card is exclusive.
Run this file directly on the host; the inference image is not needed for sysfs.
Keep this standalone script compatible with the host's Python 3.6 runtime.
"""

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_HOST = "github-bw20"
EXPECTED_PCI = "0000:b1:00.0"
DEVICE_FILES = (
    "gpu_busy_percent", "mem_info_vram_used", "mem_info_vram_total", "numa_node",
    "power_dpm_force_performance_level", "pp_dpm_sclk", "pp_dpm_mclk",
)


def _read(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return {"status": "oversized"}
        return {"status": "observed", "raw": raw}
    except FileNotFoundError:
        return {"status": "missing"}
    except PermissionError:
        return {"status": "permission_denied"}
    except (OSError, UnicodeError):
        return {"status": "unreadable"}


def _device(path: Path) -> dict:
    return {
        "resolved_path": str(path),
        "observed_pci": path.name,
        "expected_pci_matches": path.name == EXPECTED_PCI,
        "files": {name: _read(path / name) for name in DEVICE_FILES},
    }


def _kfd_processes(root: Path) -> dict:
    try:
        entries = []
        for entry in root.iterdir():
            if entry.name.isdecimal():
                entries.append(entry)
            if len(entries) > 4096:
                return {"status": "oversized", "processes": None}
        return {
            "status": "observed",
            "processes": [int(entry.name) for entry in sorted(entries, key=lambda p: int(p.name))],
            # A global PID inventory is not a reliable per-device ownership map.
            "device_attribution": "not_established",
        }
    except FileNotFoundError:
        return {"status": "missing", "processes": None}
    except PermissionError:
        return {"status": "permission_denied", "processes": None}
    except OSError:
        return {"status": "unreadable", "processes": None}


def collect(sys_root: Path = Path("/sys")) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    host = platform.node()
    try:
        path = (sys_root / "class/drm/renderD135/device").resolve(strict=True)
        device = {"status": "observed", **_device(path)}
    except (OSError, RuntimeError):
        device = {"status": "unavailable", "expected_pci_matches": False}
    kfd = _kfd_processes(sys_root / "class/kfd/kfd/proc")
    return {
        "schema_version": "bw20-stage0-host-observation-v1",
        "collection_started_at": started,
        "collection_finished_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "host_python_version": platform.python_version(),
        "expected_host_matches": host == EXPECTED_HOST,
        "expected_physical_device_index": 7,
        "expected_render_device": "/dev/dri/renderD135",
        "expected_pci": EXPECTED_PCI,
        "device": device,
        "kfd": kfd,
        "measurement_window": "not_verified",
        "clock_policy_authorized": False,
        "target_snapshot_bound": False,
        "stage0_accepted": False,
        "performance_conclusion": "not_measured",
        "automatic_release_allowed": False,
        "scope": "non_atomic_readonly_host_observation_not_formal_evidence",
    }


def main() -> int:
    result = collect()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    files = result["device"].get("files", {})
    complete = (
        result["expected_host_matches"] and result["device"]["expected_pci_matches"]
        and all(files.get(name, {}).get("status") == "observed" for name in DEVICE_FILES)
        and result["kfd"]["status"] == "observed"
    )
    # Exit 0 means complete observations only, never a go/no-go decision.
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())

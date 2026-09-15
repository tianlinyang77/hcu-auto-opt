# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only clock preflight. A compatible observation is NEVER write authority.

Proposes known-auto-only scope for review; does not implement ClockBackend or
modify the existing complete-policy capture/restore contract.
"""

import json
import re
from uuid import UUID

from hcuopt.deployment.bw20_stage0_telemetry import require_endpoint

READ_CLOCK_STATE = r"""
import hashlib,json,os,pathlib,platform,stat
root=pathlib.Path('/sys/class/drm/renderD135/device').resolve(strict=True)
if platform.node()!='github-bw20' or root.name!='0000:b1:00.0':
    raise ValueError('wrong clock observation target')
def read(path):
    with open(str(path),encoding='utf-8') as stream:
        value=stream.read(16385)
    if len(value)>16384: raise ValueError('oversized clock state')
    return value
names=('power_dpm_force_performance_level','pp_dpm_sclk','pp_dpm_mclk',
       'pp_sclk_od','pp_mclk_od','pp_gfx_boost','numa_node')
before=read(root/names[0])
files={name:read(root/name) for name in names}
after=read(root/names[0])
if before!=after or files[names[0]]!=before: raise ValueError('clock mode changed')
metadata={name:dict(mode=stat.filemode((root/name).stat().st_mode),
                   writable=os.access(str(root/name),os.W_OK)) for name in names}
header=pathlib.Path('/usr/local/hyhal/include/rocm_smi/rocm_smi_v2.h').read_bytes()
print(json.dumps(dict(host=platform.node(),pci=root.name,
    boot_id=read('/proc/sys/kernel/random/boot_id').strip(),files=files,metadata=metadata,
    header_sha256='sha256:'+hashlib.sha256(header).hexdigest(),
    scope='readonly_non_atomic_observation')))
"""


def parse_levels(value):
    """Supported levels and instantaneous active index, NOT the enabled mask."""
    levels, active, disabled = {}, [], False
    for line in value.strip().splitlines():
        match = re.fullmatch(r"\s*(\d+):\s*(\d+)Mhz\s*(\*)?\s*(\(DPM disabled\))?\s*",
                             line)
        if not match:
            raise ValueError("unsupported clock table format")
        index, mhz = int(match[1]), int(match[2])
        if index in levels or index > 63 or mhz < 1:
            raise ValueError("invalid clock level")
        levels[index] = mhz
        if match[3]:
            active.append(index)
        disabled = disabled or bool(match[4])
    if (sorted(levels) != list(range(len(levels))) or len(active) != 1
            or len(set(levels.values())) != len(levels) or (disabled and len(levels) != 1)):
        raise ValueError("ambiguous supported clock table")
    return dict(supported_mhz=levels, current_index=active[0], dpm_disabled=disabled)


def assess_clock_state(raw):
    if (raw["host"] != "github-bw20" or raw["pci"] != "0000:b1:00.0"
            or raw["scope"] != "readonly_non_atomic_observation"
            or raw["files"]["numa_node"].strip() != "4"):
        raise ValueError("clock preflight target mismatch")
    UUID(raw["boot_id"])
    files = raw["files"]
    sclk, mclk = parse_levels(files["pp_dpm_sclk"]), parse_levels(files["pp_dpm_mclk"])
    reasons = []
    if files["power_dpm_force_performance_level"].strip() != "auto":
        reasons.append("initial_mode_not_auto")
    if any(files[name].strip() != "0" for name in ("pp_sclk_od", "pp_mclk_od", "pp_gfx_boost")):
        reasons.append("nonzero_or_unknown_overdrive_or_boost")
    selected = [i for i, mhz in sclk["supported_mhz"].items() if mhz == 1500]
    if len(selected) != 1 or sclk["dpm_disabled"]:
        reasons.append("required_sclk_level_unavailable")
    if mclk["supported_mhz"] != {0: 1800} or not mclk["dpm_disabled"]:
        reasons.append("memory_clock_not_fixed_1800")
    return dict(
        schema_version="bw20-clock-preflight-v1", raw=raw,
        sclk=sclk, mclk=mclk, candidate_scope="known_auto_only_proposed",
        observed_prerequisites_match=not reasons, rejection_reasons=reasons,
        proposed_sclk_index=selected[0] if len(selected) == 1 else None,
        memory_clock_action="observe_only_no_write",
        restoration_scope="default_auto_only_not_arbitrary_prior_policy",
        enabled_mask_verified=False, restoration_verified=False,
        execution_allowed=False, stage0_accepted=False, automatic_release_allowed=False,
        remaining_requirements=["policy_review", "reserved_device_window",
                                "scoped_privileged_writer", "recovery_authority",
                                "hardware_restore_validation"],
    )


def collect_clock_preflight(runner):
    require_endpoint(runner)
    result = runner.run(("python3", "-c", READ_CLOCK_STATE), timeout=20)
    if result.returncode or result.stderr or len(result.stdout) > 131072:
        raise RuntimeError("clock preflight collection failed")
    return assess_clock_state(json.loads(result.stdout))


def main():
    from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner

    result = collect_clock_preflight(BW20LocalCommandRunner())
    print(json.dumps(result))
    # Even compatible observations are NOT admission or permission to execute.
    return 0 if result["observed_prerequisites_match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

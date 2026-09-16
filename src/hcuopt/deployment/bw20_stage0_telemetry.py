# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""BW20 read-only host telemetry for the existing measurement-evidence-v2 schema.

Unknown KFD attribution is retained conservatively, never filtered as idle. These
observations are neither a hardware window grant nor a Stage 0 verdict.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from uuid import UUID

from hcuopt.deployment.bw20_stage0_runtime import RESOURCE, _start
from hcuopt.measurement.models import TelemetrySnapshotV2

# Standard library only, including Python 3.6 on the target host. No project
# imports, package installs, clock writes, signals, or accelerator allocations.
HOST_TELEMETRY = r"""
import json,os,pathlib,platform,subprocess
def read(path,limit=65536):
    with open(str(path),encoding='utf-8') as f: value=f.read(limit+1)
    if len(value)>limit: raise ValueError('oversized host observation')
    return value
def pids():
    entries=list(pathlib.Path('/sys/class/kfd/kfd/proc').iterdir())
    if len(entries)>4096 or any(not p.name.isdecimal() for p in entries):
        raise ValueError('invalid KFD inventory')
    return sorted(int(p.name) for p in entries)
def properties(path):
    values={}
    for line in read(path).splitlines():
        parts=line.split()
        if len(parts)==2: values[parts[0]]=parts[1]
    return values
def topology():
    root=pathlib.Path('/sys/class/kfd/kfd/topology/nodes')
    entries=list(root.iterdir())
    if len(entries)>4096 or any(not p.name.isdecimal() for p in entries):
        raise ValueError('invalid KFD topology')
    result=[]
    for node in sorted(entries,key=lambda p:int(p.name)):
        values=properties(node/'properties')
        result.append(dict(node=int(node.name),gpu_id=read(node/'gpu_id',128).strip(),
                           drm_render_minor=values.get('drm_render_minor'),
                           location_id=values.get('location_id')))
    return result
def queue_gpu_ids(pid):
    root=pathlib.Path('/sys/class/kfd/kfd/proc')/str(pid)/'queues'
    before=list(root.iterdir())
    if len(before)>65536 or any(not p.name.isdecimal() for p in before):
        raise ValueError('invalid KFD queue inventory')
    names=sorted((p.name for p in before),key=int)
    values=[read(root/name/'gpuid',128).strip() for name in names]
    after=sorted((p.name for p in root.iterdir()),key=int)
    if names!=after: raise ValueError('KFD queue inventory changed')
    return values
def smi(*args):
    p=subprocess.run(('/opt/hyhal/bin/hy-smi',)+args,stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE,timeout=15,check=False)
    if len(p.stdout)>262144 or len(p.stderr)>65536: raise ValueError('oversized SMI')
    if p.returncode: raise ValueError('SMI command failed')
    return dict(stdout=p.stdout.decode('utf-8'),stderr=p.stderr.decode('utf-8'))
def executable(path):
    try: return dict(value=os.readlink(path),status='observed')
    except OSError as exc: return dict(value='unavailable',status=type(exc).__name__)
host=platform.node()
root=pathlib.Path('/sys/class/drm/renderD135/device').resolve(strict=True)
if host!='github-bw20' or root.name!='0000:b1:00.0':
    raise ValueError('wrong host or physical device')
before=pids()
stats={pid:read('/proc/%d/stat'%pid) for pid in before}
nodes=topology()
device=smi('-d','7','--showtemp','--showclocks','--showperflevel','--showpower')
process_output=smi('--showpids')
processes=[]
for pid in before:
    base='/proc/%d/'%pid
    exe=executable(base+'exe')
    processes.append(dict(pid=pid,stat_before=stats[pid],stat_after=read(base+'stat'),
                          executable=exe['value'],executable_status=exe['status'],
                          comm=read(base+'comm',4000),queue_gpu_ids=queue_gpu_ids(pid)))
after=pids()
if before!=after: raise ValueError('KFD inventory changed during observation')
files={name:read(root/name,8192) for name in (
    'gpu_busy_percent','mem_info_vram_used','numa_node',
    'power_dpm_force_performance_level','pp_dpm_sclk','pp_dpm_mclk')}
print(json.dumps(dict(schema_version='bw20-stage0-telemetry-v1',host=host,pci=root.name,
    boot_id=read('/proc/sys/kernel/random/boot_id',128).strip(),files=files,
    smi_device=device,smi_processes=process_output,kfd_before=before,kfd_after=after,
    processes=processes,kfd_topology=nodes,
    scope='non_atomic_readonly_host_observation')))
"""


class BW20TelemetryError(RuntimeError):
    pass


def require_endpoint(runner):
    if (runner.host, runner.user, runner.port) != ("10.17.1.20", "github", 22):
        raise ValueError("BW20 telemetry requires the explicit target endpoint")


def _one(text, pattern):
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise BW20TelemetryError("missing or ambiguous device telemetry field")
    return matches[0]


def parse_snapshot(raw: Mapping, managed: Mapping[int, str]) -> TelemetrySnapshotV2:
    """Keep all KFD processes as potential interference when attribution is unknown."""
    try:
        if (
            raw["schema_version"] != "bw20-stage0-telemetry-v1"
            or raw["host"] != "github-bw20"
            or raw["pci"] != "0000:b1:00.0"
            or int(raw["files"]["numa_node"]) != 4
        ):
            raise BW20TelemetryError("telemetry target mismatch")
        inventory = raw["kfd_before"]
        if (
            not isinstance(inventory, list)
            or len(inventory) > 4096
            or any(type(pid) is not int or pid < 1 for pid in inventory)
            or len(set(inventory)) != len(inventory)
            or raw["kfd_after"] != inventory
            or sorted(p["pid"] for p in raw["processes"]) != sorted(inventory)
        ):
            raise BW20TelemetryError("incomplete or changing KFD inventory")
        # Restrict extraction to the physical device, not an arbitrary first match.
        smi = raw["smi_device"]["stdout"]
        if set(re.findall(r"HCU\[([0-9]+)\]", smi)) != {"7"}:
            raise BW20TelemetryError("SMI physical device mismatch")
        smi = "\n".join(line for line in smi.splitlines() if line.startswith("HCU[7]"))
        number = r"([0-9]+(?:\.[0-9]+)?)"
        processes = []
        process_text = raw["smi_processes"]["stdout"]
        smi_pids = [int(p) for p in re.findall(r"^PID: ([0-9]+)\s*$", process_text, re.M)]
        if sorted(smi_pids) != sorted(inventory):
            raise BW20TelemetryError("SMI and KFD process inventories differ")
        for process in raw["processes"]:
            pid = process["pid"]
            token = _start(process["stat_before"], pid)
            if token != _start(process["stat_after"], pid):
                raise BW20TelemetryError("KFD process identity changed")
            block = _one(process_text, rf"^PID: {pid}\s*\n([\s\S]*?)(?=^PID: |\Z)")
            memory = int(_one(block, r"^\s*VRAM USED\(MiB\): ([0-9]+)\s*$"))
            processes.append(
                dict(
                    process_id=pid,
                    executable=process["executable"],
                    command_line=process["comm"].strip(),
                    uses_accelerator=True,
                    device_memory_bytes=memory * 1024 * 1024,
                    managed_by_stage0=managed.get(pid) == token,
                )
            )
        mode = _one(smi, r"Performance Level: ([A-Za-z0-9_-]+)").strip()
        if mode != raw["files"]["power_dpm_force_performance_level"].strip():
            raise BW20TelemetryError("performance mode changed during observation")
        warnings = []
        if inventory:
            warnings.append("Global KFD inventory retained; per-device attribution is not proven.")
        if any(p.get("executable_status", "observed") != "observed" for p in raw["processes"]):
            warnings.append("Some process executable paths are unavailable; identities retained.")
        if raw["smi_device"]["stderr"] or raw["smi_processes"]["stderr"]:
            warnings.append("SMI emitted stderr; see the retained raw observation.")
        return TelemetrySnapshotV2.model_validate(
            dict(
                device=dict(
                    device_index=7,
                    temperature_c=float(_one(smi, rf"Sensor edge\) \(C\): {number}")),
                    hotspot_temperature_c=float(_one(smi, rf"Sensor junction\) \(C\): {number}")),
                    sclk_mhz=float(_one(smi, rf"sclk clock level: .*\({number}Mhz\)")),
                    mclk_mhz=float(_one(smi, rf"mclk clock level: .*\({number}Mhz\)")),
                    performance_level=mode,
                    power_w=float(_one(smi, rf"Average Graphics Package Power \(W\): {number}")),
                ),
                # Observing a process does not prove its cache protocol was executed.
                cache=dict(state="unknown", cleared_before_sample=False),
                background_processes=processes,
                collection_warnings=warnings,
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BW20TelemetryError("invalid or incomplete BW20 telemetry") from exc


class BW20TelemetryCollector:
    def __init__(self, *, runner, live_bindings: Callable[[], list[Mapping]]):
        require_endpoint(runner)
        self.runner, self.live_bindings = runner, live_bindings
        self.boot_id = None
        self.observations: list[dict] = []

    @staticmethod
    def _identities(bindings):
        identities = {}
        for binding in bindings:
            if (
                binding.get("resource_id") != RESOURCE
                or binding.get("worker_protocol")
                not in {
                    "hcuopt-stage0-torch-worker-v1",
                    "hcuopt-m1-allocator-worker-v1",
                }
            ):
                raise BW20TelemetryError("invalid managed process binding")
            child = binding["host_measured"]
            pid, token = child["host_pid"], child["start_token"]
            if pid in identities:
                raise BW20TelemetryError("duplicate managed process identity")
            identities[pid] = token
        return identities

    def collect(self):
        before = self.live_bindings()  # Fresh CID/start-token checks, not a PID cache.
        result = self.runner.run(("python3", "-c", HOST_TELEMETRY), timeout=40)
        if result.returncode or len(result.stdout) > 1048576 or len(result.stderr) > 65536:
            self.observations.append(
                {
                    "status": "collection_failed",
                    "returncode": result.returncode,
                    "stdout": result.stdout[:1048576].decode("utf-8", errors="replace"),
                    "stderr": result.stderr[:65536].decode("utf-8", errors="replace"),
                    "output_truncated": len(result.stdout) > 1048576 or len(result.stderr) > 65536,
                    "bindings_before": before,
                }
            )
            raise BW20TelemetryError("host telemetry collection failed")
        raw = json.loads(result.stdout)
        receipt = {"raw": raw, "bindings_before": before, "status": "unverified"}
        self.observations.append(receipt)
        boot_id = str(UUID(raw["boot_id"]))
        if self.boot_id is not None and boot_id != self.boot_id:
            raise BW20TelemetryError("host rebooted between telemetry observations")
        self.boot_id = boot_id
        after = self.live_bindings()
        receipt["bindings_after"] = after
        first, last = self._identities(before), self._identities(after)
        if first != last:
            raise BW20TelemetryError("managed identity changed across telemetry")
        snapshot = parse_snapshot(raw, last)
        receipt["status"] = "parsed_not_accepted"
        return snapshot.model_dump(mode="json")


class BW20TimingCleaner:
    """Close only this job's sessions, then independently observe resource health.

    No frequency mutation is implemented. Unexpected clock state is quarantined,
    not rewritten without authority. The Worker remains the lease settlement owner.
    """

    def __init__(self, *, factory, telemetry, fencing_token, initial_clock_state):
        if type(fencing_token) is not int or fencing_token < 1:
            raise ValueError("positive fencing token required")
        if (
            set(initial_clock_state) != {"mode", "sclk_mhz", "mclk_mhz"}
            or not initial_clock_state["mode"]
            or initial_clock_state["sclk_mhz"] <= 0
            or initial_clock_state["mclk_mhz"] <= 0
        ):
            raise ValueError("observed initial clock state required")
        self.factory, self.telemetry = factory, telemetry
        self.token, self.initial = fencing_token, dict(initial_clock_state)
        self.fenced = False

    def fence(self, resource_id, fencing_token):
        if resource_id != RESOURCE or type(fencing_token) is not int or fencing_token != self.token:
            raise ValueError("cleanup scope mismatch")
        self.fenced = getattr(self.factory, "finish", self.factory.force_close)()
        return {
            "fenced": self.fenced,
            "resource_id": RESOURCE,
            "fencing_token": self.token,
            "scope": "job_owned_sessions_only",
            "clock_mutation_performed": False,
        }

    def health_check(self, resource_id):
        if resource_id != RESOURCE:
            raise ValueError("health scope mismatch")
        healthy, reason = False, "session_cleanup_unconfirmed"
        observation = None
        if self.fenced:
            try:
                snapshot = self.telemetry.collect()
                observation = self.telemetry.observations[-1]["raw"]
                device = snapshot["device"]
                current = dict(
                    mode=device["performance_level"],
                    sclk_mhz=device["sclk_mhz"],
                    mclk_mhz=device["mclk_mhz"],
                )
                files = observation["files"]
                # Under fleet-managed ``auto`` the clocks are expected to move with
                # load.  This cleaner owns no clock mutation, so recovery means the
                # performance policy is still ``auto``; SCLK/MCLK remain diagnostic
                # evidence and must not quarantine an otherwise idle device.  For a
                # non-auto policy, retain the stricter exact-state restoration check.
                clock_policy_restored = (
                    current["mode"] == self.initial["mode"]
                    and (current["mode"] == "auto" or current == self.initial)
                )
                healthy = (
                    not snapshot["background_processes"]
                    and clock_policy_restored
                    and int(files["gpu_busy_percent"]) == 0
                    and 0 <= int(files["mem_info_vram_used"]) <= 4 * 1024 * 1024
                )
                reason = (
                    "observed_idle_restored_clock_policy"
                    if healthy
                    else "host_state_not_restored"
                )
            except Exception:
                reason = "host_health_unknown"
        return {
            "resource_id": RESOURCE,
            "healthy": healthy,
            "quarantined": not healthy,
            "reason": reason,
            "host_observation": observation,
            "clock_mutation_performed": False,
        }

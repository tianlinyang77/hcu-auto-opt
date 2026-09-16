# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import ast
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_stage0_harness import (
    HOST_CLOCK,
    BW20EvidenceMeasurementHarness,
    BW20HostClock,
    build_bw20_harness,
)
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.deployment.bw20_stage0_telemetry import (
    HOST_TELEMETRY,
    BW20TelemetryCollector,
    BW20TelemetryError,
    BW20TimingCleaner,
    parse_snapshot,
)
from hcuopt.measurement.harness import MeasurementSafetyError


def stat(pid, token=123):
    return f"{pid} (worker) S " + " ".join(["1"] * 18) + f" {token}"


def raw_observation(pids=()):
    return dict(
        schema_version="bw20-stage0-telemetry-v1",
        host="github-bw20",
        pci="0000:b1:00.0",
        boot_id=str(uuid4()),
        kfd_before=list(pids),
        kfd_after=list(pids),
        files=dict(
            numa_node="4",
            gpu_busy_percent="0",
            mem_info_vram_used="2207744",
            power_dpm_force_performance_level="auto",
        ),
        smi_device=dict(
            stderr="",
            stdout="\n".join(
                "HCU[7]\t: " + line
                for line in (
                    "Temperature (Sensor edge) (C): 40.0",
                    "Temperature (Sensor junction) (C): 44.0",
                    "sclk clock level: 1 (600Mhz)",
                    "mclk clock level: 0 (1800Mhz)",
                    "Performance Level: auto",
                    "Average Graphics Package Power (W): 135.0",
                )
            ),
        ),
        smi_processes=dict(
            stderr="",
            stdout="\n".join(f"PID: {pid}\n\tHCU Index: \n\tVRAM USED(MiB): 1\n" for pid in pids),
        ),
        processes=[
            dict(
                pid=p,
                stat_before=stat(p),
                stat_after=stat(p),
                executable="/usr/bin/python",
                comm="python\n",
            )
            for p in pids
        ],
    )


class Runner:
    host, user, port = "10.17.1.20", "github", 22

    def __init__(self, raw=None):
        self.raw = raw if raw is not None else raw_observation()
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append(argv)
        return SimpleNamespace(returncode=0, stderr=b"", stdout=json.dumps(self.raw).encode())


def test_host_scripts_remain_python36_compatible():
    ast.parse(HOST_TELEMETRY, feature_version=(3, 6))
    ast.parse(HOST_CLOCK, feature_version=(3, 6))


def test_unknown_attribution_is_not_silently_dropped_and_cache_is_not_invented():
    snapshot = parse_snapshot(raw_observation([4321]), {})
    assert snapshot.background_processes[0].process_id == 4321
    assert snapshot.background_processes[0].uses_accelerator
    assert not snapshot.background_processes[0].managed_by_stage0
    assert snapshot.cache.state == "unknown" and not snapshot.cache.cleared_before_sample
    assert snapshot.collection_warnings


def test_unreadable_executable_is_not_an_empty_process_inventory():
    raw = raw_observation([4321])
    raw["processes"][0].update(executable="unavailable", executable_status="PermissionError")
    snapshot = parse_snapshot(raw, {})
    assert snapshot.background_processes[0].executable == "unavailable"
    assert not snapshot.background_processes[0].managed_by_stage0
    assert any("unavailable" in warning for warning in snapshot.collection_warnings)


def test_failed_host_collection_retains_bounded_failure_receipt():
    runner = Runner()
    runner.run = lambda *args, **kwargs: SimpleNamespace(
        returncode=255, stdout=b"", stderr=b"connection lost"
    )
    collector = BW20TelemetryCollector(runner=runner, live_bindings=lambda: [])
    with pytest.raises(BW20TelemetryError):
        collector.collect()
    assert collector.observations[0]["returncode"] == 255
    assert collector.observations[0]["stderr"] == "connection lost"


def test_management_requires_current_start_token_not_just_pid():
    raw = raw_observation([4321])
    assert (
        parse_snapshot(raw, {4321: "linux-proc-startticks:123"})
        .background_processes[0]
        .managed_by_stage0
    )
    assert (
        not parse_snapshot(raw, {4321: "linux-proc-startticks:124"})
        .background_processes[0]
        .managed_by_stage0
    )


@pytest.mark.parametrize("damage", ["host", "pci", "inventory", "missing", "reused", "smi", "mode"])
def test_bad_observations_fail_closed(damage):
    raw = raw_observation([4321])
    if damage in ("host", "pci"):
        raw[damage] = "wrong"
    elif damage == "inventory":
        raw["kfd_after"] = []
    elif damage == "missing":
        raw["processes"] = []
    elif damage == "reused":
        raw["processes"][0]["stat_after"] = stat(4321, 999)
    elif damage == "smi":
        raw["smi_device"]["stdout"] += "\nHCU[6]: Performance Level: auto"
    elif damage == "mode":
        raw["files"]["power_dpm_force_performance_level"] = "manual"
    with pytest.raises(BW20TelemetryError):
        parse_snapshot(raw, {})


def test_collector_refreshes_bindings_and_preserves_unverified_raw_on_failure():
    count = 0

    def bindings():
        nonlocal count
        count += 1
        return [
            dict(
                resource_id=RESOURCE,
                worker_protocol="hcuopt-stage0-torch-worker-v1",
                host_measured=dict(host_pid=4321, start_token=f"linux-proc-startticks:{count}"),
            )
        ]

    collector = BW20TelemetryCollector(
        runner=Runner(raw_observation([4321])), live_bindings=bindings
    )
    with pytest.raises(BW20TelemetryError, match="changed across"):
        collector.collect()
    assert count == 2 and collector.observations[0]["status"] == "unverified"


def test_collector_accepts_m1_process_as_managed_only_with_host_start_token():
    bindings = [
        dict(
            resource_id=RESOURCE,
            worker_protocol="hcuopt-m1-allocator-worker-v1",
            host_measured=dict(
                host_pid=4321,
                start_token="linux-proc-startticks:123",
            ),
        )
    ]
    assert BW20TelemetryCollector._identities(bindings) == {
        4321: "linux-proc-startticks:123"
    }


@pytest.mark.parametrize(
    "case, expected_healthy",
    [
        ("idle", True),
        ("auto_clock", True),
        ("mode", False),
        ("process", False),
        ("busy", False),
        ("unknown", False),
        ("cleanup", False),
    ],
)
def test_cleaner_checks_hardware_after_owned_sessions_close(case, expected_healthy):
    runner = Runner(raw_observation([4321] if case == "process" else []))
    if case == "busy":
        runner.raw["files"]["gpu_busy_percent"] = "50"
    if case == "auto_clock":
        runner.raw["smi_device"]["stdout"] = runner.raw["smi_device"]["stdout"].replace(
            "600Mhz", "900Mhz"
        )
    if case == "mode":
        runner.raw["smi_device"]["stdout"] = runner.raw["smi_device"]["stdout"].replace(
            "Performance Level: auto", "Performance Level: manual"
        )
    if case == "unknown":
        runner.raw["host"] = "wrong"
    events = []

    def close():
        events.append("closed")
        return case != "cleanup"

    collector = BW20TelemetryCollector(runner=runner, live_bindings=lambda: [])
    cleaner = BW20TimingCleaner(
        factory=SimpleNamespace(force_close=close),
        telemetry=collector,
        fencing_token=7,
        initial_clock_state=dict(mode="auto", sclk_mhz=600, mclk_mhz=1800),
    )
    assert not cleaner.health_check(RESOURCE)["healthy"]
    assert not runner.calls
    with pytest.raises(ValueError):
        cleaner.fence(RESOURCE, 8)
    assert not events
    cleaner.fence(RESOURCE, 7)
    health = cleaner.health_check(RESOURCE)
    assert health["healthy"] is expected_healthy
    assert health["quarantined"] is (not expected_healthy)
    assert health["clock_mutation_performed"] is False


def test_remote_clock_rejects_reboots_and_nonadvancing_time():
    runner = Runner(dict(host="github-bw20", boot_id=str(uuid4()), monotonic_ns=100))
    clock = BW20HostClock(runner)
    assert clock.now_ns() == 100
    with pytest.raises(MeasurementSafetyError):
        clock.now_ns()
    runner.raw["monotonic_ns"] = 101
    runner.raw["boot_id"] = str(uuid4())
    with pytest.raises(MeasurementSafetyError):
        clock.now_ns()


def test_harness_uses_explicit_resource_and_trusted_lease_check():
    binding = SimpleNamespace(lease=SimpleNamespace(resource_id=RESOURCE))
    assert BW20EvidenceMeasurementHarness._formal_device_index(binding) == 7
    binding.lease.resource_id = "foreign:hcu:7"
    with pytest.raises(MeasurementSafetyError):
        BW20EvidenceMeasurementHarness._formal_device_index(binding)
    for guard in (None, True, lambda: False):
        with pytest.raises(MeasurementSafetyError):
            BW20EvidenceMeasurementHarness._require_live_lease(dict(assert_live_lease=guard))


def test_composition_does_not_start_timer_or_fake_cache_state():
    from tests.unit.test_bw20_stage0_runtime import TARGET

    starts = []
    checks = []
    context = dict(
        resource_id=RESOURCE,
        lease_scope="exclusive",
        fencing_token=7,
        lease_id=str(uuid4()),
        job_id=str(uuid4()),
        assert_live_lease=lambda: checks.append(True),
    )
    harness, factory = build_bw20_harness(
        target=TARGET,
        runner=Runner(),
        context=context,
        session_factory=lambda *args: starts.append(args),
    )
    assert checks and not starts and factory.sessions == []
    assert harness.collect_formal_telemetry().cache.state == "unknown"
    cleanup = harness.cleanup_formal_context(context)
    assert cleanup["fence"]["fenced"] and cleanup["health"]["healthy"]


def test_composed_fingerprint_uses_original_evidence_and_requires_no_torch(tmp_path):
    from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
    from tests.unit.test_bw20_stage0_runtime import TARGET

    runner = Runner()
    original_run = runner.run
    boot = runner.raw["boot_id"]
    tick = 100

    def run(argv, timeout):
        nonlocal tick
        if argv[-1] == HOST_CLOCK:
            tick += 1
            return SimpleNamespace(
                returncode=0,
                stderr=b"",
                stdout=json.dumps(
                    dict(host="github-bw20", boot_id=boot, monotonic_ns=tick)
                ).encode(),
            )
        return original_run(argv, timeout)

    runner.run = run
    context = dict(
        resource_id=RESOURCE,
        lease_scope="exclusive",
        fencing_token=7,
        lease_id=str(uuid4()),
        job_id=str(uuid4()),
        assert_live_lease=lambda: None,
    )
    harness, factory = build_bw20_harness(
        target=TARGET,
        runner=runner,
        context=context,
        session_factory=lambda *args: pytest.fail("fingerprint must not allocate HCU"),
    )
    adapter = Stage0MeasurementProbeAdapter(
        harness,
        TARGET,
        measurement_plan_factory=lambda *a: {},
        known_signal_detector=lambda *a: pytest.fail("D owns verdict"),
        null_signal_detector=lambda *a: pytest.fail("D owns verdict"),
    )
    payload = dict(
        probe_type="fingerprint",
        mode="formal",
        protocol_version="s0-g0-v2",
        adapter_profile=harness.provenance.profile,
        task_id=str(uuid4()),
        stage0_run_id=str(uuid4()),
        target_snapshot_id=str(uuid4()),
        workload_id="w1",
        _job_context=context,
    )
    output = adapter.run_probe(copy.copy(payload), tmp_path)
    assert output.raw_evidence_hash.startswith("sha256:")
    assert output.cleanup_evidence["health"]["healthy"]
    assert not factory.sessions
    assert not ({"accepted", "speedup", "stage0_passed"} & output.summary.keys())

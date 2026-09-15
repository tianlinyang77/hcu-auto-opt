# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import copy
import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_runtime_probe_formal_v2 as fixtures

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.contracts.platform_v1 import ExecutionRequest
from hcuopt.deployment import bw20_stage0_capabilities as capabilities
from hcuopt.deployment.bw20_capability_execution import BW20CapabilityExecutionAdapter
from hcuopt.deployment.bw20_stage0_deployment import (
    BW20MeasurementAdmission,
    compose_measurement_adapter,
)
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.deployment.bw20_stage0_profile import compose_stage0_profile
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.deployment.bw20_stage0_staging import freeze_controller
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType
from hcuopt.domain.errors import AdapterUnavailable, ExecutionSafetyError, TargetNotReady
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint
from tests.unit.test_bw20_stage0_adapter import Clock, Host, payload
from tests.unit.test_bw20_stage0_runtime import TARGET
from tests.unit.test_bw20_stage0_staging import source
from tests.unit.test_bw20_timing_session import raw_container


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "TARGET", TARGET)
    target, configuration, *_ = fixtures._formal_hotpatch_target_and_profile(tmp_path)
    target = target.model_copy(update={"blockers": []})
    configuration = configuration.model_copy(
        update={"profile": PROFILE, "target_fingerprint": target_fingerprint(target)}
    )
    p = payload()
    p.update(
        target=target.model_dump(mode="json"),
        target_fingerprint=target_fingerprint(target),
        probe_type="profiler",
        protocol_version="s0-g0-bw20-v2",
    )
    admission = BW20MeasurementAdmission(
        p["target_fingerprint"],
        load_registered_stage0_protocol(p["protocol_version"]).sha256,
        p["workload_id"],
        protocol_version=p["protocol_version"],
    )
    host = Host()
    runtime = capabilities.BW20RuntimeProbeAdapter(
        target=target,
        runner=host,
        configuration=configuration,
        configuration_sha256="sha256:"
        + hashlib.sha256(canonical_json_bytes(configuration)).hexdigest(),
        admission=admission,
        evidence_root=tmp_path,
    )
    # CPU fixture only. Real deployments must bind BW20PreparedInputGuard.
    runtime.input_guard = lambda: dict(
        schema_version="bw20-runtime-input-recheck-v1", input_integrity_passed=True,
        target_fingerprint=runtime.target_fingerprint,
        profile_sha256=runtime.configuration_sha256,
    )
    executor = BW20CapabilityExecutionAdapter(
        runner=host,
        target=target,
        configuration=configuration,
        context=p["_job_context"],
        evidence_root=tmp_path,
    )
    return p, target, configuration, admission, runtime, executor


@pytest.mark.parametrize("damage", ["missing", "failed", "target", "profile", "lease"])
def test_prepared_input_guard_blocks_before_docker(case, tmp_path, monkeypatch, damage):
    p, _, _, _, runtime, _ = case
    monkeypatch.setattr(capabilities.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capabilities.platform, "node", lambda: "github-bw20")
    record = runtime.input_guard()
    lease = {"lost": False}
    p["_job_context"]["assert_live_lease"] = lambda: False if lease["lost"] else None
    if damage == "missing":
        runtime.input_guard = None
    else:
        if damage == "failed":
            record["input_integrity_passed"] = False
        elif damage in {"target", "profile"}:
            record["target_fingerprint" if damage == "target" else "profile_sha256"] = "other"

        def check():
            if damage == "lease":
                lease["lost"] = True
            return record

        runtime.input_guard = check
    # Mutation above must not reach executor construction or allocate a job directory.
    monkeypatch.setattr(capabilities, "BW20CapabilityExecutionAdapter",
                        lambda **kw: pytest.fail("executor constructed before input acceptance"))
    with pytest.raises(MeasurementSafetyError):
        runtime.run_probe(p, tmp_path)
    assert not runtime._jobs


def request_for(case):
    p, target, cfg, *_ = case
    return ExecutionRequest(
        target_id=target.target_id,
        argv=list(cfg.profiler.tool_candidates[0].version_argv),
        working_directory=cfg.profiler.working_directory,
        environment=cfg.profiler.environment,
        mounts=list(cfg.profiler.mounts),
        container_image=target.inference_image.immutable_reference,
        resource_id=RESOURCE,
        fencing_token=p["_job_context"]["fencing_token"],
        lease_scope=LeaseScope.EXCLUSIVE,
        timeout_seconds=10,
    )


def test_c_scope_uses_frozen_command_and_single_logical_device(case):
    _, target, _, _, _, executor = case
    request = request_for(case)
    executor._validate_request(request, target)
    resources = executor._resource_arguments(request, target)
    assert "--device=/dev/dri/renderD135" in resources
    assert "--device=/dev/dri" not in resources
    env = executor._container_environment(request, target)
    assert {
        env[k] for k in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HSA_VISIBLE_DEVICES")
    } == {"0"}
    with pytest.raises(ExecutionSafetyError):
        executor._container_environment(
            request.model_copy(update={"environment": {"HIP_VISIBLE_DEVICES": "7"}}), target
        )


@pytest.mark.parametrize(
    "change",
    [
        dict(argv=["bash"]),
        dict(timeout_seconds=601),
        dict(resource_id="hcu-7"),
        dict(fencing_token=8),
        dict(environment={"LD_PRELOAD": "/evil"}),
    ],
)
def test_c_scope_rejects_request_expansion(case, change):
    with pytest.raises(ExecutionSafetyError):
        case[-1]._validate_request(request_for(case).model_copy(update=change), case[1])


def test_c_refuses_non_bw20_controller_and_unknown_cleanup(case, tmp_path, monkeypatch):
    p, _, _, _, runtime, _ = case
    monkeypatch.setattr(capabilities.platform, "system", lambda: "Windows")
    with pytest.raises(MeasurementSafetyError, match="BW20-local"):
        runtime.run_probe(p, tmp_path)
    assert not runtime.cleanup_probe(p)["health"]["healthy"]


def test_c_outputs_are_isolated_for_each_job(case, tmp_path):
    a = capabilities.isolate_outputs(case[2], tmp_path / "job-a")
    b = capabilities.isolate_outputs(case[2], tmp_path / "job-b")
    for phase in ("baseline", "candidate", "recovery"):
        assert (
            getattr(a.hotpatch, phase).evidence_directory_uri
            != getattr(b.hotpatch, phase).evidence_directory_uri
        )
    assert a.hotpatch.artifact == b.hotpatch.artifact == case[2].hotpatch.artifact
    with pytest.raises(ValueError, match="fresh"):
        capabilities.isolate_outputs(case[2], tmp_path / "job-a")


def test_seven_probe_composition_and_cleanup_ownership(case, tmp_path):
    p, target, _, admission, runtime, _ = case
    bundle = freeze_controller(source(tmp_path), tmp_path / "controller.tar")
    measurement = compose_measurement_adapter(
        target=target,
        runner=Host(),
        bundle=bundle,
        manifest_sha256=bundle.manifest_sha256,
        admission=admission,
        clock_session_factory=lambda *args: Clock(),
    )
    profile, registry = compose_stage0_profile(
        measurement=measurement, runtime=runtime, admission=admission
    )
    profile.validate_target(target)
    with pytest.raises(AdapterUnavailable):
        AdapterProfileCatalog().require(PROFILE)
    assert AdapterProfileCatalog((profile,)).require(PROFILE) is profile
    assert len(registry.stage0_probe._routes) == 7
    calls = []
    measurement.cleanup_probe = lambda p: calls.append("B") or {"fence": {}, "health": {}}
    runtime.cleanup_probe = lambda p: calls.append("C") or {"fence": {}, "health": {}}
    for probe in Stage0ProbeType:
        p["probe_type"] = probe.value
        registry.stage0_probe.cleanup_probe(p)
    assert calls.count("B") == 5 and calls.count("C") == 2
    for scope in ("optimization", "release", "framework_smoke"):
        with pytest.raises(TargetNotReady):
            profile.validate_target(target, scope=scope)
    with pytest.raises(TargetNotReady):
        profile.validate_target(target, evidence_resolved_blockers=frozenset({"anything"}))
    with pytest.raises(TargetNotReady, match="target revision"):
        profile.validate_target(target.model_copy(update={"target_id": "another-target"}))
    with pytest.raises(AdapterUnavailable):
        compose_stage0_profile(measurement=measurement, runtime=None, admission=admission)
    with pytest.raises(AdapterUnavailable):
        compose_stage0_profile(
            measurement=measurement,
            runtime=runtime,
            admission=replace(admission, workload_id="other"),
        )


def test_between_phase_health_does_not_stop_next_phase(case):
    executor = case[-1]
    executor.confirm_empty = lambda: True
    cleaner = capabilities.BW20CapabilityCleaner(
        factory=executor,
        telemetry=capabilities.BW20TelemetryCollector(runner=Host(), live_bindings=lambda: []),
        fencing_token=7,
        initial_clock_state=dict(mode="auto", sclk_mhz=600, mclk_mhz=1800),
    )
    assert cleaner.health_check(RESOURCE)["healthy"]
    assert not executor.stopped.is_set()
    assert cleaner.fence(RESOURCE, 7)["fenced"]
    assert executor.stopped.is_set()


def install_transport(executor, request):
    cid = "a" * 64
    raw = raw_container()
    raw["Image"] = IMAGE_ID
    raw["Config"].update(
        User="65534:65534",
        Entrypoint=request.argv[:1],
        Cmd=request.argv[1:],
        WorkingDir=request.working_directory,
        Env=[
            f"{k}={v}" for k, v in executor._container_environment(request, executor.target).items()
        ],
    )
    raw["Mounts"] = [
        dict(Type="bind", Source=m.source, Destination=m.target, RW=not m.read_only)
        for m in request.mounts
    ]
    raw["HostConfig"].update(
        Memory=16 * 1024**3,
        MemorySwap=16 * 1024**3,
        PidsLimit=512,
        ShmSize=2 * 1024**3,
        Tmpfs={"/tmp": "rw,exec,nosuid,nodev,size=4g"},
    )
    transport = SimpleNamespace(owned={}, removed=[])

    def create(plan, timeout):
        transport.owned[cid] = plan
        return cid

    transport._create = create
    transport._ownership = lambda *args: None  # Original transport ownership has its own tests.
    transport.inspect = lambda *args: None if transport.removed else copy.deepcopy(raw)
    transport.remove = lambda *args: transport.removed.append(cid)
    executor.transport = transport
    return raw, transport, cid


@pytest.mark.parametrize(
    "damage", [None, "devices", "memory", "privileged", "pid", "command", "environment", "user"]
)
def test_create_inspect_before_start_and_exact_cid_cleanup(case, damage):
    executor, request = case[-1], request_for(case)
    raw, transport, cid = install_transport(executor, request)
    if damage == "devices":
        raw["HostConfig"]["Devices"].append(
            dict(PathOnHost="/dev/dri", PathInContainer="/dev/dri", CgroupPermissions="rwm")
        )
    elif damage == "memory":
        raw["HostConfig"]["Memory"] = 0
    elif damage == "privileged":
        raw["HostConfig"]["Privileged"] = True
    elif damage == "pid":
        raw["HostConfig"]["PidMode"] = "host"
    elif damage == "command":
        raw["Config"]["Cmd"] = ["evil"]
    elif damage == "environment":
        raw["Config"]["Env"] = ["HIP_VISIBLE_DEVICES=7"]
    elif damage == "user":
        raw["Config"]["User"] = "0:0"

    def start():
        return executor._docker_command(
            request, case[1], container_name="fixture", resource_id=RESOURCE, fencing_token=7
        )

    if damage:
        with pytest.raises(ExecutionSafetyError):
            start()
        assert transport.removed == [cid]
    else:
        assert start() == ("docker", "start", "--attach", cid)
        assert not transport.removed
        assert executor.force_close()


def test_unconfirmed_create_never_reports_released(case):
    executor, request = case[-1], request_for(case)
    _, transport, _ = install_transport(executor, request)

    def fail(*args):
        raise RuntimeError("create reply lost")

    transport._create = fail
    with pytest.raises(RuntimeError):
        executor._docker_command(
            request, case[1], container_name="fixture", resource_id=RESOURCE, fencing_token=7
        )
    assert executor.force_close() is False


def test_running_c_lease_loss_cleans_owned_container_and_reaps_client(case):
    executor, request = case[-1], request_for(case)
    _, transport, cid = install_transport(executor, request)
    executor._docker_command(
        request, case[1], container_name="fixture", resource_id=RESOURCE, fencing_token=7
    )
    calls = []
    process = SimpleNamespace(
        poll=lambda: None,
        kill=lambda: calls.append("kill"),
        wait=lambda timeout: calls.append(("wait", timeout)) or 0,
    )
    executor.runner.runner = SimpleNamespace(popen=lambda *a, **kw: process)
    client = executor.runner.popen(("docker", "start", "--attach", cid))

    def lost():
        raise ExecutionSafetyError("lease expired")

    executor.fencing_guard.context["assert_live_lease"] = lost
    with pytest.raises(ExecutionSafetyError, match="lease expired"):
        client.poll()
    assert transport.removed == [cid]
    assert calls == ["kill", ("wait", 5)]
    assert executor.stopped.is_set()
    with pytest.raises(ExecutionSafetyError):
        executor.runner.popen(("docker", "start", "--attach", cid))


def test_c_timeout_cleanup_never_removes_by_container_name(case):
    executor, request = case[-1], request_for(case)
    _, transport, cid = install_transport(executor, request)
    executor._docker_command(
        request, case[1], container_name="fixture", resource_id=RESOURCE, fencing_token=7
    )
    removed = []

    def remove(actual_cid, timeout):
        removed.append(actual_cid)
        transport.removed.append(actual_cid)

    transport.remove = remove
    receipt = executor._terminate_owned_container(
        SimpleNamespace(container_name="fixture"), "timeout"
    )
    assert removed == [cid]
    assert receipt == dict(requested=True, reason="timeout", container_removed=True)


@pytest.mark.parametrize("probe", ["profiler", "hotpatch"])
def test_original_c_adapter_publishes_job_bound_evidence(case, tmp_path, monkeypatch, probe):
    p, _, _, _, runtime, _ = case
    p["probe_type"] = probe
    monkeypatch.setattr(capabilities.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capabilities.platform, "node", lambda: "github-bw20")
    created = []

    class FixtureExecutor:
        def __init__(self, *, configuration, **kwargs):
            self.stopped = threading.Event()
            self.transport = SimpleNamespace(owned={})
            self.calls = 0
            self.overlay = fixtures._FormalOverlayExecutor(
                [
                    file_uri_to_path(getattr(configuration.hotpatch, phase).evidence_directory_uri)
                    for phase in ("baseline", "candidate", "recovery")
                ],
                file_uri_to_path(configuration.hotpatch.baseline.implementation_source_uri),
                configuration.hotpatch.artifact.content_hash,
            )
            self.overlay.provenance = self.overlay.provenance.model_copy(
                update={"profile": PROFILE}
            )
            created.append(self)

        def execute(self, request, target, output_dir):
            assert not self.stopped.is_set()
            if probe == "hotpatch":
                return self.overlay.execute(request, target, output_dir)
            contents = [
                b"rocprofiler-sdk 0.6\n",
                b"Kernel_Name,Start_Timestamp,End_Timestamp,GPU_ID,Queue_ID\nadd,100,1000,0,1\n",
            ]
            path = tmp_path / f"profiler-{self.calls}.txt"
            path.write_bytes(contents[self.calls])
            self.calls += 1
            return SimpleNamespace(exit_code=0, stdout_uri=path.as_uri(), stderr_uri=None)

        def confirm_empty(self):
            return True

        def force_close(self):
            self.stopped.set()
            return True

    monkeypatch.setattr(capabilities, "BW20CapabilityExecutionAdapter", FixtureExecutor)
    result = runtime.run_probe(p, tmp_path)
    envelope = json.loads(file_uri_to_path(result.raw_evidence_uri).read_text())
    assert envelope["binding"]["target_id"] == "bw20-sglang-0.5.12"
    assert envelope["binding"]["lease"]["resource_id"] == RESOURCE
    assert result.cleanup_evidence["health"]["healthy"]
    assert created[0].stopped.is_set()
    diagnostic = json.loads(
        file_uri_to_path(result.cleanup_evidence["diagnostics"]["uri"]).read_text()
    )
    assert diagnostic["primary_evidence"]["sha256"] == result.raw_evidence_hash
    if probe == "hotpatch":
        assert created[0].overlay.ordinal == 3
        assert all(
            name in envelope for name in ("baseline_state", "candidate_state", "recovery_state")
        )

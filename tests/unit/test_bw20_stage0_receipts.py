# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.evaluation.bw20_stage0_receipts import RESOURCE, verify_bw20_diagnostics
from hcuopt.evaluation.evidence_reader import EvidenceReadError
from hcuopt.evaluation.stage0_verifier import _proc_start_token
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.models import MeasurementEvidenceV2
from hcuopt.source_hash import file_uri_to_path
from tests.unit.test_stage0_measurement_v2 import LifecycleRecorder, _adapter, _payload
from tests.unit.test_stage0_verifier import _PortableVerifierTestReader


def test_original_seven_probe_verifier_requires_bw20_sidecar(tmp_path):
    from tests.unit.test_bw20_stage0_runtime import TARGET
    from tests.unit.test_stage0_verifier import _build_suite

    suite = _build_suite(tmp_path, target=TARGET, protocol_version="s0-g0-v2")
    with pytest.raises(EvidenceReadError, match="missing BW20 diagnostic evidence reference"):
        suite.verify()


def stat(pid, parent, start):
    return f"{pid} (fixture) S {parent} " + "0 " * 17 + str(start)


def session(pid, token, index, lease):
    cid = f"{index:064x}"
    ready = dict(
        protocol="hcuopt-stage0-torch-worker-v1",
        event="ready",
        process_id=pid,
        observer_process_id=1,
        proc_stat_line=stat(pid, 1, token),
        device_identity=dict(pci="0000:b1:00.0", architecture="gfx936", logical_device_index=0),
    )
    s = dict(
        container_id=cid,
        cleanup_complete=True,
        start=ready,
        exit={**ready, "event": "closing", "waitpid_result_pid": pid, "wait_status": 0},
        cache_receipts=[],
    )

    def record(host_pid, parent, local_pid, ticks):
        return dict(
            host_pid=host_pid,
            parent_pid=parent,
            start_token=f"linux-proc-startticks:{ticks}",
            namespace=f"pid:[{index}]",
            nspid=[host_pid, local_pid],
            stat_before=stat(host_pid, parent, ticks),
            stat_after=stat(host_pid, parent, ticks),
            status=f"NSpid:\t{host_pid}\t{local_pid}\n",
            cgroup=f"0::/docker/{cid}\n",
        )

    init = record(index * 100 + 1000, 99, 1, 100)
    child = record(index * 100 + 1001, init["host_pid"], pid, token)
    binding = dict(
        schema_version="bw20-stage0-process-binding-v1",
        container_id=cid,
        container_ready=ready,
        resource_id=RESOURCE,
        fencing_token=lease.fencing_token,
        worker_protocol="hcuopt-stage0-torch-worker-v1",
        host_controller=init,
        host_measured=child,
        final_controller=copy.deepcopy(init),
        final_measured=copy.deepcopy(child),
    )
    s["bindings"] = [binding]
    return s


@pytest.fixture
def case(tmp_path, request):
    class Recorder(LifecycleRecorder):
        @staticmethod
        def _proc_stat_line(pid, start_ticks):
            return stat(pid, 1, start_ticks)

        def _record(self, *args, **kwargs):
            return super()._record(*args, **kwargs).model_copy(update={"observer_process_id": 1})

    producer = _adapter()
    producer.harness.lifecycle_recorder = Recorder()
    probe_type = getattr(request, "param", "noise")
    output = producer.run_probe(_payload(probe_type), tmp_path)
    evidence = MeasurementEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )
    lease = evidence.binding.lease.model_copy(update={"resource_id": RESOURCE})
    evidence = evidence.model_copy(
        update={"binding": evidence.binding.model_copy(update={"lease": lease})}
    )
    cleanup = dict(
        fence=dict(resource_id=RESOURCE, fencing_token=lease.fencing_token, fenced=True),
        health=dict(resource_id=RESOURCE, healthy=True),
    )
    reference = SimpleNamespace(
        raw_evidence_uri=output.raw_evidence_uri,
        raw_evidence_hash=output.raw_evidence_hash,
        cleanup_evidence=cleanup,
    )
    sessions = [session(2, 99999, 999, lease)]
    for restart in range(evidence.plan.restart_count):
        samples = [s for s in evidence.samples if s.restart_ordinal == restart]
        first = samples[0]
        s = session(
            first.process_id, int(first.process_start_token.split(":")[-1]), restart + 1, lease
        )
        for ordinal, sample in enumerate(samples, 1):
            receipt = dict(
                schema_version="stage0-allocator-cache-receipt-v1",
                process_id=sample.process_id,
                sequence=ordinal,
                method="torch.cuda.synchronize/empty_cache/synchronize",
                scope="pytorch_unused_allocator_blocks_only",
                hardware_cache_flushed=False,
                started_monotonic_ns=sample.started_monotonic_ns - 2,
                finished_monotonic_ns=sample.started_monotonic_ns - 1,
            )
            response = {
                k: getattr(sample, k)
                for k in (
                    "process_id",
                    "segment",
                    "batch_iterations",
                    "started_monotonic_ns",
                    "finished_monotonic_ns",
                    "started_device_ticks",
                    "finished_device_ticks",
                )
            }
            response["cache_receipt"] = receipt
            s["cache_receipts"].append(
                dict(
                    receipt=receipt,
                    sample=response,
                    request=dict(
                        op="measure",
                        probe_type=probe_type,
                        segment=sample.segment,
                        iterations=sample.batch_iterations,
                    ),
                    host_binding=s["bindings"][0],
                )
            )
        sessions.append(s)
    diagnostic = dict(
        schema_version="bw20-stage0-job-diagnostics-v1",
        resource_id=RESOURCE,
        job_id=str(uuid4()),
        lease_id=str(lease.lease_id),
        fencing_token=lease.fencing_token,
        primary_evidence=dict(uri=reference.raw_evidence_uri, sha256=reference.raw_evidence_hash),
        cleanup_evidence=copy.deepcopy(cleanup),
        sessions=sessions,
    )
    reader = _PortableVerifierTestReader(tmp_path)
    original_read = reader.read

    def run():
        encoded = canonical_json_bytes(diagnostic)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        reference.cleanup_evidence["diagnostics"] = dict(
            uri="file:///diagnostics.json", sha256=digest
        )

        def read(uri, pin):
            if uri == "file:///diagnostics.json":
                assert pin == digest
                return copy.deepcopy(diagnostic)
            return original_read(uri, pin)

        reader.read = read
        return verify_bw20_diagnostics(evidence, reference, reader, start_token=_proc_start_token)

    return diagnostic, reference, run


@pytest.mark.parametrize("case", ["timer", "noise", "known_signal", "null_signal"], indirect=True)
def test_d_recomputes_complete_sample_receipt_binding(case):
    _, _, run = case
    assert run().sha256.startswith("sha256:")


@pytest.mark.parametrize(
    "damage",
    [
        "primary",
        "lease",
        "cleanup",
        "missing",
        "extra",
        "sequence",
        "request",
        "sample",
        "clock",
        "hardware",
        "namespace",
        "cgroup",
        "reap",
        "lifecycle",
        "final_status",
        "final_cgroup",
        "final_parent",
        "final_start",
    ],
)
def test_d_rejects_tampering_even_when_sidecar_hash_is_updated(case, damage):
    raw, _, run = case
    s = raw["sessions"][1]
    r = s["cache_receipts"][0]
    if damage == "primary":
        raw["primary_evidence"]["sha256"] = "sha256:" + "f" * 64
    elif damage == "lease":
        raw["lease_id"] = str(uuid4())
    elif damage == "cleanup":
        raw["cleanup_evidence"]["health"]["healthy"] = False
    elif damage == "missing":
        s["cache_receipts"].pop()
    elif damage == "extra":
        s["cache_receipts"].append(copy.deepcopy(r))
    elif damage == "sequence":
        r["receipt"]["sequence"] = True
    elif damage == "request":
        r["request"]["iterations"] = 1
    elif damage == "sample":
        r["sample"]["started_device_ticks"] += 1
    elif damage == "clock":
        r["receipt"]["finished_monotonic_ns"] += 100
    elif damage == "hardware":
        r["receipt"]["hardware_cache_flushed"] = True
    elif damage == "namespace":
        s["bindings"][0]["host_measured"]["nspid"][1] += 1
    elif damage == "cgroup":
        s["bindings"][0]["host_measured"]["cgroup"] = "0::/other\n"
    elif damage == "reap":
        s["exit"]["wait_status"] = 256
    elif damage == "lifecycle":
        s["exit"]["proc_stat_line"] = s["exit"]["proc_stat_line"].replace("fixture", "other")
    elif damage == "final_status":
        s["bindings"][0]["final_measured"]["status"] = "NSpid:\t999\t2\n"
    elif damage == "final_cgroup":
        s["bindings"][0]["final_measured"]["cgroup"] = "0::/other\n"
    elif damage in ("final_parent", "final_start"):
        final = s["bindings"][0]["final_measured"]
        ticks = int(final["start_token"].split(":")[-1])
        final["stat_after"] = stat(
            final["host_pid"],
            final["parent_pid"] + (damage == "final_parent"),
            ticks + (damage == "final_start"),
        )
    with pytest.raises(EvidenceReadError):
        run()

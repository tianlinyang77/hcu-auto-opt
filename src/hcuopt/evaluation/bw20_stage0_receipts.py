# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Independent D-side consistency checks for BW20 job diagnostics.

No device calls or producer-validator imports. Producer pass flags alone are
insufficient: recorded identities and sample receipts must agree with the primary
evidence. This supplements the original seven-probe D verifier; allocator receipts
do not establish a hardware cache-flush policy.
"""

from __future__ import annotations

import re
from uuid import UUID

from hcuopt.evaluation.evidence_reader import EvidenceReadError
from hcuopt.measurement.models import MeasurementEvidenceV2, RawEvidenceFileV2

RESOURCE = "bw20-sglang-0.5.12:hcu:7"


def _require(condition, message):
    if not condition:
        raise EvidenceReadError("bw20_receipt_invalid", message)


def _int(value):
    return type(value) is int


def _host_binding(binding, session, lease, start_token):
    cid, ready = session["container_id"], session["start"]
    _require(
        binding["schema_version"] == "bw20-stage0-process-binding-v1"
        and binding["container_id"] == cid
        and binding["container_ready"] == ready
        and binding["resource_id"] == lease.resource_id
        and _int(binding["fencing_token"])
        and binding["fencing_token"] == lease.fencing_token
        and binding["worker_protocol"] == "hcuopt-stage0-torch-worker-v1",
        "host binding belongs to another session/lease",
    )
    init, child = binding["host_controller"], binding["host_measured"]
    for name, record, local_pid in (
        ("controller", init, 1),
        ("measured", child, ready["process_id"]),
    ):
        pid = record["host_pid"]
        _require(_int(pid) and pid > 1, "invalid host PID")
        _require(record["nspid"] == [pid, local_pid], "wrong namespace PID mapping")
        _require(
            re.fullmatch(r"pid:\[[0-9]+\]", record["namespace"]) is not None,
            "invalid PID namespace",
        )
        for stat in (record["stat_before"], record["stat_after"]):
            _require(
                start_token(stat.strip(), pid) == record["start_token"],
                "host start token does not follow procfs",
            )
            _require(
                int(stat[stat.rindex(") ") + 2 :].split()[1]) == record["parent_pid"],
                "host parent does not follow procfs",
            )
        status = re.findall(r"^NSpid:\s*([0-9 \t]+)$", record["status"], re.M)
        _require(
            len(status) == 1 and [int(v) for v in status[0].split()] == [pid, local_pid],
            "namespace mapping does not follow raw status",
        )
        paths = [
            line.split(":", 2)[2] for line in record["cgroup"].splitlines() if line.count(":") == 2
        ]
        _require(
            any(
                p.endswith("/docker/" + cid) or p.endswith("/docker-" + cid + ".scope")
                for p in paths
            ),
            "raw cgroup belongs to another container",
        )
        final = binding["final_" + name]
        _require(
            all(
                final[key] == record[key]
                for key in ("host_pid", "start_token", "namespace", "nspid", "parent_pid")
            ),
            "host identity snapshot changed",
        )
        for stat in (final["stat_before"], final["stat_after"]):
            _require(
                start_token(stat.strip(), pid) == record["start_token"],
                "final host start token does not follow procfs",
            )
            _require(
                int(stat[stat.rindex(") ") + 2 :].split()[1]) == record["parent_pid"],
                "final host parent does not follow procfs",
            )
        status = re.findall(r"^NSpid:\s*([0-9 \t]+)$", final["status"], re.M)
        _require(
            len(status) == 1 and [int(v) for v in status[0].split()] == [pid, local_pid],
            "final namespace mapping does not follow raw status",
        )
        paths = [
            line.split(":", 2)[2] for line in final["cgroup"].splitlines() if line.count(":") == 2
        ]
        _require(
            any(
                p.endswith("/docker/" + cid) or p.endswith("/docker-" + cid + ".scope")
                for p in paths
            ),
            "final raw cgroup belongs to another container",
        )
    _require(
        child["parent_pid"] == init["host_pid"] and child["namespace"] == init["namespace"],
        "measured child not bound to its controller",
    )
    _require(
        child["start_token"] == start_token(ready["proc_stat_line"], ready["process_id"]),
        "container and host start token differ",
    )


def verify_bw20_diagnostics(evidence, reference, reader, *, start_token):
    """Read hashed sidecar through the original trusted-root reader and bind every sample."""
    try:
        pointer = reference.cleanup_evidence.get("diagnostics")
        _require(isinstance(pointer, dict), "missing BW20 diagnostic evidence reference")
        link = RawEvidenceFileV2.model_validate(pointer)
        raw = reader.read(link.uri, link.sha256)
        lease = evidence.binding.lease
        _require(
            raw["schema_version"] == "bw20-stage0-job-diagnostics-v1"
            and raw["resource_id"] == RESOURCE == lease.resource_id
            and str(UUID(raw["lease_id"])) == str(lease.lease_id)
            and _int(raw["fencing_token"])
            and raw["fencing_token"] == lease.fencing_token,
            "diagnostics belong to another lease",
        )
        UUID(raw["job_id"])
        _require(
            raw["primary_evidence"]
            == {"uri": reference.raw_evidence_uri, "sha256": reference.raw_evidence_hash},
            "diagnostics do not bind this primary evidence",
        )
        for key in ("fence", "health"):
            _require(
                raw["cleanup_evidence"][key] == reference.cleanup_evidence[key],
                "diagnostic cleanup differs from recorded cleanup",
            )
        sessions = raw.get("sessions")
        _require(isinstance(sessions, list), "missing session inventory")
        if not isinstance(evidence, MeasurementEvidenceV2):
            _require(not sessions, "fingerprint unexpectedly contains timing sessions")
            return link
        _require(
            len(sessions) == evidence.plan.restart_count + 1,
            "expected one calibration session and every restarted workload",
        )
        remaining = {}
        for sample in evidence.samples:
            remaining.setdefault((sample.process_id, sample.process_start_token), []).append(sample)
        seen, calibrators = set(), 0
        for session in sessions:
            cid = session["container_id"]
            _require(
                isinstance(cid, str)
                and re.fullmatch(r"[0-9a-f]{64}", cid) is not None
                and cid not in seen
                and session["cleanup_complete"] is True,
                "duplicate, invalid or uncleared session",
            )
            seen.add(cid)
            ready, exit_record = session["start"], session["exit"]
            pid = ready["process_id"]
            _require(
                _int(pid)
                and pid > 1
                and _int(ready["observer_process_id"])
                and ready["observer_process_id"] == 1
                and ready["protocol"] == "hcuopt-stage0-torch-worker-v1"
                and ready["device_identity"]
                == {"pci": "0000:b1:00.0", "architecture": "gfx936", "logical_device_index": 0},
                "invalid measured child identity",
            )
            token = start_token(ready["proc_stat_line"], pid)
            _require(
                isinstance(exit_record, dict)
                and exit_record["event"] == "closing"
                and _int(exit_record["process_id"])
                and _int(exit_record["waitpid_result_pid"])
                and _int(exit_record["observer_process_id"])
                and exit_record["observer_process_id"] == 1
                and exit_record["process_id"] == pid
                and exit_record["waitpid_result_pid"] == pid
                and _int(exit_record["wait_status"])
                and exit_record["wait_status"] == 0
                and start_token(exit_record["proc_stat_line"], pid) == token,
                "session missing its successful raw child reap",
            )
            _require(bool(session["bindings"]), "missing host process bindings")
            for binding in session["bindings"]:
                _host_binding(binding, session, lease, start_token)
            samples = remaining.pop((pid, token), None)
            receipts = session["cache_receipts"]
            if samples is None:
                calibrators += 1
                _require(not receipts, "unbound session has measured receipts")
                continue
            for phase, observed in (("before_restart", ready), ("after_restart", exit_record)):
                matches = [
                    o
                    for o in evidence.observations
                    if o.phase == phase and o.restart_ordinal == samples[0].restart_ordinal
                ]
                _require(len(matches) == 1, "missing original workload lifecycle")
                ref = matches[0].process_lifecycle_record
                lifecycle = reader.read(ref.uri, ref.sha256)
                for key in ("process_id", "observer_process_id", "proc_stat_line"):
                    _require(lifecycle[key] == observed[key], "diagnostics differ from lifecycle")
                if phase == "after_restart":
                    _require(
                        lifecycle["waitpid_result_pid"] == observed["waitpid_result_pid"]
                        and lifecycle["wait_status"] == observed["wait_status"],
                        "diagnostic reap differs from lifecycle",
                    )
            _require(len(receipts) == len(samples), "missing or extra per-sample cache receipts")
            previous_end = -1
            for ordinal, (record, sample) in enumerate(zip(receipts, samples, strict=True), 1):
                receipt, response = record["receipt"], record["sample"]
                _require(
                    record["request"]
                    == {
                        "op": "measure",
                        "probe_type": evidence.binding.probe_type.value,
                        "segment": sample.segment,
                        "iterations": sample.batch_iterations,
                    },
                    "cache receipt request differs from the sampled workload",
                )
                for field in (
                    "process_id",
                    "segment",
                    "batch_iterations",
                    "started_monotonic_ns",
                    "finished_monotonic_ns",
                    "started_device_ticks",
                    "finished_device_ticks",
                ):
                    _require(
                        response[field] == getattr(sample, field), "cache receipt sample mismatch"
                    )
                _require(response["cache_receipt"] == receipt, "receipt differs from raw response")
                _require(
                    receipt["schema_version"] == "stage0-allocator-cache-receipt-v1"
                    and _int(receipt["process_id"])
                    and receipt["process_id"] == pid
                    and _int(receipt["sequence"])
                    and receipt["sequence"] == ordinal
                    and receipt["method"] == "torch.cuda.synchronize/empty_cache/synchronize"
                    and receipt["scope"] == "pytorch_unused_allocator_blocks_only"
                    and receipt["hardware_cache_flushed"] is False,
                    "invalid allocator cache operation or sequence",
                )
                begin, end = receipt["started_monotonic_ns"], receipt["finished_monotonic_ns"]
                _require(
                    _int(begin)
                    and _int(end)
                    and max(0, previous_end) <= begin < end <= sample.started_monotonic_ns,
                    "cache operation not ordered before its sample",
                )
                previous_end = sample.finished_monotonic_ns
                _host_binding(record["host_binding"], session, lease, start_token)
        _require(not remaining and calibrators == 1, "session/workload identities are incomplete")
        return link
    except EvidenceReadError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError) as exc:
        raise EvidenceReadError("bw20_receipt_invalid", "incomplete BW20 raw diagnostics") from exc

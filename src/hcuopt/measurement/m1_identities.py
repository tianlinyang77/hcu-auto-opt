# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Content-derived M1 identities consumed by Formal phase isolation.

Do not salt these identities with a Round, Candidate, phase, path or report ID:
relabeling the same acquisitions must not bypass the reuse guard.
"""

from __future__ import annotations

import hashlib

from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.m1_models import M1MeasurementEvidence


def m1_isolation_hashes(evidence: M1MeasurementEvidence) -> dict[str, str]:
    """Project canonical sets from raw acquisitions, never from caller metadata.

    Whole-set identities support the existing registry, not partial-overlap
    detection. Independent verification of raw records remains D's responsibility.
    """
    baseline_records = sorted(
        {
            sample.device_event_record.sha256
            for acquisition in evidence.acquisitions
            if acquisition.arm == "baseline"
            for sample in acquisition.samples
        }
    )
    processes = sorted(
        {(item.process_id, item.process_start_token) for item in evidence.acquisitions}
    )
    namespaces = sorted({item.activation.cache_namespace_hash for item in evidence.acquisitions})
    sets = {
        "baseline_sample_set_hash": baseline_records,
        "process_identity_set_hash": processes,
        "cache_namespace_set_hash": namespaces,
    }
    return {
        name: "sha256:"
        + hashlib.sha256(
            canonical_json_bytes(
                {"schema_version": "m1-isolation-set-v1", "kind": name, "members": members}
            )
        ).hexdigest()
        for name, members in sets.items()
    }

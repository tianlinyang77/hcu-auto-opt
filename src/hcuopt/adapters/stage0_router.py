from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hcuopt.adapters.interfaces import Stage0ProbeAdapter
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.stage0 import Stage0ProbeOutput


class RoutedStage0ProbeAdapter:
    """Expose one Stage 0 profile while preserving probe ownership boundaries."""

    def __init__(
        self,
        routes: Mapping[Stage0ProbeType, Stage0ProbeAdapter],
        *,
        profile: str,
    ) -> None:
        normalized = {Stage0ProbeType(key): adapter for key, adapter in routes.items()}
        required = frozenset(Stage0ProbeType)
        missing = sorted(item.value for item in required - normalized.keys())
        unexpected = sorted(item.value for item in normalized.keys() - required)
        if missing or unexpected:
            raise ValueError(
                "Stage 0 probe routes must cover the public contract exactly; "
                f"missing={missing}, unexpected={unexpected}"
            )
        for probe_type, adapter in normalized.items():
            provenance = getattr(adapter, "provenance", None)
            if not isinstance(provenance, AdapterProvenance):
                raise ValueError(f"route {probe_type.value} has no valid adapter provenance")
            if provenance.capability != "stage0_probe":
                raise ValueError(
                    f"route {probe_type.value} does not provide stage0_probe capability"
                )
            if provenance.implementation_kind != "real":
                raise ValueError(f"route {probe_type.value} must use a real adapter")

        self._routes = MappingProxyType(normalized)
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="stage0_probe",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def run_probe(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> Stage0ProbeOutput:
        probe_type = Stage0ProbeType(str(payload.get("probe_type")))
        delegate = self._routes[probe_type]
        output = delegate.run_probe(payload, output_dir)
        summary = dict(output.summary)
        summary["probe_adapter_provenance"] = delegate.provenance.model_dump(mode="json")
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=output.raw_evidence_uri,
            raw_evidence_hash=output.raw_evidence_hash,
            cleanup_evidence=output.cleanup_evidence,
            synthetic=output.synthetic,
        )

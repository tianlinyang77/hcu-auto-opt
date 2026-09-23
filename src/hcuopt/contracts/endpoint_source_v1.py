# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Endpoint source references, not admission or permission to execute.

The current v1 Endpoint API remains M1-only. A Formal reference must be checked
against durable Round, D result, artifact and signoff records before a future
endpoint admission route accepts it. Never coerce it into signed_m1.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, TypeAdapter

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.endpoint_models import SignedM1EvidenceReference


class M1EndpointSource(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["signed_m1"]
    evidence: SignedM1EvidenceReference


class FormalEndpointSource(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["signed_formal_round"]
    task_id: UUID
    round_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    round_evidence_bundle_id: UUID
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    round_signoff_id: UUID
    decision_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    automatic_release_allowed: Literal[False] = False


EndpointSourceReference = Annotated[
    M1EndpointSource | FormalEndpointSource, Field(discriminator="kind"),
]
ENDPOINT_SOURCE_ADAPTER = TypeAdapter(EndpointSourceReference)

# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hcuopt.adapters.profiles import (
    BW20_MANUAL_CANDIDATE_PROFILE,
    MANUAL_CANDIDATE_CAPABILITIES,
    REAL_MANUAL_CANDIDATE_PROFILE,
    AdapterProfileCatalog,
)
from hcuopt.adapters.real_profile import compose_m1_registry, compose_nmz36_m1_registry
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import AdapterUnavailable


@dataclass
class _Adapter:
    provenance: AdapterProvenance


def _adapter(capability: str, *, profile: str = REAL_MANUAL_CANDIDATE_PROFILE) -> _Adapter:
    return _Adapter(
        AdapterProvenance(
            profile=profile,
            capability=capability,
            adapter_name=f"Fixture{capability.title()}",
            adapter_version="1",
            implementation_kind="real",
        )
    )


def _registries(profile: str = REAL_MANUAL_CANDIDATE_PROFILE):
    source = AdapterRegistry(
        profile=profile,
        candidate_builder=_adapter("candidate_builder", profile=profile),
    )
    correctness = AdapterRegistry(
        profile=profile,
        kernel_correctness=_adapter("kernel_correctness", profile=profile),
    )
    measurement = AdapterRegistry(
        profile=profile,
        measurement_harness=_adapter("measurement_harness", profile=profile),
        resource_cleaner=_adapter("resource_cleaner", profile=profile),
    )
    adjudication = AdapterRegistry(
        profile=profile,
        candidate_adjudicator=_adapter("candidate_adjudicator", profile=profile),
    )
    return source, correctness, measurement, adjudication


def test_complete_m1_profile_composes_but_default_catalog_stays_closed() -> None:
    with pytest.raises(AdapterUnavailable, match="not registered"):
        AdapterProfileCatalog().require(REAL_MANUAL_CANDIDATE_PROFILE)

    registry = compose_nmz36_m1_registry(*_registries())
    assert registry.profile == REAL_MANUAL_CANDIDATE_PROFILE
    assert MANUAL_CANDIDATE_CAPABILITIES.issubset(registry.available())


def test_m1_profile_composition_rejects_cross_profile_adapter() -> None:
    source, correctness, measurement, adjudication = _registries()
    wrong = AdapterRegistry(
        profile=REAL_MANUAL_CANDIDATE_PROFILE,
        candidate_adjudicator=_adapter(
            "candidate_adjudicator",
            profile="another-profile",
        ),
    )
    with pytest.raises(AdapterUnavailable, match="does not match registry profile"):
        compose_nmz36_m1_registry(source, correctness, measurement, wrong)


def test_bw20_m1_profile_is_opt_in_and_requires_one_complete_real_registry() -> None:
    with pytest.raises(AdapterUnavailable, match="not registered"):
        AdapterProfileCatalog().require(BW20_MANUAL_CANDIDATE_PROFILE)

    registry = compose_m1_registry(
        *_registries(BW20_MANUAL_CANDIDATE_PROFILE),
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
    )
    assert registry.profile == BW20_MANUAL_CANDIDATE_PROFILE
    assert MANUAL_CANDIDATE_CAPABILITIES.issubset(registry.available())


def test_generic_m1_composition_rejects_an_undeclared_profile() -> None:
    with pytest.raises(ValueError, match="unsupported real M1"):
        compose_m1_registry(*_registries(), profile="unreviewed-m1-profile")

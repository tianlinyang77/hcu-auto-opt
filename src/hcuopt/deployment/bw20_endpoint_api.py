# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in API application for the BW20 provisional endpoint validation slice."""

from __future__ import annotations

from hcuopt.adapters.profiles import (
    AdapterProfileCatalog,
    bw20_endpoint_validation_profile,
)
from hcuopt.api.app import create_app
from hcuopt.storage.repository import PostgresRepository


def build_application(repository: PostgresRepository | None = None):
    return create_app(
        repository=repository,
        adapter_profiles=AdapterProfileCatalog((bw20_endpoint_validation_profile(),)),
        auto_migrate=False,
    )


app = build_application()


__all__ = ["app", "build_application"]

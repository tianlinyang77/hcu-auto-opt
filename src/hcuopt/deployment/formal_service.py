# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Configuration-owned console startup, without migrations or worker execution."""

import argparse
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
from hcuopt.deployment.formal_schema_check import inspect_formal_schema
from hcuopt.measurement.m2_formal_receipt import _is_link, _read_regular
from hcuopt.operator.formal_start_store import (
    DeploymentFormalStartAuthorityStore,
    FileFormalStartPreviewStore,
)
from hcuopt.storage.repository import PostgresRepository


class FormalServiceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["formal-service-configuration-v1"]
    compiler_path: str
    trust_path: str
    capabilities_path: str
    package_root: str
    preview_root: str
    authority_root: str
    static_root: str
    package_profile: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    store_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    allowed_overlay_roots: tuple[str, ...] = Field(min_length=1)
    approved_mount_targets: dict[str, str] = Field(min_length=1)
    port: int = Field(ge=1024, le=65535, strict=True)


def _directory(root: Path, value: str) -> Path:
    """Require existing admin-owned directories, never create or follow links."""
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Formal service directory must be beneath deployment root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if _is_link(cursor) or not cursor.is_dir():
            raise ValueError("Formal service directory is missing or redirected")
    cursor.resolve().relative_to(root.resolve())
    return cursor


def load_formal_service_runtime(
    *, deployment_root: Path, configuration_path: Path, database_url: str,
    expected_source_commit: str, clock=None, enabled: bool = False,
):
    """Read local packages and schema before exposing the narrow HTTP surface.

    Root and all parents must be administrator-controlled. DSN is never loaded
    from public configuration. No dispatch loop, migration or HCU worker runs.
    """
    config = FormalServiceConfiguration.model_validate_json(
        _read_regular(deployment_root, configuration_path, 64 * 1024),
    )
    directories = {
        name: _directory(deployment_root, getattr(config, name))
        for name in ("package_root", "preview_root", "authority_root", "static_root")
    }
    verifier = BusinessCandidateFamilyVerifier(
        CandidateSourcePackageStore(
            directories["package_root"], profile=config.package_profile,
            allowed_overlay_roots=config.allowed_overlay_roots,
            approved_mount_targets=config.approved_mount_targets,
        ), store_id=config.store_id, store_hash=config.store_hash,
    )
    objects = DeploymentFormalStartAuthorityStore(
        directories["authority_root"],
        preview_store=FileFormalStartPreviewStore(directories["preview_root"]),
    )
    runtime = FormalDeploymentRuntime.from_configuration(
        PostgresRepository(database_url), deployment_root=deployment_root,
        compiler_path=deployment_root / config.compiler_path,
        trust_path=deployment_root / config.trust_path,
        capabilities_path=deployment_root / config.capabilities_path,
        candidate_family_verifier=verifier, expected_source_commit=expected_source_commit,
        object_store=objects, clock=clock, enabled=enabled,
    )
    compiler = runtime.management.coordinator.compiler
    verifier.verify(compiler.candidate_family_manifest_store.read_manifest(
        source_family_hash=compiler.authorization.source_family_hash,
    ))
    if not inspect_formal_schema(database_url)["schema_checks_passed"]:
        raise ValueError("Formal service database requires a separate reviewed migration")
    return runtime, config, directories["static_root"]


def build_formal_service(
    *, deployment_root: Path, configuration_path: Path, database_url: str,
    expected_source_commit: str, clock=None,
):
    runtime, config, static_root = load_formal_service_runtime(
        deployment_root=deployment_root, configuration_path=configuration_path,
        database_url=database_url, expected_source_commit=expected_source_commit, clock=clock,
    )
    app = runtime.console(
        static_root=static_root, browser_origin=f"http://127.0.0.1:{config.port}",
    )
    app.state.formal_runtime = runtime
    return app, config.port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        dsn = os.environ.get("HCUOPT_DATABASE_URL")
        if not dsn:
            raise ValueError("database configuration missing")
        app, port = build_formal_service(
            deployment_root=args.deployment_root,
            configuration_path=args.deployment_root / args.configuration,
            database_url=dsn, expected_source_commit=args.source_commit,
        )
    except Exception:
        # Configuration/driver errors may carry credentials or private paths.
        print(json.dumps({"error": "formal_service_preflight_failed", "started": False}))
        return 2
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

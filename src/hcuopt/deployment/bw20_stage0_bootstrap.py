# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit BW20 composition for the existing API and Worker, never a fake fallback.

The check CLI reads inputs only. Service creation requires original Target admission;
no database defaults, migrations, clock writes, signing or network listeners here.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.deployment.bw20_auto_clock_session import BW20AutoClockSessionFactory
from hcuopt.deployment.bw20_auto_observation_session import (
    BW20AutoObservationSessionFactory,
)
from hcuopt.deployment.bw20_clock_backend_review import (
    BW20ClockBackendReviewManifest,
    inspect_clock_backend_review,
)
from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_runtime_binding import validate_binding
from hcuopt.deployment.bw20_runtime_verify import BW20PreparedInputGuard, _pinned, checked_digest
from hcuopt.deployment.bw20_stage0_capabilities import BW20RuntimeProbeAdapter
from hcuopt.deployment.bw20_stage0_deployment import (
    BW20MeasurementAdmission,
    compose_measurement_adapter,
)
from hcuopt.deployment.bw20_stage0_profile import compose_stage0_profile
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import TargetConfigError, TargetNotReady
from hcuopt.evaluation.sglang_smoke import load_workload_spec
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.targets import target_fingerprint


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    prepared_root: Path
    controller_root: Path
    controller_archive: Path
    preparation_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    archive_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_binding: Path
    runtime_binding_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Default preserves replay of already frozen BW20 v2 deployment files.
    protocol_version: Literal[
        "s0-g0-bw20-v2", "s0-g0-bw20-v3", "s0-g0-bw20-v4"
    ] = "s0-g0-bw20-v2"
    protocol_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_root: Path
    clock_backend_review: Path | None = None
    clock_backend_review_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @field_validator("prepared_root", "controller_root", "controller_archive",
                     "runtime_binding", "evidence_root")
    @classmethod
    def absolute_path(cls, value):
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("deployment paths must be absolute and canonical")
        return value

    @field_validator("clock_backend_review")
    @classmethod
    def optional_absolute_path(cls, value):
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("deployment paths must be absolute and canonical")
        return value

    @model_validator(mode="after")
    def complete_clock_backend_review_pin(self):
        if (self.clock_backend_review is None) != (
            self.clock_backend_review_sha256 is None
        ):
            raise ValueError("clock backend review path and pin must be supplied together")
        return self


class PreparedTargetCatalog:
    """Expose exactly one pinned Target via the original catalog load/list interface."""

    def __init__(self, path, pin, target_id):
        self.path, self.pin, self.target_id = path, pin, target_id

    def source_path(self, target_id):
        self.load(target_id)
        return self.path

    def load(self, target_id):
        if target_id != self.target_id:
            raise TargetConfigError("target outside BW20 deployment")
        target = TargetSpec.model_validate_json(_pinned(self.path, self.pin))
        if target.target_id != target_id:
            raise TargetConfigError("prepared target identity changed")
        return target

    def list(self):
        return [self.load(self.target_id)]


@dataclass
class BW20Stage0Deployment:
    config: DeploymentConfig
    config_path: Path
    config_sha256: str
    profile: object
    registry: object
    targets: PreparedTargetCatalog
    input_guard: BW20PreparedInputGuard
    measurement_clock_policy_bound: bool
    measurement_clock_policy: str | None
    clock_control_bound: bool
    clock_journal: ClockJournal | None
    clock_backend_static_review: dict | None
    clock_backend_review_journal: ClockJournal | None

    def check(self):
        _pinned(self.config_path, self.config_sha256)
        target = self.targets.load(self.targets.target_id)
        inputs = self.input_guard()
        raw = json.loads(_pinned(self.config.runtime_binding, self.config.runtime_binding_sha256))
        binding = validate_binding(raw)
        if (raw["source_commit"] != target.source_baseline.commit
                or raw["version"] != target.inference_image.sglang_package_version
                or inputs["target_fingerprint"] != target_fingerprint(target)):
            raise ValueError("runtime or preparation differs from deployment target")
        if (checked_digest(self.config.controller_archive, limit=80 * 1024**2)
                != self.config.archive_sha256):
            raise ValueError("controller archive changed")
        try:
            self.profile.validate_target(target)
        except TargetNotReady as exc:
            admitted, rejection = False, str(exc)
        else:
            admitted, rejection = True, None
        static_review = self.clock_backend_static_review
        if self.config.clock_backend_review is not None:
            if self.clock_backend_review_journal is None:
                raise ValueError("clock backend review journal is unavailable")
            static_review = inspect_clock_backend_review(
                self.config.clock_backend_review,
                manifest_sha256=self.config.clock_backend_review_sha256,
                expected_target_id=target.target_id,
                expected_target_fingerprint=target_fingerprint(target),
                journal=self.clock_backend_review_journal,
            )
        return dict(
            schema_version="bw20-stage0-deployment-check-v1", inputs_verified=True,
            profile=self.profile.name, target_fingerprint=target_fingerprint(target),
            routes=sorted(p.value for p in self.registry.stage0_probe._routes),
            runtime_binding=binding, target_admitted=admitted, rejection=rejection,
            measurement_clock_policy_bound=self.measurement_clock_policy_bound,
            measurement_clock_policy=self.measurement_clock_policy,
            clock_control_bound=self.clock_control_bound,
            clock_backend_static_review=static_review,
            services_started=False, hcu_used=False, stage0_accepted=False,
            automatic_release_allowed=False,
        )

    def require_ready(self):
        report = self.check()
        if not report["target_admitted"]:
            raise TargetNotReady(report["rejection"])

    def make_application(self, *, repository):
        """Inject an explicitly configured repository; never migrate it implicitly."""
        self.require_ready()
        if not self.measurement_clock_policy_bound:
            raise TargetNotReady("BW20 deployment has no bound measurement clock policy")
        if repository is None:
            raise ValueError("explicit database repository required")
        from hcuopt.api.app import create_app

        return create_app(repository=repository, target_catalog=self.targets,
                          adapter_profiles=AdapterProfileCatalog((self.profile,)),
                          auto_migrate=False)

    def make_worker(self, *, api_url: str, worker_id: str,
                    clock_journal: ClockJournal | None = None):
        parsed = urlsplit(api_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port is None or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("BW20 worker requires an explicit loopback API port")
        self.require_ready()
        if not self.measurement_clock_policy_bound:
            raise TargetNotReady("BW20 deployment has no bound measurement clock policy")
        if self.clock_control_bound:
            if (
                not isinstance(clock_journal, ClockJournal)
                or clock_journal is not self.clock_journal
            ):
                raise ValueError("BW20 worker must reuse its clock session factory journal")
        elif clock_journal is not None:
            raise ValueError("auto-observation policy does not use a clock journal")

        def resource_guard(resource_id):
            if resource_id != RESOURCE:
                raise ValueError("resource outside BW20 clock guard scope")
            if self.clock_journal is not None:
                self.clock_journal.require_clear(resource_id)

        from hcuopt.workers.sdk import Worker

        return Worker(worker_id, WorkerType.GPU, api_url,
                      capabilities={"resource_id": RESOURCE}, adapters=self.registry,
                      output_dir=self.config.evidence_root, resource_guard=resource_guard)


def load_deployment(
    path: Path, *, config_sha256: str, runner, clock_session_factory=None
) -> BW20Stage0Deployment:
    config = DeploymentConfig.model_validate_json(_pinned(path, config_sha256))
    for directory in (config.prepared_root, config.controller_root, config.evidence_root):
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise ValueError("deployment directory missing or redirected")
    guard = BW20PreparedInputGuard(config.prepared_root, config.controller_root,
                                  config.preparation_sha256, runner)
    inputs = guard()
    preparation = json.loads(_pinned(config.prepared_root / "preparation.json",
                                     config.preparation_sha256))
    target_path = config.prepared_root / "target.json"
    runtime_config = RuntimeProbeProfile.model_validate_json(_pinned(
        config.prepared_root / "runtime-profile.json", inputs["profile_sha256"]))
    targets = PreparedTargetCatalog(target_path, preparation["input_hashes"]["target.json"],
                                   runtime_config.target_id)
    target = targets.load(targets.target_id)
    workload = load_workload_spec(config.prepared_root / "workload.json")
    protocol = load_registered_stage0_protocol(config.protocol_version)
    if protocol.sha256 != config.protocol_sha256:
        raise ValueError("deployment protocol changed")
    admission = BW20MeasurementAdmission(
        target_fingerprint(target),
        protocol.sha256,
        workload.workload_id,
        protocol_version=config.protocol_version,
    )
    bundle = ControllerBundle(config.controller_archive, config.archive_sha256,
                              preparation["controller_manifest_sha256"],
                              inputs["controller_checks"][-1]["file_count"])
    clock_control_bound = isinstance(clock_session_factory, BW20AutoClockSessionFactory)
    auto_observation_bound = isinstance(
        clock_session_factory, BW20AutoObservationSessionFactory
    )
    measurement_clock_policy_bound = clock_control_bound or auto_observation_bound
    if clock_session_factory is not None and not measurement_clock_policy_bound:
        raise ValueError("reviewed BW20 measurement clock policy factory required")
    required_mode = protocol.protocol.environment_gates.required_performance_level
    if clock_control_bound and required_mode != "manual":
        raise ValueError("clock-control factory conflicts with the registered auto protocol")
    if auto_observation_bound and required_mode != "auto":
        raise ValueError("auto-observation factory conflicts with the registered protocol")
    measurement_clock_policy = (
        "manual_1500_restore_auto_v1"
        if clock_control_bound
        else "host_auto_observe_only_v1" if auto_observation_bound else None
    )
    clock_journal = clock_session_factory.journal if clock_control_bound else None
    if not measurement_clock_policy_bound:
        def clock_session_factory(*args, **kwargs):
            raise TargetNotReady("BW20 deployment has no bound measurement clock policy")

    static_review = None
    review_journal = None
    if config.clock_backend_review is not None:
        review_manifest = BW20ClockBackendReviewManifest.model_validate_json(
            _pinned(config.clock_backend_review, config.clock_backend_review_sha256)
        )
        review_journal = ClockJournal.open_existing(review_manifest.journal_path)
        if clock_journal is not None and review_journal.path != clock_journal.path:
            raise ValueError("clock backend review and runtime factory journals differ")
        static_review = inspect_clock_backend_review(
            config.clock_backend_review,
            manifest_sha256=config.clock_backend_review_sha256,
            expected_target_id=target.target_id,
            expected_target_fingerprint=target_fingerprint(target),
            journal=review_journal,
        )

    measurement = compose_measurement_adapter(target=target, runner=runner, bundle=bundle,
                                              manifest_sha256=bundle.manifest_sha256,
                                              admission=admission,
                                              clock_session_factory=clock_session_factory)
    runtime = BW20RuntimeProbeAdapter(target=target, runner=runner, configuration=runtime_config,
                                     configuration_sha256=inputs["profile_sha256"],
                                     admission=admission, evidence_root=config.evidence_root,
                                     input_guard=guard)
    profile, registry = compose_stage0_profile(measurement=measurement, runtime=runtime,
                                              admission=admission)
    return BW20Stage0Deployment(
        config, path, config_sha256, profile, registry, targets, guard,
        measurement_clock_policy_bound, measurement_clock_policy,
        clock_control_bound, clock_journal, static_review, review_journal,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner

    deployment = load_deployment(args.config, config_sha256=args.sha256,
                                 runner=BW20LocalCommandRunner())
    result = deployment.check()
    print(json.dumps(result))
    return 0 if result["target_admitted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Single composition root for the console and its trusted deployment services.

Construction does not migrate, dispatch, claim, acquire resources or run work.
Authority issuers and physical adapters remain deployment-owned.
"""

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI

from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.deployment.formal_intent_console import create_formal_intent_console
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_correctness_journal import PostgresFormalCorrectnessJournal
from hcuopt.storage.formal_correctness_recovery import PostgresFormalCorrectnessRecovery
from hcuopt.storage.formal_dispatch import PostgresFormalDispatcher
from hcuopt.storage.repository import PostgresRepository


class FormalDeploymentRuntime:
    """Share one repository/coordinator/claim chain instead of manual reader wiring.

    enabled is a deployment switch, not an authorization grant. Existing signed
    authority, window, budget and resource checks still run on every operation.
    No background thread or generic control-plane API is started here.
    """

    def __init__(
        self,
        repository: PostgresRepository,
        management: FormalStartManagement,
        *,
        enabled: bool = False,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("Formal runtime enabled must be a boolean")
        if management.dispatch_reader is not None or management.recovery_reader is not None:
            raise Conflict("Formal runtime requires unbound management readers")
        self.repository = repository
        self.dispatcher = PostgresFormalDispatcher(
            repository, management.coordinator, enabled=enabled,
        )
        self.claims = PostgresFormalClaimStore(self.dispatcher, enabled=enabled)
        self.management = replace(management, dispatch_reader=self.dispatcher)

    def build_consumer(self, *, intent_id, worker_id, claim_token, builder):
        """Bind a reviewed builder to this runtime; never grant or acquire a claim."""
        from hcuopt.storage.formal_build import PostgresFormalBuildStore
        from hcuopt.storage.formal_build_journal import PostgresFormalBuildJournal
        from hcuopt.workers.formal_build_consumer import FormalBuildConsumer

        return FormalBuildConsumer(
            PostgresFormalBuildJournal(self.claims, intent_id, worker_id, claim_token),
            builder, PostgresFormalBuildStore(self.claims, enabled=self.dispatcher.enabled),
            enabled=self.dispatcher.enabled,
        )

    def correctness_consumer(self, *, journal, adapter):
        """Bind D's adapter only to this runtime's exact lease/claim chain."""
        from hcuopt.workers.formal_correctness_consumer import FormalCorrectnessConsumer

        if journal.lease.jobs.claims is not self.claims:
            raise Conflict("Formal correctness journal belongs to another runtime")
        return FormalCorrectnessConsumer(journal, adapter, enabled=self.dispatcher.enabled)

    def local_build_consumer(
        self, *, intent_id, worker_id, claim_token, artifact_root: Path, cache_root: Path,
    ):
        """Explicit CPU builder factory using the admitted compiler's package policy.

        May initialize deployment-owned artifact/cache directories. Does not
        register a Worker, acquire a claim, reserve budget or perform a build.
        """
        if not self.dispatcher.enabled or not self.claims.enabled:
            raise Conflict("Formal local build runtime is disabled")
        from hcuopt.adapters.build_cache import LocalBuildCache
        from hcuopt.adapters.formal_candidate_builder import FormalRoundCandidateBuilder
        from hcuopt.adapters.git_source import GitSourceManager
        from hcuopt.adapters.local_artifact_store import LocalArtifactStore
        from hcuopt.adapters.manual_candidate import (
            CandidateSourcePackageStore,
            ManualOverlayCandidateBuilder,
        )
        from hcuopt.domain.enums import SearchRoundRunMode

        compiler = self.management.coordinator.compiler
        target = compiler.profiles.require(
            compiler.authorization.profiles.target_profile, SearchRoundRunMode.FORMAL,
        ).authority_refs
        verifier = compiler.candidate_family_verifier
        if (verifier.store_id != target.candidate_package_store_id
                or verifier.store_hash != target.candidate_package_store_hash):
            raise Conflict("Formal build package Store differs from admitted target")
        packages = verifier.source_packages
        profile = target.adapter_profile
        builder = FormalRoundCandidateBuilder(ManualOverlayCandidateBuilder(
            GitSourceManager(profile),
            CandidateSourcePackageStore(
                packages.root, profile=profile,
                allowed_overlay_roots=packages.allowed_overlay_roots,
                approved_mount_targets=packages.approved_mount_targets,
            ),
            LocalArtifactStore(artifact_root, profile), LocalBuildCache(cache_root),
            profile=profile,
        ), store_id=verifier.store_id, store_hash=verifier.store_hash, enabled=True)
        return self.build_consumer(
            intent_id=intent_id, worker_id=worker_id, claim_token=claim_token, builder=builder,
        )

    def phase_consumer(self, *, intent_id, worker_id, claim_token, adapter):
        """Bind B's registered phase runner to durable reads and receipt writeback.

        Does not reserve a phase, acquire a device, or turn a receipt into D's
        acceptance verdict. The adapter keeps its original authority checks.
        """
        from hcuopt.measurement.m2_formal_runner import M2FormalPhaseExecutionAdapter
        from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
        from hcuopt.storage.formal_phase_materials import PostgresFormalPhaseMaterialReader
        from hcuopt.workers.formal_phase_consumer import FormalPhaseConsumer

        if not isinstance(adapter, M2FormalPhaseExecutionAdapter):
            raise TypeError("Formal phase requires B's registered execution adapter")
        journal = PostgresFormalPhaseJournal(
            self.claims, intent_id, worker_id, claim_token, adapter.receipt_store,
        )
        return FormalPhaseConsumer(
            journal, adapter, enabled=self.dispatcher.enabled,
            material_reader=PostgresFormalPhaseMaterialReader(journal),
        )

    @classmethod
    def from_configuration(
        cls, repository: PostgresRepository, *, deployment_root: Path,
        compiler_path: Path, trust_path: Path, capabilities_path: Path,
        candidate_family_verifier, expected_source_commit: str, object_store,
        enabled: bool = False, clock=None,
    ) -> "FormalDeploymentRuntime":
        """Rebuild compiler and runtime through original signed Profile admission."""
        from hcuopt.deployment.formal_compiler import load_formal_compiler
        from hcuopt.deployment.formal_trust import FormalPublicTrust

        trust = FormalPublicTrust.from_file(deployment_root=deployment_root, path=trust_path)
        compiler = load_formal_compiler(
            deployment_root=deployment_root, path=compiler_path, trust=trust,
            candidate_family_verifier=candidate_family_verifier,
            expected_source_commit=expected_source_commit, clock=clock,
        )
        coordinator = trust.coordinator(compiler, object_store=object_store, clock=clock)
        management = FormalStartManagement.from_file(
            coordinator, deployment_root=deployment_root, path=capabilities_path,
        )
        return cls(repository, management, enabled=enabled)

    @classmethod
    def from_deployment_files(
        cls, repository: PostgresRepository, *, compiler, object_store,
        deployment_root: Path, trust_path: Path, capabilities_path: Path,
        enabled: bool = False, clock=None,
    ) -> "FormalDeploymentRuntime":
        """Load public trust and pre-signed access together, with no private keys.

        compiler and object_store are trusted, already admitted deployment inputs.
        This factory never creates authorization, migrates DB or starts workers.
        """
        from hcuopt.deployment.formal_trust import FormalPublicTrust

        trust = FormalPublicTrust.from_file(deployment_root=deployment_root, path=trust_path)
        coordinator = trust.coordinator(compiler, object_store=object_store, clock=clock)
        management = FormalStartManagement.from_file(
            coordinator, deployment_root=deployment_root, path=capabilities_path,
        )
        return cls(repository, management, enabled=enabled)

    def console(
        self,
        *,
        static_root: Path,
        browser_origin: str,
        correctness_journal: PostgresFormalCorrectnessJournal | None = None,
    ) -> FastAPI:
        """Bind optional recovery to this runtime's exact claim chain.

        A different repository object, even with the same DSN, must not be
        silently substituted. Build a new explicit runtime after a restart.
        This route reads one selected invocation; it does not release or retry it.
        """
        management = self.management
        if correctness_journal is not None:
            if correctness_journal.lease.jobs.claims is not self.claims:
                raise Conflict("Formal recovery journal belongs to another runtime")
            management = replace(
                management,
                recovery_reader=PostgresFormalCorrectnessRecovery(correctness_journal),
            )
        return create_formal_intent_console(
            management=management,
            repository=self.repository,
            static_root=static_root,
            browser_origin=browser_origin,
        )

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

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID


class WorkflowCoordinator(Protocol):
    name: str

    def start_after_baseline(self, task_id: UUID, baseline: Mapping[str, Any]) -> Any: ...

    def advance(self, job: Mapping[str, Any]) -> None: ...

    def reconcile(self) -> list[UUID]: ...


class WorkflowFactory(Protocol):
    def __call__(self, repository: Any) -> WorkflowCoordinator: ...

from pathlib import Path

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]


def _records(resource_id: str | None) -> list[dict[str, str | None]]:
    return [{"resource_id": resource_id} for _ in range(7)]


def test_bw20_stage0_preserves_target_scoped_resource_identity() -> None:
    target = load_target(ROOT / "config" / "targets" / "bw20-sglang-0.5.12.yaml")

    assert PostgresRepository._stage0_expected_resource_id(
        target,
        _records("bw20-sglang-0.5.12:hcu:7"),
    ) == "bw20-sglang-0.5.12:hcu:7"


def test_existing_stage0_resource_identity_remains_compatible() -> None:
    target = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")

    assert PostgresRepository._stage0_expected_resource_id(
        target,
        _records("hcu-7"),
    ) == "hcu-7"


@pytest.mark.parametrize(
    "records",
    [
        _records("hcu-6"),
        _records("another-target:hcu:7"),
        _records(None),
        _records("hcu-7")[:-1] + [{"resource_id": "hcu-6"}],
    ],
)
def test_stage0_resource_identity_fails_closed(
    records: list[dict[str, str | None]],
) -> None:
    target = load_target(ROOT / "config" / "targets" / "bw20-sglang-0.5.12.yaml")

    with pytest.raises(Conflict):
        PostgresRepository._stage0_expected_resource_id(target, records)

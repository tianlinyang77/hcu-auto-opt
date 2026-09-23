# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_phase_materials import (
    FormalPhaseMaterials,
    PostgresFormalPhaseMaterialReader,
)
from hcuopt.workers.formal_phase_consumer import FormalPhaseConsumer
from tests.unit.test_formal_execution_checkpoint import setup
from tests.unit.test_formal_phase_consumer import MemoryJournal
from tests.unit.test_formal_phase_prepare import materials


def reader_case(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    journal = MemoryJournal(adapter, kwargs["request"])
    expected = FormalPhaseMaterials(
        kwargs["round_authority"], kwargs["formal_authority"], kwargs["member"]
    )
    rows = {
        "search_rounds": expected.round_authority.model_dump(mode="python"),
        "round_candidates": expected.member.model_dump(mode="python"),
        "artifacts": {"synthetic": False},
        "formal_round_authority_contexts": expected.formal_authority,
    }
    queries = []

    class Connection:
        def execute(self, sql, params):  # type: ignore[no-untyped-def]
            queries.append((sql, params))
            table = sql.split("FROM ")[1].split()[0]
            return SimpleNamespace(fetchone=lambda: rows[table])

    @contextmanager
    def connection():  # type: ignore[no-untyped-def]
        yield Connection()

    repository = journal.claims.dispatcher.repository
    repository.connection = connection
    repository._search_round_authority = lambda row: type(expected.round_authority)(**row)
    repository._formal_authority_context = lambda row: row
    journal.claims._lock_deployment_intent = lambda conn, ident: (
        repository.get_formal_start_intent(ident)
    )
    reader = PostgresFormalPhaseMaterialReader(journal)
    return reader, expected, rows, queries, adapter, harness, budget, kwargs


def test_reads_scoped_locked_materials(tmp_path):  # type: ignore[no-untyped-def]
    reader, expected, _, queries, *_ = reader_case(tmp_path)
    assert reader.load(expected.member.candidate_id) == expected
    assert len(queries) == 4
    assert all(sql.endswith("FOR SHARE") for sql, _ in queries)
    assert expected.round_authority.task_id in queries[2][1]


@pytest.mark.parametrize(
    "fault", ["outside", "round", "member", "family", "artifact", "synthetic", "context"]
)
def test_incomplete_or_synthetic_materials_rejected(tmp_path, fault):  # type: ignore[no-untyped-def]
    reader, expected, rows, _, *_ = reader_case(tmp_path)
    candidate_id = expected.member.candidate_id
    if fault == "outside":
        candidate_id = uuid4()
    elif fault == "family":
        rows["search_rounds"]["artifact_family_hash"] = None
    elif fault == "synthetic":
        rows["artifacts"]["synthetic"] = True
    else:
        table = {"round": "search_rounds", "member": "round_candidates",
                 "artifact": "artifacts", "context": "formal_round_authority_contexts"}[fault]
        rows[table] = None
    with pytest.raises(Conflict):
        reader.load(candidate_id)


def test_current_execution_rereads_at_all_checkpoints(tmp_path):  # type: ignore[no-untyped-def]
    reader, _, _, queries, adapter, harness, budget, kwargs = reader_case(tmp_path)
    consumer = FormalPhaseConsumer(reader.journal, adapter, enabled=True, material_reader=reader)
    inputs = materials(kwargs)
    for key in ("round_authority", "formal_authority", "member"):
        del inputs[key]
    receipt = consumer.execute_current_once(
        candidate_id=kwargs["member"].candidate_id, output_dir=kwargs["output_dir"], **inputs
    )
    assert receipt.execution.status == "succeeded"
    assert len(queries) == 16  # initial snapshot + three B checkpoints
    assert len(harness.payloads) == len(budget.reserves) == 1


def test_post_sampling_drift_keeps_failure_receipt(tmp_path):  # type: ignore[no-untyped-def]
    reader, _, rows, _, adapter, harness, _, kwargs = reader_case(tmp_path)
    original = harness.run_manual_performance

    def changed(*args):  # type: ignore[no-untyped-def]
        result = original(*args)
        rows["round_candidates"]["optimization_intent"] = "changed after sampling"
        return result

    harness.run_manual_performance = changed
    consumer = FormalPhaseConsumer(reader.journal, adapter, enabled=True, material_reader=reader)
    receipt = consumer.execute_once(**kwargs)
    assert receipt.execution.status != "succeeded"
    assert receipt.execution.measurement_ref is None
    assert reader.journal.row["state"] == "receipt_recorded"


def test_current_execution_requires_reader(tmp_path):  # type: ignore[no-untyped-def]
    adapter, _, _, kwargs = setup(tmp_path)
    inputs = materials(kwargs)
    for key in ("round_authority", "formal_authority", "member"):
        del inputs[key]
    consumer = FormalPhaseConsumer(MemoryJournal(adapter, kwargs["request"]), adapter, enabled=True)
    with pytest.raises(Conflict, match="material reader"):
        consumer.execute_current_once(
            candidate_id=kwargs["member"].candidate_id, output_dir=kwargs["output_dir"], **inputs
        )

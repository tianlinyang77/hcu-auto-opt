# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import os
from uuid import uuid4

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_phase_materials import PostgresFormalPhaseMaterialReader
from tests.integration.test_formal_dispatch_postgres import dispatch_case  # noqa: F401
from tests.integration.test_formal_phase_journal_postgres import journal_case  # noqa: F401
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


def test_durable_intake_cannot_masquerade_as_prepared_phase(
    journal_case,  # noqa: F811
):  # type: ignore[no-untyped-def]
    journal, request = journal_case
    reader = PostgresFormalPhaseMaterialReader(journal)
    with pytest.raises(Conflict, match="outside"):
        reader.load(uuid4())
    with pytest.raises(Conflict, match="completed build"):
        reader.load(request.binding.candidate_id)
    journal.claims.request_stop(journal.intent_id, requested_by="operator")
    with pytest.raises(Conflict, match="stop requested"):
        reader.load(request.binding.candidate_id)
    with journal.claims.dispatcher.repository.connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM formal_phase_journal"
        ).fetchone()["n"] == 0
        assert connection.execute(
            "SELECT state FROM search_rounds WHERE round_id = %s", (request.binding.round_id,)
        ).fetchone()["state"] == "intake_closed"

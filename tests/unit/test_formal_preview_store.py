# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.operator.formal_start_store import FileFormalStartPreviewStore
from tests.unit.test_formal_operator_plans import _fixture


def preview(tmp_path):
    fixture = _fixture(tmp_path / "packages")
    return fixture.compiler.compile(fixture.request, fixture.repository)


def test_preview_concurrent_publish_and_restart(tmp_path):
    value = preview(tmp_path)
    store = FileFormalStartPreviewStore(tmp_path / "store")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.publish_preview, (value, value)))
    assert results == [value, value]
    assert FileFormalStartPreviewStore(store.root).load_preview(value.preview_id) == value


def test_preview_same_identity_cannot_be_replaced(tmp_path):
    value = preview(tmp_path)
    store = FileFormalStartPreviewStore(tmp_path / "store")
    store.publish_preview(value)
    changed = value.model_copy(update={"preview_request_digest": "sha256:" + "f" * 64})
    with pytest.raises(SourceArtifactError):
        store.publish_preview(changed)
    assert store.load_preview(value.preview_id) == value


def test_preview_hash_mismatch_and_missing_are_rejected(tmp_path):
    value = preview(tmp_path)
    store = FileFormalStartPreviewStore(tmp_path / "store")
    with pytest.raises(SourceArtifactError, match="Hash"):
        store.publish_preview(value.model_copy(update={"resolved_plan_hash": "sha256:" + "f" * 64}))
    with pytest.raises(NotFound):
        store.load_preview(uuid4())
    with pytest.raises(ValueError):
        store.load_preview("../../escape")


def test_preview_on_disk_identity_tamper_rejected(tmp_path):
    value = preview(tmp_path)
    store = FileFormalStartPreviewStore(tmp_path / "store")
    store.publish_preview(value)
    path = next(store.root.rglob("preview.json"))
    changed = value.model_copy(update={"preview_id": uuid4()})
    path.write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(SourceArtifactError, match="invalid"):
        store.load_preview(value.preview_id)

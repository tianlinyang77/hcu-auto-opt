# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
import os
import subprocess
from uuid import uuid4

import pytest

from hcuopt.deployment import bw20_source_build as subject
from hcuopt.targets import load_target


def test_only_locked_transport_aliases_are_accepted():
    subject.verify_origin('https://github.com/HYGON-AI/sglang-das.git',
                          'git@github.com:HYGON-AI/sglang-das.git')
    with pytest.raises(ValueError, match='origin'):
        subject.verify_origin('https://github.com/other/sglang-das.git',
                              'git@github.com:HYGON-AI/sglang-das.git')


def test_git_is_thread_bounded_and_preserves_failure(monkeypatch):
    def failed(argv, **kwargs):
        assert argv[1:5] == ['-c', 'pack.threads=1', '-c', 'index.threads=1']
        return subprocess.CompletedProcess(argv, 128, '', 'unable to create thread')
    monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(RuntimeError, match='unable to create thread'):
        subject.run_git(['git', 'clone', 'source', 'destination'])


def test_output_requires_fresh_uuid_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, 'BUILD_PARENT', tmp_path)
    root = tmp_path / str(uuid4())
    root.mkdir()
    assert subject.validate_output(root) == root
    (root / 'existing.json').write_text('{}')
    with pytest.raises(ValueError, match='fresh'):
        subject.validate_output(root)


def test_output_rejects_wrong_parent(tmp_path):
    root = tmp_path / str(uuid4())
    root.mkdir()
    with pytest.raises(ValueError, match='fresh'):
        subject.validate_output(root)


def test_output_rejects_noncanonical_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(subject, 'BUILD_PARENT', tmp_path)
    root = tmp_path / str(uuid4()).upper()
    root.mkdir()
    with pytest.raises(ValueError, match='fresh'):
        subject.validate_output(root)


@pytest.mark.skipif(os.name == 'nt', reason='existing CAS atomic publish requires Linux')
def test_real_clone_build_publish_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    origin = tmp_path / 'origin'
    subprocess.run(['git', 'init', str(origin)], check=True, capture_output=True)
    (origin / 'kernel.py').write_text('identity = True\n')
    subject.git(origin, 'add', '.')
    subject.git(origin, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-m', 'test: source fixture')
    subject.git(origin, 'remote', 'add', 'origin', 'https://github.com/HYGON-AI/sglang-das.git')
    target_path = Path(__file__).parents[2] / 'config/targets/bw20-sglang-0.5.12.yaml'
    target = load_target(target_path)
    target.source_baseline.clean_checkout = str(origin)
    target.source_baseline.commit = subject.git(origin, 'rev-parse', 'HEAD')
    monkeypatch.setattr(subject, 'load_target', lambda _: target)
    parent = tmp_path / 'builds'
    parent.mkdir()
    monkeypatch.setattr(subject, 'BUILD_PARENT', parent)
    root = parent / str(uuid4())
    root.mkdir()
    result = subject.build(target_path, root)
    assert result['status'] == 'built'
    assert result['shared_unchanged'] and result['candidate_removed']
    assert result['source_sha256'] == result['baseline_source_sha256']
    assert result['hcu_used'] is False
    assert result['framework_pair_accepted'] is False
    assert result['candidate_activation'] == 'not_installed'
    first = json.loads((root / 'build-1-manifest.json').read_text())
    second = json.loads((root / 'build-2-manifest.json').read_text())
    published = json.loads((root / 'artifact-manifest.json').read_text())
    assert first['content_hash'] == second['content_hash'] == published['content_hash']

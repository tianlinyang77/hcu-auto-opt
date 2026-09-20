# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Build real no-op source artifacts from an isolated local clone, with no HCU.

The shared source is read-only. This is source/build evidence, not installation,
candidate activation, a resource lease, or permission to clear Target blockers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.deployment.bw20_pair_prepare import regular_file
from hcuopt.deployment.kernel_binary_inventory import file_hash
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

BUILD_PARENT = Path('/home/github/hcu-auto-opt-runtime/bw20-source-builds')
SOURCE_ORIGIN_ALIASES = frozenset({
    'git@github.com:HYGON-AI/sglang-das.git',
    'https://github.com/HYGON-AI/sglang-das.git',
})


def verify_origin(actual: str, declared: str) -> None:
    if actual != declared and not {actual, declared}.issubset(SOURCE_ORIGIN_ALIASES):
        raise ValueError('shared origin is not the declared repository or its locked alias')


def run_git(argv: list[str]) -> str:
    # Git otherwise sizes pack/index thread pools from the whole host topology,
    # which can exceed a small diagnostic container's PID budget. Command-scoped
    # -c is inherited by upload-pack/pack-objects without modifying Git config.
    argv = [argv[0], '-c', 'pack.threads=1', '-c', 'index.threads=1', *argv[1:]]
    result = subprocess.run(
        argv, check=False, capture_output=True,
        text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f'git exited {result.returncode}: {result.stderr[-4000:]}')
    return result.stdout.strip()


def git(directory: Path, *args: str) -> str:
    return run_git(['git', '-C', str(directory), *args])


def write_json(path: Path, value: object) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def validate_output(root: Path) -> Path:
    root = root.absolute()
    if (root.parent != BUILD_PARENT or str(UUID(root.name)) != root.name
            or root != root.resolve(strict=True) or not root.is_dir()
            or any(root.iterdir())):
        raise ValueError('build output must be a fresh canonical UUID directory under BUILD_PARENT')
    return root


def clone_locked_source(target, isolated: Path) -> dict:
    """Clone the locked shared source without changing its worktree or config."""
    shared = Path(target.source_baseline.clean_checkout)
    if shared != shared.resolve(strict=True):
        raise ValueError('shared baseline path is redirected')
    expected = target.source_baseline.commit
    before = {'commit': git(shared, 'rev-parse', 'HEAD'),
              'tree': git(shared, 'rev-parse', 'HEAD^{tree}'),
              'status': git(shared, 'status', '--porcelain'),
              'origin': git(shared, 'config', '--get', 'remote.origin.url')}
    if before['commit'] != expected or before['status']:
        raise ValueError('shared source is not the clean locked commit')
    verify_origin(before['origin'], target.source_baseline.repository)
    # Local clone provenance is explicit. No shared .git worktree registration.
    write_json(isolated.parent / 'clone-request.json',
               {'source': str(shared), 'destination': str(isolated),
                                           'shared_before': before})
    # The shared checkout is shallow: Git 2.34 ignores --local and uses
    # upload-pack. Clone's -c settings do not constrain that server process,
    # so pass bounded pack settings explicitly at its fixed entrypoint.
    run_git(['git', 'clone', '--no-hardlinks', '--no-checkout',
             '--upload-pack=git -c pack.threads=1 -c pack.windowMemory=32m upload-pack', '--',
             str(shared), str(isolated)])
    git(isolated, 'checkout', '--detach', expected)
    git(isolated, 'remote', 'set-url', 'origin', target.source_baseline.repository)
    return before


def build(target_path: Path, output_root: Path) -> dict:
    root = validate_output(output_root)
    target = load_target(regular_file(target_path))
    shared = Path(target.source_baseline.clean_checkout)
    isolated = root / 'baseline'
    before = clone_locked_source(target, isolated)
    scoped = target.model_copy(deep=True)
    scoped.source_baseline.clean_checkout = str(isolated)
    manager = GitSourceManager(profile='bw20-noop-source-build-v1')
    builder = NoopBuilder(profile='bw20-noop-source-build-v1')
    store = LocalArtifactStore(root / 'store', profile='bw20-noop-source-build-v1')
    source_output = root / 'source-evidence'
    baseline = manager.prepare_baseline(scoped, source_output)
    candidate_id = uuid4()
    candidate = manager.create_candidate(baseline, candidate_id, source_output)
    record = {'schema': 'bw20-source-build-v1', 'status': 'failed',
              'target_fingerprint': target_fingerprint(target),
              'shared_baseline': str(shared), 'shared_before': before,
              'isolated_clone': str(isolated),
              'clone_source': str(shared), 'clone_uses_hardlinks': False,
              'isolated_origin': target.source_baseline.repository,
              'origin_transport_alias_verified': True,
              'candidate_id': str(candidate_id),
              'source_runtime_equivalence_verified': False,
              'candidate_activation': 'not_installed', 'hcu_used': False,
              'framework_pair_accepted': False, 'automatic_release_allowed': False}
    try:
        write_json(root / 'baseline-snapshot.json', baseline.model_dump(mode='json'))
        write_json(root / 'candidate-snapshot.json', candidate.model_dump(mode='json'))
        payload = {'candidate_id': str(candidate_id), 'source_snapshot': candidate,
                   'adapter_provenance': [manager.provenance.model_dump(mode='json')]}
        first = builder.build(payload, root / 'build-1')
        second = builder.build(payload, root / 'build-2')
        if first.content_hash != second.content_hash:
            raise ValueError('two no-op builds differ')
        artifact = store.publish(first, file_uri_to_path(first.uri))
        if file_hash(file_uri_to_path(artifact.uri)) != first.content_hash:
            raise ValueError('published artifact content differs')
        write_json(root / 'build-1-manifest.json', first.model_dump(mode='json'))
        write_json(root / 'build-2-manifest.json', second.model_dump(mode='json'))
        write_json(root / 'artifact-manifest.json', artifact.model_dump(mode='json'))
        record.update(status='built', source_sha256=candidate.source_hash,
                      baseline_source_sha256=baseline.source_hash,
                      artifact_sha256=artifact.content_hash,
                      artifact_uri=artifact.uri, deterministic_builds=2)
    finally:
        manager.remove_candidate(baseline, candidate, source_output)
        record['candidate_removed'] = not file_uri_to_path(candidate.worktree_uri).exists()
        record['isolated_baseline_clean'] = not git(isolated, 'status', '--porcelain')
        after = {'commit': git(shared, 'rev-parse', 'HEAD'),
                 'tree': git(shared, 'rev-parse', 'HEAD^{tree}'),
                 'status': git(shared, 'status', '--porcelain'),
                 'origin': git(shared, 'config', '--get', 'remote.origin.url')}
        record['shared_after'] = after
        record['shared_unchanged'] = before == after
        write_json(root / 'source-build-result.json', record)
    if not all(record[k] for k in ('candidate_removed', 'isolated_baseline_clean',
                                   'shared_unchanged')):
        raise ValueError('source cleanup check failed')
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.target, args.output_root), indent=2))


if __name__ == '__main__':
    main()

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance, SourceSnapshot, TargetSpec
from hcuopt.domain.errors import SourceArtifactError, SourceIntegrityError
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path


class GitSourceManager:
    """Prepare an exact baseline checkout and isolated candidate worktrees."""

    def __init__(self, profile: str = "f1-c-local-v1") -> None:
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="source_manager",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def prepare_baseline(self, target: TargetSpec, output_dir: Path) -> SourceSnapshot:
        spec = target.source_baseline
        baseline_path = Path(spec.clean_checkout)
        self._require_separate_output(baseline_path, output_dir)
        if baseline_path.is_symlink():
            raise SourceIntegrityError(f"baseline checkout cannot be a symlink: {baseline_path}")

        if not baseline_path.exists():
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                ["git", "clone", "--no-checkout", "--", spec.repository, str(baseline_path)]
            )
            self._git(baseline_path, "checkout", "--detach", spec.commit)

        self._require_repository(baseline_path)
        actual_origin = self._git(baseline_path, "config", "--get", "remote.origin.url")
        if not self._same_repository(actual_origin, spec.repository):
            raise SourceIntegrityError(
                "baseline origin does not match Target Lock: "
                f"expected {spec.repository}, got {actual_origin}"
            )

        commit = self._git(baseline_path, "rev-parse", "HEAD")
        if commit != spec.commit:
            raise SourceIntegrityError(
                f"baseline HEAD must equal locked commit {spec.commit}, got {commit}"
            )
        if self._git_status(baseline_path):
            raise SourceIntegrityError(f"baseline checkout is not clean: {baseline_path}")

        snapshot = self._snapshot(
            kind="baseline",
            repository=spec.repository,
            path=baseline_path,
        )
        self._write_source_log(snapshot, output_dir, target.target_id)
        return snapshot

    def create_candidate(
        self,
        baseline: SourceSnapshot,
        candidate_id: UUID,
        output_dir: Path,
    ) -> SourceSnapshot:
        if baseline.kind != "baseline" or not baseline.clean:
            raise SourceIntegrityError("candidate creation requires a clean baseline snapshot")

        baseline_path = file_uri_to_path(baseline.worktree_uri)
        self._require_separate_output(baseline_path, output_dir)
        self._assert_snapshot_unchanged(baseline, baseline_path)
        worktree_root = self._worktree_root(output_dir)
        worktree_root.mkdir(parents=True, exist_ok=True)
        candidate_path = worktree_root / str(candidate_id)
        if candidate_path.exists() or candidate_path.is_symlink():
            raise SourceArtifactError(f"candidate worktree already exists: {candidate_path}")

        try:
            self._git(
                baseline_path,
                "worktree",
                "add",
                "--detach",
                str(candidate_path),
                baseline.commit,
            )
            candidate = self._snapshot(
                kind="candidate",
                repository=baseline.repository,
                path=candidate_path,
                parent_snapshot_id=baseline.snapshot_id,
            )
            if candidate.source_hash != baseline.source_hash:
                raise SourceIntegrityError(
                    "No-op candidate content does not match the baseline source hash"
                )
            self._assert_snapshot_unchanged(baseline, baseline_path)
            return candidate
        except Exception:
            if candidate_path.exists():
                self._remove_worktree(baseline_path, candidate_path)
            raise

    def remove_candidate(
        self,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        output_dir: Path,
    ) -> None:
        if baseline.kind != "baseline" or candidate.kind != "candidate":
            raise SourceIntegrityError("cleanup requires baseline and candidate snapshots")
        if candidate.parent_snapshot_id != baseline.snapshot_id:
            raise SourceIntegrityError("candidate does not belong to the supplied baseline")

        baseline_path = file_uri_to_path(baseline.worktree_uri)
        candidate_path = file_uri_to_path(candidate.worktree_uri)
        self._require_managed_candidate(candidate_path, output_dir)
        self._remove_worktree(baseline_path, candidate_path)
        self._assert_snapshot_unchanged(baseline, baseline_path)

    def finalize_candidate(
        self,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        candidate_id: UUID,
        changed_paths: tuple[str, ...],
        expected_source_hash: str,
        output_dir: Path,
    ) -> SourceSnapshot:
        """Commit a reviewed replacement set and return a clean immutable snapshot."""

        if baseline.kind != "baseline" or not baseline.clean:
            raise SourceIntegrityError("candidate finalization requires a clean baseline")
        if candidate.kind != "candidate" or candidate.parent_snapshot_id != baseline.snapshot_id:
            raise SourceIntegrityError("candidate does not belong to the supplied baseline")
        if candidate.commit != baseline.commit:
            raise SourceIntegrityError("candidate must start from the exact baseline commit")
        if not changed_paths:
            raise SourceIntegrityError("M1 candidate must replace at least one source file")

        baseline_path = file_uri_to_path(baseline.worktree_uri)
        candidate_path = file_uri_to_path(candidate.worktree_uri)
        self._require_managed_candidate(candidate_path, output_dir)
        self._assert_snapshot_unchanged(baseline, baseline_path)

        expected_paths = tuple(sorted(set(changed_paths)))
        actual_paths = tuple(
            sorted(
                item
                for item in self._git(candidate_path, "diff", "--name-only", "--").splitlines()
                if item
            )
        )
        untracked_paths = tuple(
            sorted(
                item
                for item in self._git(
                    candidate_path,
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                ).splitlines()
                if item
            )
        )
        if actual_paths != expected_paths or untracked_paths:
            raise SourceIntegrityError(
                "candidate changes differ from the reviewed replacement set: "
                f"expected={expected_paths}, modified={actual_paths}, untracked={untracked_paths}"
            )

        self._git(candidate_path, "add", "--", *expected_paths)
        commit_environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "hcu-auto-opt",
            "GIT_AUTHOR_EMAIL": "hcu-auto-opt@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "hcu-auto-opt",
            "GIT_COMMITTER_EMAIL": "hcu-auto-opt@localhost",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        self._run(
            [
                "git",
                "-C",
                str(candidate_path),
                "commit",
                "--no-gpg-sign",
                "-m",
                f"hcu-auto-opt candidate {candidate_id}",
            ],
            env=commit_environment,
        )
        finalized = self._snapshot(
            kind="candidate",
            repository=baseline.repository,
            path=candidate_path,
            parent_snapshot_id=baseline.snapshot_id,
        )
        if not finalized.clean:
            raise SourceIntegrityError("finalized Candidate SourceSnapshot is not clean")
        if finalized.source_hash != expected_source_hash:
            raise SourceIntegrityError(
                "candidate source does not match the reviewed source hash: "
                f"expected {expected_source_hash}, got {finalized.source_hash}"
            )
        if finalized.source_hash == baseline.source_hash:
            raise SourceIntegrityError("M1 candidate cannot be a No-op Candidate")
        self._assert_snapshot_unchanged(baseline, baseline_path)
        return finalized

    def recover_candidates(self, baseline: SourceSnapshot, output_dir: Path) -> tuple[str, ...]:
        """Remove UUID-named worktrees left below the managed candidate root."""

        baseline_path = file_uri_to_path(baseline.worktree_uri)
        worktree_root = self._worktree_root(output_dir)
        if not worktree_root.exists():
            return ()

        recovered: list[str] = []
        for candidate_path in sorted(worktree_root.iterdir()):
            try:
                UUID(candidate_path.name)
            except ValueError:
                continue
            self._require_managed_candidate(candidate_path, output_dir)
            self._remove_worktree(baseline_path, candidate_path)
            recovered.append(candidate_path.as_uri())
        self._git(baseline_path, "worktree", "prune")
        self._assert_snapshot_unchanged(baseline, baseline_path)
        return tuple(recovered)

    def _snapshot(
        self,
        *,
        kind: str,
        repository: str,
        path: Path,
        parent_snapshot_id: UUID | None = None,
    ) -> SourceSnapshot:
        path = path.resolve(strict=True)
        commit = self._git(path, "rev-parse", "HEAD")
        tree_hash = self._git(path, "rev-parse", "HEAD^{tree}")
        clean = not bool(self._git_status(path))
        return SourceSnapshot(
            kind=kind,
            repository=repository,
            commit=commit,
            tree_hash=tree_hash,
            source_hash=canonical_source_hash(path),
            worktree_uri=path.as_uri(),
            clean=clean,
            parent_snapshot_id=parent_snapshot_id,
        )

    def _assert_snapshot_unchanged(self, snapshot: SourceSnapshot, path: Path) -> None:
        current = self._snapshot(kind="baseline", repository=snapshot.repository, path=path)
        expected = (snapshot.commit, snapshot.tree_hash, snapshot.source_hash, snapshot.clean)
        actual = (current.commit, current.tree_hash, current.source_hash, current.clean)
        if actual != expected:
            raise SourceIntegrityError(
                "baseline changed after snapshot: "
                f"expected commit/tree/source/clean={expected}, got {actual}"
            )

    def _write_source_log(
        self, snapshot: SourceSnapshot, output_dir: Path, target_id: str
    ) -> None:
        log_dir = output_dir.resolve() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "source-baseline.log"
        log_path.write_text(
            "\n".join(
                (
                    f"target_id={target_id}",
                    f"repository={snapshot.repository}",
                    f"commit={snapshot.commit}",
                    f"tree_hash={snapshot.tree_hash}",
                    f"source_hash={snapshot.source_hash}",
                    f"clean={str(snapshot.clean).lower()}",
                    f"adapter={self.provenance.adapter_name}",
                    f"adapter_version={self.provenance.adapter_version}",
                    "",
                )
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _worktree_root(output_dir: Path) -> Path:
        return output_dir.resolve() / "worktrees"

    @staticmethod
    def _require_separate_output(source_path: Path, output_dir: Path) -> None:
        source = source_path.resolve(strict=False)
        output = output_dir.resolve()
        if output == source or source in output.parents:
            raise SourceIntegrityError(
                f"F1-C output directory cannot be inside the baseline checkout: {output}"
            )

    def _require_managed_candidate(self, candidate_path: Path, output_dir: Path) -> None:
        resolved = candidate_path.resolve(strict=False)
        expected_parent = self._worktree_root(output_dir)
        if resolved.parent != expected_parent or resolved == expected_parent:
            raise SourceIntegrityError(f"refusing to remove unmanaged path: {candidate_path}")
        try:
            UUID(resolved.name)
        except ValueError as error:
            raise SourceIntegrityError(
                f"managed candidate directory must be named by UUID: {candidate_path}"
            ) from error

    def _remove_worktree(self, baseline_path: Path, candidate_path: Path) -> None:
        result = self._run(
            [
                "git",
                "-C",
                str(baseline_path),
                "worktree",
                "remove",
                "--force",
                str(candidate_path),
            ],
            check=False,
        )
        if result.returncode != 0 and candidate_path.exists():
            # Only callers that already proved the path is managed reach this fallback.
            shutil.rmtree(candidate_path)
        self._git(baseline_path, "worktree", "prune")

    @staticmethod
    def _same_repository(actual: str, expected: str) -> bool:
        actual_path = Path(actual)
        expected_path = Path(expected)
        if actual_path.exists() and expected_path.exists():
            return actual_path.resolve() == expected_path.resolve()
        return actual.rstrip("/") == expected.rstrip("/")

    def _require_repository(self, path: Path) -> None:
        if not path.is_dir():
            raise SourceIntegrityError(f"baseline checkout is not a directory: {path}")
        if self._git(path, "rev-parse", "--is-inside-work-tree") != "true":
            raise SourceIntegrityError(f"baseline checkout is not a Git worktree: {path}")

    def _git_status(self, path: Path) -> str:
        return self._git(path, "status", "--porcelain=v1", "--untracked-files=all")

    def _git(self, path: Path, *arguments: str) -> str:
        result = self._run(["git", "-C", str(path), *arguments])
        return result.stdout.strip()

    @staticmethod
    def _run(
        argv: list[str], *, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, env=env
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise SourceArtifactError(f"command failed ({' '.join(argv[:3])}): {detail}")
        return result

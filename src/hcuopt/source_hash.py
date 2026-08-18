from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from hcuopt.domain.errors import SourceIntegrityError


def _frame(hasher: object, value: bytes) -> None:
    # Length-prefix every value so different path/content boundaries cannot collide.
    hasher.update(len(value).to_bytes(8, "big"))  # type: ignore[attr-defined]
    hasher.update(value)  # type: ignore[attr-defined]


def canonical_source_hash(root: Path) -> str:
    """Hash a source tree independently of its absolute path and timestamps.

    The Git administrative entry is deliberately excluded. File names, entry types,
    executable bits, symlink targets, empty directories, and regular-file contents are
    included in a stable byte stream.
    """

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise SourceIntegrityError(f"source root is not a directory: {root}")

    hasher = hashlib.sha256()

    def visit(directory: Path, relative: Path) -> None:
        entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        for entry in entries:
            if relative == Path() and entry.name == ".git":
                continue
            entry_relative = relative / entry.name
            relative_bytes = os.fsencode(entry_relative.as_posix())

            if entry.is_symlink():
                _frame(hasher, b"symlink")
                _frame(hasher, relative_bytes)
                _frame(hasher, os.fsencode(os.readlink(entry.path)))
                continue

            entry_stat = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                _frame(hasher, b"directory")
                _frame(hasher, relative_bytes)
                visit(Path(entry.path), entry_relative)
                continue

            if entry.is_file(follow_symlinks=False):
                _frame(hasher, b"executable" if entry_stat.st_mode & stat.S_IXUSR else b"file")
                _frame(hasher, relative_bytes)
                _frame(hasher, entry_stat.st_size.to_bytes(8, "big"))
                with open(entry.path, "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        hasher.update(chunk)
                continue

            raise SourceIntegrityError(f"unsupported special file in source tree: {entry.path}")

    visit(root, Path())
    return f"sha256:{hasher.hexdigest()}"


def file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise SourceIntegrityError(f"expected a local file URI, got: {uri}")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise SourceIntegrityError(f"file URI must resolve to an absolute path: {uri}")
    return path

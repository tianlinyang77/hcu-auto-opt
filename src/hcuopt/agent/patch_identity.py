# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib


class PatchIdentityError(ValueError):
    """A raw Patch cannot be normalized or does not match its frozen identity."""


def raw_patch_hash_v1(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def normalize_patch_v1(raw: bytes) -> bytes:
    """Return the sole canonical byte representation for M2b Patch identity."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise PatchIdentityError("Patch must be strict UTF-8") from error
    if "\x00" in text:
        raise PatchIdentityError("Patch must not contain NUL")

    normalized_lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip(" \t")
        if line.startswith("index "):
            continue
        if line.startswith(("--- ", "+++ ")):
            line = line.split("\t", 1)[0]
        normalized_lines.append(line)
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()
    return ("\n".join(normalized_lines) + "\n").encode("utf-8")


def normalized_patch_hash_v1(raw: bytes) -> str:
    return raw_patch_hash_v1(normalize_patch_v1(raw))

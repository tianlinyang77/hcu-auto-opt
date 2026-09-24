"""Independent, CPU-only reference for one paged KV-cache write hypothesis.

This module does not import SGLang or Torch and cannot produce a formal M1
verdict.  It freezes valid, distinct cache locations and the exact K/V layout
so a later GPU producer can compare every touched and untouched cache cell.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from hcuopt.measurement.evidence import canonical_json_bytes

PAGE_SIZE = 64
MAX_REFERENCE_CELLS = 1_000_000


@dataclass(frozen=True, slots=True)
class KVWriteCase:
    case_id: str
    locations: tuple[int, ...]
    page_count: int
    head_count: int = 2
    head_dim: int = 64

    def __post_init__(self) -> None:
        if self.page_count < 1 or self.head_count < 1 or self.head_dim < 1:
            raise ValueError("KV-cache reference dimensions must be positive")
        if any(
            type(loc) is not int or not 0 <= loc < self.page_count * PAGE_SIZE
            for loc in self.locations
        ):
            raise ValueError("KV-cache locations must be valid nonnegative integers")
        if len(set(self.locations)) != len(self.locations):
            raise ValueError("fused KV-cache reference requires distinct locations")


def build_case(case_id: str) -> KVWriteCase:
    """Create deterministic observed/edge location families; no GPU allocation."""

    cases = {
        "representative-128": tuple(range(60, 188)),
        "empty": (),
        "one": (64,),
        "boundary-63": tuple(range(1, 64)),
        "boundary-64": tuple(range(0, 64)),
        "boundary-65": tuple(range(63, 128)),
        "permuted": (65, 1, 130, 64, 2),
    }
    try:
        locations = cases[case_id]
    except KeyError as exc:
        raise ValueError(f"unsupported KV-cache write case: {case_id}") from exc
    return KVWriteCase(case_id=case_id, locations=locations, page_count=4)


def _k_value(token: int, head: int, channel: int) -> int:
    return (31 * token + 19 * head + 7 * channel) % 97 - 48


def _v_value(token: int, head: int, channel: int) -> int:
    return (43 * token + 11 * head + 13 * channel) % 89 - 44


def expected_writes(
    case: KVWriteCase,
) -> tuple[dict[tuple[int, int, int, int], int], dict[tuple[int, int, int, int], int]]:
    """Return exact sparse K[P,H,offset,D] and V[P,H,D,offset] writes.

    A full-cache verifier must initialize untouched cells to an independent
    sentinel and compare them too; sparse writes alone are not a correctness
    verdict.
    """

    keys: dict[tuple[int, int, int, int], int] = {}
    values: dict[tuple[int, int, int, int], int] = {}
    for token, location in enumerate(case.locations):
        page, offset = divmod(location, PAGE_SIZE)
        for head in range(case.head_count):
            for channel in range(case.head_dim):
                keys[(page, head, offset, channel)] = _k_value(token, head, channel)
                values[(page, head, channel, offset)] = _v_value(token, head, channel)
    return keys, values


def expected_full_cache(case: KVWriteCase, *, untouched: int = -127) -> tuple[list[int], list[int]]:
    """Materialize a bounded, independent full-cache oracle for correctness cases.

    This is deliberately unsuitable for the production-sized cache: a GPU
    correctness producer must compare the complete real cache in its own
    bounded protocol, rather than allocating another 72k-page CPU copy here.
    """

    cells = case.page_count * case.head_count * PAGE_SIZE * case.head_dim
    if cells > MAX_REFERENCE_CELLS:
        raise ValueError("KV-cache CPU reference case exceeds its cell budget")
    keys = [untouched] * cells
    values = [untouched] * cells
    k_writes, v_writes = expected_writes(case)
    for (page, head, offset, channel), value in k_writes.items():
        index = ((page * case.head_count + head) * PAGE_SIZE + offset) * case.head_dim + channel
        keys[index] = value
    for (page, head, channel, offset), value in v_writes.items():
        index = ((page * case.head_count + head) * case.head_dim + channel) * PAGE_SIZE + offset
        values[index] = value
    return keys, values


def verify_full_cache(
    case: KVWriteCase,
    actual_k: Sequence[int],
    actual_v: Sequence[int],
    *,
    untouched: int = -127,
) -> bool:
    """Require exact K/V contents, including every untouched position."""

    expected_k, expected_v = expected_full_cache(case, untouched=untouched)
    return list(actual_k) == expected_k and list(actual_v) == expected_v


def case_hash(case: KVWriteCase) -> str:
    payload = {
        "schema_version": "m1-kv-write-case-v1",
        "case_id": case.case_id,
        "locations": case.locations,
        "page_count": case.page_count,
        "head_count": case.head_count,
        "head_dim": case.head_dim,
        "page_size": PAGE_SIZE,
        "k_value_rule": "(31*t+19*h+7*d)%97-48",
        "v_value_rule": "(43*t+11*h+13*d)%89-44",
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

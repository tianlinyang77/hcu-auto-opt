"""Independent M1 reference inputs for the paged allocator ``free`` hotspot.

This module intentionally does not import SGLang.  Formal correctness compares the
Baseline and Candidate implementations after both consume these frozen integer inputs.
The business case is limited to one request, page size 64, and page-contiguous token
indices; unsupported interleaved ownership is not silently generalized.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hcuopt.measurement.evidence import canonical_json_bytes

PAGE_SIZE = 64
INITIAL_FREE_PAGE_COUNT = 8


@dataclass(frozen=True, slots=True)
class AllocatorCase:
    values: tuple[int, ...]
    need_sort: bool
    group_splits: tuple[int, ...] = ()

    @property
    def page_ids(self) -> tuple[int, ...]:
        return tuple(value // PAGE_SIZE for value in self.values)

    @property
    def unique_pages(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.page_ids)))


def _page(page_id: int, *, count: int = PAGE_SIZE, stride: int = 1) -> tuple[int, ...]:
    if page_id < 1 or count < 1 or stride < 1:
        raise ValueError("allocator case page arguments must be positive")
    values = tuple(page_id * PAGE_SIZE + offset for offset in range(0, PAGE_SIZE, stride))
    if count > len(values):
        raise ValueError("allocator case requests more values than the page stride provides")
    return values[:count]


def build_case(case_id: str, seed: int, special_value: str) -> AllocatorCase:
    """Return one deterministic, page-contiguous allocator input.

    ``seed`` is part of the frozen input identity.  It rotates only the starting page;
    it never changes the page-contiguous invariant.  Integer cases currently accept the
    registered ``ordinary`` special-value class only.
    """

    if special_value != "ordinary":
        raise ValueError("M1 allocator cases accept only ordinary integer inputs")
    base = 1 + abs(seed) % 97
    if case_id == "target-4091-direct-nosort":
        values = tuple(
            value
            for page_ordinal in range(64)
            for value in _page(
                base + page_ordinal,
                count=PAGE_SIZE if page_ordinal < 63 else 59,
            )
        )
        return AllocatorCase(values=values, need_sort=False)
    if case_id == "target-4090-direct-nosort":
        values = tuple(
            value
            for page_ordinal in range(64)
            for value in _page(
                base + page_ordinal,
                count=PAGE_SIZE if page_ordinal < 63 else 58,
            )
        )
        return AllocatorCase(values=values, need_sort=False)
    if case_id == "nonsorted-pages-direct-nosort":
        order = (base + 17, base + 3, base + 11)
        return AllocatorCase(
            values=tuple(value for page_id in order for value in _page(page_id)),
            need_sort=False,
        )
    if case_id == "sparse-pages-direct-nosort":
        order = (base + 7, base + 2)
        return AllocatorCase(
            values=tuple(
                value for page_id in order for value in _page(page_id, count=32, stride=2)
            ),
            need_sort=False,
        )
    if case_id == "free-group-nosort":
        first = _page(base + 8)
        second = _page(base + 4)
        third = _page(base + 19, count=32)
        return AllocatorCase(
            values=first + second + third,
            need_sort=False,
            group_splits=(len(first), len(first) + len(second)),
        )
    if case_id == "nonsorted-pages-need-sort":
        order = (base + 13, base + 1, base + 9)
        return AllocatorCase(
            values=tuple(value for page_id in order for value in _page(page_id)),
            need_sort=True,
        )
    raise ValueError(f"unsupported M1 allocator case: {case_id}")


def input_tensor_record(case: AllocatorCase) -> dict[str, object]:
    return {
        "name": "free_index",
        "shape": [len(case.values)],
        "dtype": "int64",
        "values": list(case.values),
    }


def input_hash(case: AllocatorCase) -> str:
    encoded = canonical_json_bytes([input_tensor_record(case)])
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

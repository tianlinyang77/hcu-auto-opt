# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Run one HCU-free BW20 M1 ``manual_adjudicate`` Job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.adapters.real_profile import build_m1_adjudication_registry
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import load_registered_m1_protocol
from hcuopt.workers.sdk import Worker


def _trusted_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ExecutionSafetyError(f"{label} must be an existing regular directory")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise ExecutionSafetyError(f"{label} must not be redirected")
    return resolved


def build_worker(
    *,
    worker_id: str,
    api_url: str,
    trusted_evidence_root: Path,
    output_dir: Path,
) -> Worker:
    """Bind D's existing verifier to the BW20 profile without HCU authority."""

    trusted = _trusted_directory(trusted_evidence_root, "BW20 trusted evidence root")
    output = output_dir.absolute()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ExecutionSafetyError("BW20 adjudication output must be a regular directory")
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ExecutionSafetyError(
            "BW20 adjudication output parent must be an existing regular directory"
        )
    try:
        parent.resolve(strict=True).relative_to(trusted)
    except ValueError as exc:
        raise ExecutionSafetyError(
            "BW20 adjudication output must stay in the trusted evidence root"
        ) from exc
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    try:
        output.relative_to(trusted)
    except ValueError as exc:
        raise ExecutionSafetyError(
            "BW20 adjudication output must stay in the trusted evidence root"
        ) from exc

    registry = build_m1_adjudication_registry(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        protocol=load_registered_m1_protocol(),
        reader=HashedEvidenceReader(trusted),
        evidence_root=output,
    )
    return Worker(
        worker_id,
        WorkerType.EVALUATION,
        api_url,
        capabilities={
            "adapter_profile": BW20_MANUAL_CANDIDATE_PROFILE,
            "adapters": ["candidate_adjudicator"],
            "hcu_required": False,
            "automatic_release_allowed": False,
        },
        heartbeat_seconds=10,
        adapters=registry,
        output_dir=output,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--trusted-evidence-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        trusted_evidence_root=args.trusted_evidence_root,
        output_dir=args.output_dir,
    )
    worked = worker.run_once()
    print(
        json.dumps(
            {
                "worked": worked,
                "profile": BW20_MANUAL_CANDIDATE_PROFILE,
                "hcu_required": False,
                "automatic_release_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if worked else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_worker", "main"]

# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit single-file live model probe. No DB claim, Candidate, HCU or promotion.

This operator tool checks an approved model service with an exported real source
file before deploying the Messages worker. Its output is NOT Formal evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from hcuopt.adapters.agent_generator import _publish_once
from hcuopt.adapters.agent_promotion import apply_single_file_unified_patch
from hcuopt.adapters.agent_runner import (
    AgentDeploymentCredential,
    AgentInputFile,
    AgentRunLimits,
    AgentRunRequest,
    LocalCommandAgentRunner,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.adapters.messages_generator import (
    MessagesSettings,
    _ProposalsText,
    messages_input_manifest_hash,
)
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
from hcuopt.generators import anthropic_messages as program
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--source-path")
    parser.add_argument("--source-commit")
    parser.add_argument("--hotspot")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument(
        "--recover", action="store_true", help="finalize a saved Receipt, no API call"
    )
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.recover:
        return finalize_probe(args.output.resolve())
    if not all(
        (
            args.source_file,
            args.source_path,
            args.source_commit,
            args.hotspot,
            args.base_url,
            args.model,
        )
    ):
        parser.error("source, hotspot and model settings are required for a new probe")
    if args.output.exists():
        parser.error("output must be new; never rerun or overwrite a paid probe")
    if (
        not args.source_path.endswith(".py")
        or ":" in args.source_path
        or "\\" in args.source_path
        or any(p in {"", ".", ".."} for p in args.source_path.split("/"))
    ):
        parser.error("source-path must be one normalized Python path")
    if args.source_file.is_symlink() or args.source_file.stat().st_size > 256_000:
        parser.error("source must be a bounded regular file")
    source = args.source_file.read_bytes()
    settings = MessagesSettings(
        base_url=args.base_url,
        model=args.model,
        allow_http=args.allow_http,
        max_output_tokens=4096,
        timeout_seconds=120,
    )
    program.messages_url(settings.base_url, allow_http=settings.allow_http)
    credential_path = os.environ.get(program.CREDENTIAL_ENVIRONMENT_NAME, "")
    if not credential_path:
        parser.error(f"set {program.CREDENTIAL_ENVIRONMENT_NAME} to a private file")
    try:
        deployment_credential = Path(credential_path).read_bytes()
    except OSError:
        parser.error("deployment credential file is unreadable")
    context = {
        "source_path": args.source_path,
        "source": source.decode("utf-8"),
        "source_content_hash": digest(source),
        "source_origin_commit": args.source_commit,
        "hotspot_summary": args.hotspot,
        "max_proposals": 1,
        "knowledge": [],
        "scope": "single_file_development_probe_not_formal",
        "performance_conclusion": "not_measured",
    }
    request_hash = digest(canonical_json_bytes(context))
    context["request_hash"] = request_hash
    item = AgentInputFile(
        path=program.INPUT_NAME,
        content=canonical_json_bytes(
            {
                "schema_version": "m2b-messages-input-v1",
                "provider": settings.model_dump(mode="json"),
                "context": context,
            }
        ),
    )
    artifact = Path(program.__file__).resolve()
    root = args.output.resolve()
    root.mkdir(parents=True)
    _publish_once(root / "input.json", item.content)
    request = AgentRunRequest(
        attempt_id=uuid4(),
        generation_run_id=uuid4(),
        request_id=uuid4(),
        request_hash=request_hash,
        plan_id=uuid4(),
        generator_id="messages-hotspot-probe",
        executable=Path(sys.executable),
        generator_artifact=artifact,
        generator_artifact_hash=digest(artifact.read_bytes()),
        argv=(str(artifact),),
        input_files=(item,),
        limits=AgentRunLimits(
            attempt_number=1,
            timeout_seconds=150.0,
            max_stdout_bytes=1_000_000,
            max_stderr_bytes=8192,
            max_total_output_bytes=1_008_192,
            max_tokens=45_000,
        ),
    )
    runner = LocalCommandAgentRunner(
        allowed_executables=(Path(sys.executable),),
        allowed_argv_prefixes=((str(artifact),),),
        deployment_credentials=(
            AgentDeploymentCredential(
                environment_name=program.CREDENTIAL_ENVIRONMENT_NAME,
                content=deployment_credential,
            ),
        ),
    )
    result = runner.run(request, root / "work")
    receipt = RunnerExecutionReceiptStore(root / "runner").publish(result)
    _publish_once(root / "receipt-ref.json", canonical_json_bytes(receipt))
    return finalize_probe(root)


def finalize_probe(root: Path) -> int:
    """Recover a report without repeating a provider call or trusting a producer summary."""
    receipt_ref = RunnerExecutionReceiptRef.model_validate_json(
        (root / "receipt-ref.json").read_bytes()
    )
    receipt = RunnerExecutionReceiptStore(root / "runner").load(receipt_ref)
    execution = receipt.execution
    input_bytes = (root / "input.json").read_bytes()
    if (
        messages_input_manifest_hash(AgentInputFile(path=program.INPUT_NAME, content=input_bytes))
        != execution.input_manifest_hash
    ):
        raise ValueError("saved input differs from the executed Runner manifest")
    envelope = program.strict_json(input_bytes)
    context = envelope["context"]
    if context["request_hash"] != execution.request_hash:
        raise ValueError("saved context differs from Runner Request authority")
    source = context["source"].encode("utf-8")
    if digest(source) != context["source_content_hash"]:
        raise ValueError("saved source content changed")
    patches = []
    ingestion = "not_attempted"
    rejection_reason = None
    if execution.status == "succeeded":
        try:
            raw = file_uri_to_path(receipt.raw_output_uri).read_bytes()
            if digest(raw) != receipt.raw_output_hash:
                raise ValueError("saved response content changed")
            reply = program.strict_json(raw)
            parsed = _ProposalsText.model_validate(
                program.strict_json(
                    program.proposal_text(
                        reply, expected_model=envelope["provider"]["model"]
                    )
                )
            )
            if len(parsed.proposals) > 1:
                raise ValueError("too many proposals")
            for item_proposal in parsed.proposals:
                patch = item_proposal.patch.encode("utf-8")
                apply_single_file_unified_patch(source, patch, expected_path=context["source_path"])
                _publish_once(root / "suggestion.diff", patch)
                _publish_once(root / "suggestion.json", canonical_json_bytes(item_proposal))
                patches.append({"patch_hash": digest(patch), "path": "suggestion.diff"})
            ingestion = "applicable_unreviewed_patch" if patches else "no_proposals"
        except Exception as error:
            ingestion = "model_output_rejected"
            # Do not persist exception text (may contain source or model content).
            rejection_reason = type(error).__name__
    report = {
        "schema_version": "m2b-live-hotspot-probe-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scope": "standalone_source_only_development_probe",
        "runner_status": execution.status,
        "ingestion": ingestion,
        "rejection_reason": rejection_reason,
        "patches": patches,
        "source_path": context["source_path"],
        "source_origin_commit": context["source_origin_commit"],
        "source_content_hash": digest(source),
        "requested_model": envelope["provider"]["model"],
        "receipt_ref": receipt_ref.model_dump(mode="json"),
        "tokens_consumed": execution.tokens_consumed,
        "wall_seconds_consumed": execution.wall_seconds_consumed,
        "cleanup_status": execution.cleanup_status,
        "scheduler_claimed": False,
        "candidate_created": False,
        "hcu_accessed": False,
        "performance_conclusion": "not_measured",
        "formal_intake_allowed": False,
        "automatic_release_allowed": False,
    }
    if (root / "report.json").exists():
        previous = program.strict_json((root / "report.json").read_bytes())
        report["recorded_at"] = previous["recorded_at"]
    _publish_once(root / "report.json", canonical_json_bytes(report))
    # Only safe status metadata, never source, raw model text, headers or credentials.
    print(
        json.dumps(
            {
                "runner_status": execution.status,
                "ingestion": ingestion,
                "tokens_consumed": execution.tokens_consumed,
                "report": str(root / "report.json"),
            }
        )
    )
    return 0 if ingestion == "applicable_unreviewed_patch" else 2


if __name__ == "__main__":
    raise SystemExit(main())

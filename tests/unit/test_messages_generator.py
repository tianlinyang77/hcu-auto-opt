# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import UUID

import pytest

from hcuopt.adapters.agent_generator import CandidateProposalBatchStore, ProposalPatchStore
from hcuopt.adapters.agent_promotion import BaselineOverlaySource
from hcuopt.adapters.agent_runner import (
    AgentDeploymentCredential,
    AgentInputFile,
    AgentRunLimits,
    AgentRunRequest,
    DeterministicAgentRunner,
    LocalCommandAgentRunner,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.adapters.messages_generator import (
    MessagesProposalIngestor,
    MessagesSettings,
    messages_profile_for_input,
    prepare_messages_input,
)
from hcuopt.agent.authority import (
    AgentAuthorityError,
    build_generation_run_start,
    generation_plan_id_for,
    generation_run_id_for,
    proposal_refs_for_batch,
    validate_runner_receipt,
)
from hcuopt.agent.identity import apex_generation_plan_hash, candidate_generation_request_hash
from hcuopt.agent.messages_worker import MessagesGenerationWorker
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    GenerationAttemptClaim,
    GenerationRunStartRequest,
)
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.generators import anthropic_messages as program
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from tests.unit.test_agent_generator import NOW, PATCH, _knowledge, _request

SOURCE_PATH = "sglang/runtime/operator.py"
ARTIFACT = Path(program.__file__).resolve()
PROFILE = "m2b-messages-v1"


def _hash(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _response(proposals=None):
    if proposals is None:
        proposals = [
            {
                "optimization_intent": "remove redundant work",
                "rationale": "A proposal only, independently test later.",
                "risk_summary": "Correctness is not measured.",
                "patch": PATCH.decode(),
            }
        ]
    return {
        "type": "message",
        "role": "assistant",
        "model": "test-model",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps({"proposals": proposals})}],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 30,
        },
    }


@pytest.fixture
def setup(tmp_path):
    knowledge, snapshot = _knowledge(tmp_path / "knowledge")
    root = tmp_path / "baseline"
    source = root / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(b"return value\n")
    baseline = BaselineOverlaySource(
        snapshot=SourceSnapshot(
            kind="baseline",
            repository="test-only",
            commit="a" * 40,
            tree_hash="b" * 40,
            source_hash=canonical_source_hash(root),
            worktree_uri=root.as_uri(),
            clean=True,
        ),
        path=SOURCE_PATH,
    )
    key = "messages-generator-test-v1"
    run_id = generation_run_id_for(key)
    request = _request(snapshot).model_copy(
        update={
            "generation_run_id": run_id,
            "baseline_source_hash": baseline.snapshot.source_hash,
        }
    )
    artifact_hash = _hash(ARTIFACT.read_bytes())
    settings = MessagesSettings(
        base_url="https://example.invalid",
        model="test-model",
        timeout_seconds=5,
        max_output_tokens=100,
    )
    item = prepare_messages_input(
        request,
        baseline=baseline,
        knowledge_store=knowledge,
        settings=settings,
        hotspot_summary="operator supplied hotspot",
    )
    profile = messages_profile_for_input(item)
    plan = ApexGenerationPlan(
        plan_id=generation_plan_id_for(run_id),
        generation_run_id=run_id,
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generators=(
            {
                "generator_id": "messages-agent",
                "adapter_profile": profile,
                "generator_artifact_hash": artifact_hash,
                "max_attempts": 1,
                "max_proposals": 1,
                "timeout_seconds": 15,
                "max_output_bytes_per_attempt": 100_000,
                "max_tokens_per_attempt": 1000,
            },
        ),
        max_concurrency=1,
        budget={
            "max_generator_attempts": 1,
            "max_wall_seconds": 15,
            "max_total_output_bytes": 100_000,
            "max_total_tokens": 1000,
            "max_proposals": 1,
        },
        created_by="test",
        created_at=NOW,
    )
    run, attempts, _ = build_generation_run_start(
        GenerationRunStartRequest(
            request=request,
            plan=plan,
            actor="test",
            idempotency_key=key,
        ),
        created_at=NOW,
    )
    attempt = attempts[0].model_copy(
        update={
            "state": "running",
            "worker_id": "messages-test",
            "claim_token": UUID(int=900),
            "lease_expires_at": NOW + timedelta(seconds=5),
            "started_at": NOW,
        }
    )
    run = run.model_copy(update={"state": "running"})
    runner_request = AgentRunRequest(
        attempt_id=attempt.attempt_id,
        generation_run_id=run_id,
        request_id=request.request_id,
        request_hash=run.request_hash,
        plan_id=plan.plan_id,
        generator_id=attempt.generator_id,
        executable=Path(sys.executable),
        generator_artifact=ARTIFACT,
        generator_artifact_hash=artifact_hash,
        argv=(str(ARTIFACT),),
        input_files=(item,),
        limits=AgentRunLimits(
            attempt_number=1,
            timeout_seconds=15.0,
            max_stdout_bytes=80_000,
            max_stderr_bytes=20_000,
            max_total_output_bytes=100_000,
            max_tokens=1000,
        ),
    )
    receipts = RunnerExecutionReceiptStore(tmp_path / "receipts")
    patches = ProposalPatchStore(tmp_path / "patches", profile=PROFILE)
    batches = CandidateProposalBatchStore(tmp_path / "batches", profile=PROFILE)
    ingestor = MessagesProposalIngestor(
        receipt_store=receipts, patch_store=patches, batch_store=batches, profile=profile
    )
    return locals()


def _ingest(setup, reply=None, *, raw=None, reported_tokens=100):
    if raw is None:
        raw = canonical_json_bytes(_response() if reply is None else reply)
    result = DeterministicAgentRunner(proposal_bytes=raw, reported_tokens=reported_tokens).run(
        setup["runner_request"], setup["tmp_path"] / "runner"
    )
    ref = setup["receipts"].publish(result)
    stored = setup["ingestor"].ingest(
        setup["run"], setup["attempt"], ref, prepared_input=setup["item"]
    )
    return stored, ref


def test_ingest_replays_and_preserves_synthetic_identity(setup):
    stored, ref = _ingest(setup)
    assert stored.batch.status == "succeeded"
    assert stored.batch.synthetic is True
    assert stored.batch.token_count == 100
    assert stored.batch.proposals[0].formal_intake_allowed is False
    assert stored.batch.proposals[0].performance_conclusion == "not_measured"
    assert stored.batch.raw_output_uri == setup["receipts"].load(ref).raw_output_uri
    assert (
        setup["ingestor"].ingest(setup["run"], setup["attempt"], ref, prepared_input=setup["item"])
        == stored
    )
    validate_runner_receipt(
        setup["run"],
        setup["attempt"],
        setup["plan"].generators[0],
        setup["receipts"].load(ref),
        stored.batch,
    )
    assert (
        len(
            proposal_refs_for_batch(
                setup["run"], setup["attempt"], setup["plan"].generators[0], stored.batch
            )
        )
        == 1
    )


def test_ingest_accepts_unique_structured_replacement(setup):
    proposal = {
        "optimization_intent": "remove redundant work",
        "rationale": "A proposal only, independently test later.",
        "risk_summary": "Correctness is not measured.",
        "old_text": "return value\n",
        "new_text": "return value + 1\n",
    }

    stored, _ = _ingest(setup, _response([proposal]))

    assert stored.batch.status == "succeeded"
    assert len(stored.batch.proposals) == 1
    patch = file_uri_to_path(stored.batch.proposals[0].patch_uri).read_bytes()
    assert b"-return value\n+return value + 1\n" in patch


@pytest.mark.parametrize("mode", ["both", "old_only", "new_only"])
def test_invalid_structured_replacement_fails_without_publishing_patch(setup, mode):
    proposal = {
        "optimization_intent": "remove redundant work",
        "rationale": "A proposal only, independently test later.",
        "risk_summary": "Correctness is not measured.",
    }
    if mode in {"both", "old_only"}:
        proposal["old_text"] = "return value\n"
    if mode in {"both", "new_only"}:
        proposal["new_text"] = "return value + 1\n"
    if mode == "both":
        proposal["patch"] = PATCH.decode()

    stored, _ = _ingest(setup, _response([proposal]))

    assert stored.batch.status == "failed"
    assert stored.batch.proposals == ()
    assert not setup["patches"].root.exists()


@pytest.mark.parametrize(
    "mode",
    [
        "extra_authority",
        "bad_path",
        "bad_hunk",
        "two_proposals",
        "fenced",
        "tool",
        "truncated",
        "duplicate_json",
        "bad_usage",
        "model_mismatch",
    ],
)
def test_invalid_model_reply_retains_receipt_without_publishing_patches(setup, mode):
    reply = _response()
    proposals = json.loads(reply["content"][0]["text"])["proposals"]
    if mode == "extra_authority":
        proposals[0]["formal_intake_allowed"] = True
    elif mode == "bad_path":
        proposals[0]["patch"] = PATCH.decode().replace(SOURCE_PATH, "../escape.py")
    elif mode == "bad_hunk":
        proposals[0]["patch"] = PATCH.decode().replace("-return value", "-return other")
    elif mode == "two_proposals":
        proposals *= 2
    reply["content"][0]["text"] = json.dumps({"proposals": proposals})
    if mode == "fenced":
        reply["content"][0]["text"] = "```json\n" + reply["content"][0]["text"] + "\n```"
    elif mode == "tool":
        reply["content"][0]["type"] = "tool_use"
    elif mode == "truncated":
        reply["stop_reason"] = "max_tokens"
    elif mode == "duplicate_json":
        reply["content"][0]["text"] = '{"proposals": [], "proposals": []}'
    elif mode == "bad_usage":
        reply["usage"]["input_tokens"] = 21
    elif mode == "model_mismatch":
        reply["model"] = "different-model"
    stored, ref = _ingest(setup, reply)
    assert stored.batch.status == "failed"
    assert stored.batch.proposals == ()
    assert stored.batch.token_count == 100
    assert not setup["patches"].root.exists()
    assert setup["receipts"].load(ref).raw_output_bytes > 0


def test_zero_proposals_is_an_explicit_failed_batch(setup):
    stored, _ = _ingest(setup, _response([]))
    assert stored.batch.error_code == "no_model_proposals"


def test_input_binding_and_receipt_tampering_rejected(setup):
    _, ref = _ingest(setup)
    with pytest.raises(SourceArtifactError, match="Profile|manifest"):
        setup["ingestor"].ingest(
            setup["run"],
            setup["attempt"],
            ref,
            prepared_input=AgentInputFile(path=program.INPUT_NAME, content=b"{}"),
        )
    raw = file_uri_to_path(setup["receipts"].load(ref).raw_output_uri)
    raw.write_bytes(b"tampered")
    with pytest.raises(SourceArtifactError, match="changed"):
        setup["ingestor"].ingest(setup["run"], setup["attempt"], ref, prepared_input=setup["item"])


def test_cross_attempt_and_changed_plan_rejected(setup):
    _, ref = _ingest(setup)
    with pytest.raises(AgentAuthorityError):
        setup["ingestor"].ingest(
            setup["run"],
            setup["attempt"].model_copy(update={"attempt_id": UUID(int=123)}),
            ref,
            prepared_input=setup["item"],
        )
    with pytest.raises(SourceArtifactError, match="authority"):
        setup["ingestor"].ingest(
            setup["run"].model_copy(update={"plan_hash": "sha256:" + "0" * 64}),
            setup["attempt"],
            ref,
            prepared_input=setup["item"],
        )


def test_baseline_drift_refused_before_input_preparation(setup):
    setup["source"].write_bytes(b"changed")
    with pytest.raises(SourceArtifactError, match="drifted"):
        prepare_messages_input(
            setup["request"],
            baseline=setup["baseline"],
            knowledge_store=setup["knowledge"],
            settings=setup["settings"],
            hotspot_summary="hotspot",
        )


def test_worker_respects_lease_and_never_repeats_a_paid_attempt(setup, monkeypatch):
    claim = GenerationAttemptClaim(
        run=setup["run"],
        attempt=setup["attempt"],
        generator=setup["plan"].generators[0],
        request=setup["request"],
    )
    worker = MessagesGenerationWorker(setup["tmp_path"] / "worker", clock=lambda: NOW)
    calls = []

    def execute(_self, request, output):
        calls.append(request)
        return DeterministicAgentRunner(
            proposal_bytes=canonical_json_bytes(_response()),
            reported_tokens=100,
        ).run(request, output)

    monkeypatch.setattr(LocalCommandAgentRunner, "run", execute)
    with pytest.raises(SourceArtifactError, match="lease"):
        worker.execute_claim(
            claim, setup["item"], deployment_credential=b"test-only"
        )
    assert not calls
    claim = claim.model_copy(
        update={
            "attempt": claim.attempt.model_copy(
                update={
                    "lease_expires_at": NOW + timedelta(seconds=60),
                }
            )
        }
    )
    with pytest.raises(SourceArtifactError, match="authority"):
        worker.execute_claim(
            claim.model_copy(
                update={
                    "run": claim.run.model_copy(update={"state": "created"}),
                }
            ),
            setup["item"],
            deployment_credential=b"test-only",
        )
    assert not calls
    result = worker.execute_claim(
        claim, setup["item"], deployment_credential=b"test-only"
    )
    assert result.batch.batch.status == "succeeded"
    assert result.batch.batch.synthetic is True
    with pytest.raises(SourceArtifactError, match="already started"):
        worker.execute_claim(
            claim, setup["item"], deployment_credential=b"test-only"
        )
    assert len(calls) == 1
    assert "test-only" not in file_uri_to_path(result.prepared_input_uri).read_text()


def test_cache_token_usage_is_not_double_counted():
    reply = _response()
    reply["usage"]["iterations"] = [{"input_tokens": 99999}]
    reply["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 30}
    assert program.usage_tokens(reply) == 100
    reply["usage"]["input_tokens"] = True
    with pytest.raises(program.MessagesError):
        program.usage_tokens(reply)


def test_probe_report_can_be_recovered_without_calling_model(setup):
    from examples.messages_hotspot_probe import finalize_probe

    root = setup["tmp_path"] / "probe"
    root.mkdir()
    envelope = program.strict_json(setup["item"].content)
    envelope["context"]["source_origin_commit"] = "a" * 40
    item = AgentInputFile(path=program.INPUT_NAME, content=canonical_json_bytes(envelope))
    request = replace(setup["runner_request"], input_files=(item,))
    result = DeterministicAgentRunner(
        proposal_bytes=canonical_json_bytes(_response()),
        reported_tokens=100,
    ).run(request, root / "work")
    ref = RunnerExecutionReceiptStore(root / "runner").publish(result)
    (root / "receipt-ref.json").write_bytes(canonical_json_bytes(ref))
    (root / "input.json").write_bytes(item.content)
    assert finalize_probe(root) == 0
    report = (root / "report.json").read_bytes()
    assert finalize_probe(root) == 0
    assert (root / "report.json").read_bytes() == report
    assert json.loads(report)["scheduler_claimed"] is False
    assert json.loads(report)["formal_intake_allowed"] is False


def test_messages_client_does_not_forward_keys_in_response():
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, maximum):
            assert maximum == program.MAX_RESPONSE_BYTES + 1
            return b'{"oops": "test-key-should-never-be-in-evidence"}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://example.invalid/v1/messages"
            assert timeout == 5
            payload = json.loads(request.data)
            assert "tools" not in payload
            assert payload["thinking"] == {"type": "disabled"}
            return Reply()

    with pytest.raises(program.MessagesError, match="contains_credential"):
        program.request_message(
            {
                "provider": {
                    "base_url": "https://example.invalid",
                    "model": "test",
                    "allow_http": False,
                    "timeout_seconds": 5,
                    "max_output_tokens": 32,
                    "thinking_mode": "disabled",
                },
                "context": {"source": "data only"},
            },
            "test-key-should-never-be-in-evidence",
            opener=Opener(),
        )
    with pytest.raises(program.MessagesError, match="redirect_refused"):
        program.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")


def test_messages_system_requires_target_reachability_and_patch_claim_alignment():
    instructions = " ".join(program.SYSTEM.split())
    assert "condition holds for the supplied frozen target-case facts" in instructions
    assert "Match the rationale to the actual patch" in instructions
    assert "unless the edited code removes it" in instructions


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid",
        "https://key@example.invalid",
        "https://example.invalid?key=value",
        "https://example.invalid/x",
    ],
)
def test_unsafe_url_refused(url):
    with pytest.raises(program.MessagesError):
        program.messages_url(url, allow_http=False)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic/v1", "https://api.deepseek.com/anthropic/v1/messages"),
        (
            "https://api.deepseek.com/anthropic/v1/messages",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
    ],
)
def test_deepseek_anthropic_base_url_is_normalized(base_url, expected):
    assert program.messages_url(base_url, allow_http=False) == expected


def test_real_runner_to_local_http_stub_to_receipt_and_ingest(setup):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            raw = canonical_json_bytes(_response())
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        item = prepare_messages_input(
            setup["request"],
            baseline=setup["baseline"],
            knowledge_store=setup["knowledge"],
            settings=MessagesSettings(
                base_url=f"http://127.0.0.1:{server.server_port}",
                model="test-model",
                allow_http=True,
                timeout_seconds=5,
            ),
            hotspot_summary="test fixture; not a real optimization",
        )
        request = replace(setup["runner_request"], input_files=(item,))
        runner = LocalCommandAgentRunner(
            allowed_executables=(Path(sys.executable),),
            allowed_argv_prefixes=((str(ARTIFACT),),),
            deployment_credentials=(
                AgentDeploymentCredential(
                    environment_name=program.CREDENTIAL_ENVIRONMENT_NAME,
                    content=b"test-only-not-a-secret",
                ),
            ),
        )
        result = runner.run(request, setup["tmp_path"] / "http-run")
        assert result.status == "succeeded", result.evidence.stderr_summary
        assert result.evidence.tokens_consumed == 100
        assert result.evidence.cleanup_status == "verified"
        assert len(requests) == 1
        assert "tools" not in requests[0]
        assert not list((setup["tmp_path"] / "http-run").iterdir())
        ref = setup["receipts"].publish(result)
        profile = messages_profile_for_input(item)
        plan = setup["plan"].model_copy(
            update={
                "generators": (
                    setup["plan"].generators[0].model_copy(update={"adapter_profile": profile}),
                )
            }
        )
        run = setup["run"].model_copy(
            update={
                "plan": plan,
                "plan_hash": apex_generation_plan_hash(plan),
            }
        )
        attempt = setup["attempt"].model_copy(update={"adapter_profile": profile})
        ingestor = MessagesProposalIngestor(
            receipt_store=setup["receipts"],
            patch_store=setup["patches"],
            batch_store=setup["batches"],
            profile=profile,
        )
        batch = ingestor.ingest(run, attempt, ref, prepared_input=item)
        assert batch.batch.status == "succeeded"
        # Real HTTP/process implementation, but this test's provider is a local stub.
        assert batch.batch.performance_conclusion == "not_measured"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

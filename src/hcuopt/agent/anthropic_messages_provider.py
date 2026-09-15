# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded Anthropic Messages provider artifact for the no-HCU Agent Runner.

The process reads one immutable request staged by ``LocalCommandAgentRunner`` and a
deployment-owned credential file. It emits only canonical proposal JSON to stdout and
reports authoritative provider token usage through the Runner-owned usage sidecar.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

PROVIDER_REQUEST_SCHEMA = "hcuopt-anthropic-messages-request-v1"
PROPOSAL_OUTPUT_SCHEMA = "hcuopt-agent-proposal-output-v1"
USAGE_SCHEMA = "hcuopt-agent-usage-v1"
REQUEST_FILENAME = "provider-request.json"
CREDENTIAL_ENVIRONMENT_NAME = "HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProviderError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_json_constant(_value: str) -> None:
    raise ProviderError("json_constant_invalid")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ProviderError("json_duplicate_key")
        result[name] = value
    return result


def _strict_json_loads(payload: bytes | str) -> object:
    return json.loads(
        payload,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_object_from_pairs,
    )


def _read_bounded(path: Path, *, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProviderError("input_not_regular")
    if path.stat().st_size > maximum_bytes:
        raise ProviderError("input_too_large")
    return path.read_bytes()


def _require_text(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ProviderError("request_contract_invalid")
    return value


def _require_model_text(value: object, *, maximum: int) -> str:
    text = _require_text(value, maximum=maximum)
    if text.strip() != text:
        raise ProviderError("model_output_contract_invalid")
    return text


def _require_model_path(value: object) -> str:
    path = _require_model_text(value, maximum=1_000)
    parts = path.split("/")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
        or not path.endswith((".py", ".pyi"))
    ):
        raise ProviderError("model_output_contract_invalid")
    return path


def _load_request(input_root: Path) -> dict[str, Any]:
    try:
        value = _strict_json_loads(
            _read_bounded(input_root / REQUEST_FILENAME, maximum_bytes=MAX_REQUEST_BYTES)
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderError("request_json_invalid") from error
    required = {
        "schema_version",
        "endpoint",
        "model",
        "max_tokens",
        "timeout_seconds",
        "system_prompt",
        "user_prompt",
        "max_response_bytes",
        "allow_insecure_http",
        "thinking_mode",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ProviderError("request_contract_invalid")
    if value["schema_version"] != PROVIDER_REQUEST_SCHEMA:
        raise ProviderError("request_contract_invalid")
    endpoint = _require_text(value["endpoint"], maximum=2_000)
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/v1/messages")
    ):
        raise ProviderError("endpoint_invalid")
    allow_insecure = value["allow_insecure_http"]
    if type(allow_insecure) is not bool:
        raise ProviderError("request_contract_invalid")
    if value["thinking_mode"] not in {"disabled", "provider_default"}:
        raise ProviderError("request_contract_invalid")
    if parsed.scheme == "http" and (
        not allow_insecure or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ProviderError("endpoint_tls_required")
    max_tokens = value["max_tokens"]
    if type(max_tokens) is not int or not 1 <= max_tokens <= 100_000_000:
        raise ProviderError("request_contract_invalid")
    timeout_seconds = value["timeout_seconds"]
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 7_200:
        raise ProviderError("request_contract_invalid")
    max_response_bytes = value["max_response_bytes"]
    if (
        type(max_response_bytes) is not int
        or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES
    ):
        raise ProviderError("request_contract_invalid")
    _require_text(value["model"], maximum=200)
    _require_text(value["system_prompt"], maximum=1_000_000)
    _require_text(value["user_prompt"], maximum=6_000_000)
    return value


def _load_api_key() -> str:
    raw_path = os.environ.get(CREDENTIAL_ENVIRONMENT_NAME)
    if not raw_path:
        raise ProviderError("credential_unavailable")
    try:
        payload = _read_bounded(Path(raw_path), maximum_bytes=64 * 1024)
        value = payload.decode("utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise ProviderError("credential_unavailable") from error
    if not value or "\x00" in value:
        raise ProviderError("credential_unavailable")
    return value


def _request_provider(config: dict[str, Any], api_key: str) -> dict[str, Any]:
    provider_payload = {
        "model": config["model"],
        "max_tokens": config["max_tokens"],
        "system": config["system_prompt"],
        "messages": [{"role": "user", "content": config["user_prompt"]}],
    }
    if config["thinking_mode"] == "disabled":
        provider_payload["thinking"] = {"type": "disabled"}
    payload = _canonical_json_bytes(provider_payload)
    request = urllib.request.Request(
        config["endpoint"],
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=float(config["timeout_seconds"])) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > config["max_response_bytes"]:
                        raise ProviderError("provider_response_too_large")
                except ValueError as error:
                    raise ProviderError("provider_response_invalid") from error
            payload = response.read(config["max_response_bytes"] + 1)
    except ProviderError:
        raise
    except urllib.error.HTTPError as error:
        raise ProviderError(f"provider_http_{error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError("provider_transport_failed") from error
    if len(payload) > config["max_response_bytes"]:
        raise ProviderError("provider_response_too_large")
    try:
        value = _strict_json_loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProviderError("provider_response_invalid") from error
    if not isinstance(value, dict):
        raise ProviderError("provider_response_invalid")
    return value


def _extract_usage(response: dict[str, Any]) -> int:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise ProviderError("provider_usage_missing")
    values: list[int] = []
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise ProviderError("provider_usage_invalid")
        values.append(value)
    return sum(values)


def _verify_response_model(response: dict[str, Any], expected_model: str) -> None:
    if response.get("model") != expected_model:
        raise ProviderError("provider_model_mismatch")


def _extract_model_output(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("stop_reason") not in {"end_turn", "stop_sequence"}:
        raise ProviderError("provider_completion_incomplete")
    content = response.get("content")
    if not isinstance(content, list):
        raise ProviderError("provider_content_invalid")
    text_blocks = [
        item.get("text")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if not text_blocks or any(not isinstance(item, str) for item in text_blocks):
        raise ProviderError("provider_content_invalid")
    text = "".join(text_blocks)
    try:
        output = _strict_json_loads(text)
    except json.JSONDecodeError as error:
        raise ProviderError("model_output_json_invalid") from error
    if not isinstance(output, dict) or set(output) != {"schema_version", "proposals"}:
        raise ProviderError("model_output_contract_invalid")
    if output["schema_version"] != PROPOSAL_OUTPUT_SCHEMA:
        raise ProviderError("model_output_contract_invalid")
    proposals = output["proposals"]
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 8:
        raise ProviderError("model_output_contract_invalid")
    required = {
        "optimization_intent",
        "rationale",
        "risk_summary",
        "touched_paths",
        "patch",
    }
    for proposal in proposals:
        if not isinstance(proposal, dict) or set(proposal) != required:
            raise ProviderError("model_output_contract_invalid")
        for name, maximum in (
            ("optimization_intent", 2_000),
            ("rationale", 10_000),
            ("risk_summary", 4_000),
        ):
            _require_model_text(proposal[name], maximum=maximum)
        _require_text(proposal["patch"], maximum=4 * 1024 * 1024)
        touched_paths = proposal["touched_paths"]
        if (
            not isinstance(touched_paths, list)
            or not 1 <= len(touched_paths) <= 4
            or any(not isinstance(path, str) or not path for path in touched_paths)
        ):
            raise ProviderError("model_output_contract_invalid")
        normalized_paths = [_require_model_path(path) for path in touched_paths]
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ProviderError("model_output_contract_invalid")
    return output


def _write_usage(total_tokens: int) -> None:
    raw_path = os.environ.get("HCUOPT_USAGE_PATH")
    if not raw_path:
        raise ProviderError("usage_path_unavailable")
    path = Path(raw_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(
        _canonical_json_bytes(
            {"schema_version": USAGE_SCHEMA, "total_tokens": total_tokens}
        )
    )
    os.replace(temporary, path)


def run() -> bytes:
    raw_input_root = os.environ.get("HCUOPT_INPUT_ROOT")
    if not raw_input_root:
        raise ProviderError("input_root_unavailable")
    config = _load_request(Path(raw_input_root))
    api_key = _load_api_key()
    response = _request_provider(config, api_key)
    _verify_response_model(response, config["model"])
    usage = _extract_usage(response)
    output = _extract_model_output(response)
    _write_usage(usage)
    return _canonical_json_bytes(output)


def main() -> int:
    try:
        sys.stdout.buffer.write(run())
        sys.stdout.buffer.flush()
        return 0
    except ProviderError as error:
        print(f"provider_error:{error.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

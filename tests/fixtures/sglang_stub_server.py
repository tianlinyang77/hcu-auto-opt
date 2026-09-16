#!/usr/bin/env python3
"""Small standard-library server used by SGLang smoke runner tests."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-failures", type=int, default=0)
    parser.add_argument("--generate-status", type=int, default=200)
    parser.add_argument("--generate-delay", type=float, default=0.0)
    parser.add_argument("--response-text", default=" Paris")
    parser.add_argument("--invalid-json", action="store_true")
    parser.add_argument("--invalid-shape", action="store_true")
    parser.add_argument("--exit-immediately", action="store_true")
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--oversized-bytes", type=int, default=0)
    parser.add_argument("--spawn-child-ignore-term", action="store_true")
    parser.add_argument("--import-module")
    args = parser.parse_args()

    if args.exit_immediately:
        return 17
    if args.import_module:
        __import__(args.import_module)
    state = {"ready_count": 0, "generate_count": 0, "child_pid": None}
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if args.spawn_child_ignore_term:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import signal,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); "
                    "time.sleep(60)"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        if child.stdout.readline().strip() != "ready":
            return 18
        state["child_pid"] = child.pid

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health_generate":
                self._send_json(404, {"error": "not found"})
                return
            state["ready_count"] += 1
            status = 503 if state["ready_count"] <= args.ready_failures else 200
            self._send_json(status, {"ready": status == 200})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/generate":
                self._send_json(404, {"error": "not found"})
                return
            state["generate_count"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid request"})
                return
            sampling = request.get("sampling_params", {})
            if "seed" in sampling or sampling.get("sampling_seed") != 0:
                self._send_json(422, {"error": "sampling_seed required"})
                return
            if args.generate_delay:
                time.sleep(args.generate_delay)
            if args.generate_status != 200:
                self._send_json(args.generate_status, {"error": "scripted failure"})
                return
            if args.oversized_bytes:
                body = b"x" * args.oversized_bytes
                self._send_bytes(200, body)
                return
            if args.invalid_json:
                self._send_bytes(200, b"{not-json")
                return
            if args.invalid_shape:
                self._send_json(200, {"text": args.response_text})
                return
            self._send_json(
                200,
                {
                    "text": args.response_text,
                    "output_ids": [1, 2],
                    "meta_info": {
                        "id": "dynamic-request-id",
                        "finish_reason": {"type": "stop", "matched": None},
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "e2e_latency": 123.456,
                        "cached_tokens": 0,
                        "generate_count": state["generate_count"],
                    },
                },
            )

        def log_message(self, format: str, *args: Any) -> None:
            print(format % args, flush=True)

        def _send_json(self, status: int, value: Any) -> None:
            self._send_bytes(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
            )

        def _send_bytes(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
"""Real frontend transport to FastAPI over HTTP, using a synthetic memory repository."""

import json
import runpy
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hcuopt.api.app import create_app


def test_frontend_retry_over_http_creates_one_round(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for frontend transport integration")
    root = Path(__file__).resolve().parents[2]
    suite = runpy.run_path(str(root / "tests/unit/test_operator_plans.py"))
    compiler, repository, preview, coordinator, _ = suite["_startable_suite"](tmp_path)
    app = create_app(repository=repository, operator_profiles=compiler.profiles,
                     operator_plan_compiler=compiler, operator_start_coordinator=coordinator)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.05)
            script = """
import assert from 'node:assert/strict';
import {freezeScriptedStart, submitScriptedStart, savedStartRequests}
  from './web/src/scripted-start.js';
let stdin = ''; for await (const chunk of process.stdin) stdin += chunk;
const {preview, base} = JSON.parse(stdin);
let saved = null;
Object.defineProperty(globalThis, 'sessionStorage', {value: {
  getItem: () => saved, setItem: (_, value) => { saved = value; }
}});
const request = freezeScriptedStart(preview, 'http-test', [], 'web-http-retry-test');
let firstReceipt;
await assert.rejects(submitScriptedStart({}, request, async (path, options) => {
  const response = await fetch(base + path, options);
  assert.equal(response.status, 202);
  firstReceipt = await response.json();
  throw new Error('simulate response lost after commit');
}));
const recovered = savedStartRequests()[0];
assert.deepEqual(recovered, request);
const receipt = await submitScriptedStart({}, recovered,
  (path, options) => fetch(base + path, options));
assert.equal(receipt.replayed, true);
assert.equal(receipt.round_id, firstReceipt.round_id);
assert.equal(receipt.intent_id, firstReceipt.intent_id);
assert.equal(receipt.state, 'finalized');
const readResponse = await fetch(base + '/v1/operator/start-intents/' + receipt.intent_id);
const readback = await readResponse.json();
assert.equal(readback.round_id, receipt.round_id);
assert.equal(readback.automatic_release_allowed, false);
console.log('frontend-http-recovery-passed');
"""
            result = subprocess.run(
                [node, "--input-type=module", "-e", script], cwd=root,
                input=json.dumps({"preview": preview.model_dump(mode="json"),
                                  "base": f"http://127.0.0.1:{port}"}),
                text=True, capture_output=True, timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert "frontend-http-recovery-passed" in result.stdout
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()

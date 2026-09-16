// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectionAuthorization, readInspection, readTerminalEvidence } from '../src/inspection-client.js';

const runId = '091705f9-e1bb-55f4-b07c-de3d58cb173c';
const authorization = inspectionAuthorization('operator', 'a'.repeat(43));
test('uses explicit memory credential on exact same-origin read route, never cookies or redirects', async () => {
  let calls = 0;
  const body = { run_id: runId };
  assert.deepEqual(await readInspection(runId, authorization, {
    origin: 'http://127.0.0.1:4191',
    fetchImpl: async (url, options) => {
      calls++;
      assert.equal(url, `http://127.0.0.1:4191/v1/operator/agent-generations/${runId}/inspection`);
      assert.equal(options.headers.Authorization, authorization);
      assert.equal(options.credentials, 'omit');
      assert.equal(options.redirect, 'error');
      assert.equal(options.cache, 'no-store');
      assert.ok(options.signal);
      return { ok: true, json: async () => body };
    },
  }), body);
  assert.equal(calls, 1);
});
test('rejects unencrypted remote origins and invalid identity before sending credentials', async () => {
  const fetchImpl = () => assert.fail('must not send request');
  await assert.rejects(readInspection(runId, authorization, {origin:'http://inspection.example:8091', fetchImpl}));
  await assert.rejects(readInspection('../tasks', authorization, {origin:'http://localhost', fetchImpl}));
  await assert.rejects(readInspection(runId, undefined, {origin:'http://localhost', fetchImpl}));
  assert.throws(() => inspectionAuthorization('bad:username', 'a'.repeat(43)));
});
test('denial and expiry never return a fallback snapshot', async () => {
  for (const status of [401, 403, 422, 503]) {
    await assert.rejects(readInspection(runId, authorization, {
      origin:'https://inspection.example', fetchImpl:async () => ({ok:false,status}),
    }));
  }
});

test('terminal evidence uses the same safe transport and never falls back to inspection', async () => {
  for (const status of [200, 401, 403, 404, 422, 503]) {
    const calls = [];
    const read = readTerminalEvidence(runId, authorization, {
      origin: 'http://127.0.0.1:4191',
      fetchImpl: async (url, options) => {
        calls.push(url);
        assert.equal(options.headers.Authorization, authorization);
        assert.equal(options.credentials, 'omit');
        assert.equal(options.redirect, 'error');
        assert.equal(options.cache, 'no-store');
        return {ok: status === 200, status, json: async () => ({fixture: true})};
      },
    });
    if (status === 200) assert.deepEqual(await read, {fixture: true});
    else await assert.rejects(read);
    assert.deepEqual(calls, [`http://127.0.0.1:4191/v1/operator/agent-generations/${runId}/evidence`]);
  }
  await assert.rejects(readInspection(runId, authorization, {
    kind: '../tasks', fetchImpl: () => assert.fail('unexpected request'),
    origin: 'https://viewer.invalid',
  }));
});

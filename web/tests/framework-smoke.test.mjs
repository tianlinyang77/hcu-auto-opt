import assert from "node:assert/strict";
import test from "node:test";
import { checkLabel, frameworkState, loadFrameworkInspection } from "../src/framework-smoke.js";

const id = "8b853469-df3b-4aba-8a30-cad83b2b349f";
test("unknown checks are never passing", () => {
  assert.equal(checkLabel(null), "尚无证据");
  assert.equal(checkLabel("true"), "尚无证据");
  assert.equal(checkLabel(false), "未通过");
  assert.equal(frameworkState("rejected"), "验收未通过");
});
test("only a matching task response with no release authority is accepted", async () => {
  const data = { schema: "framework-smoke-inspection-v1", task: { task_id: id },
    performance_conclusion: "not_measured", automatic_release_allowed: false };
  const fetcher = async (path, options) => {
    assert.equal(path, `/v1/framework-smoke-inspection/${id}`);
    assert.equal(options.headers.Authorization, "Bearer independent-task-key");
    assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => data };
  };
  assert.deepEqual(await loadFrameworkInspection(id, "independent-task-key", fetcher), data);
  data.automatic_release_allowed = true;
  await assert.rejects(loadFrameworkInspection(id, "independent-task-key", fetcher), /不匹配/);
});
test("authentication failure hides untrusted server messages", async () => {
  await assert.rejects(loadFrameworkInspection(id, "wrong", async () => ({ok: false, status: 401})), /凭据/);
});
test("invalid task rejected before network access", async () => {
  await assert.rejects(loadFrameworkInspection("../../secret", "key", () => assert.fail()), /编号无效/);
});

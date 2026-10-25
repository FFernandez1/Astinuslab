import test from "node:test";
import assert from "node:assert/strict";
import { selectActivity } from "../../../../src/astinuslab/web/static/packages/activity.js";

test("empty durable activity yields empty maps and no selected panels", () => {
  assert.deepEqual(selectActivity({ prepares: [], instances: [], verifications: [] }), {
    prepares: {},
    operations: {},
    verifications: {},
    prepare: null,
    operation: null,
    verification: null,
  });
});

test("selection preserves server order and retains every prepare/instance", () => {
  const prepare = { id: "z", updated_at: "2020-01-01" };
  const other = { id: "a", updated_at: "2030-01-01" };
  const operation = { id: "i-first" };
  const verification = { id: "v-first" };
  const input = {
    prepares: [prepare, other],
    instances: [operation, { id: "i-other" }],
    verifications: [verification, { id: "v-other" }],
  };
  const before = JSON.parse(JSON.stringify(input));
  assert.deepEqual(selectActivity(input), {
    prepare,
    operation,
    verification,
    prepares: { z: prepare, a: other },
    operations: { "i-first": operation, "i-other": { id: "i-other" } },
    verifications: { "v-first": verification, "v-other": { id: "v-other" } },
  });
  assert.deepEqual(input, before);
});

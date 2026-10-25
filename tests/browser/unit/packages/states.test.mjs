import test from "node:test";
import assert from "node:assert/strict";

const {
  INSTANCE_CLEANUP_INCOMPLETE,
  INSTANCE_POLL_STOP,
  INSTANCE_TERMINAL,
  PREPARE_TERMINAL,
  VERIFICATION_TERMINAL,
} = await import("../../../../src/astinuslab/web/static/packages/states.js");

test("package lifecycle state sets match durable terminal vocabulary", () => {
  assert.deepEqual([...INSTANCE_TERMINAL], ["failed", "destroyed"]);
  assert.deepEqual([...VERIFICATION_TERMINAL], ["verified", "failed", "cancelled"]);
  assert.deepEqual([...PREPARE_TERMINAL], ["completed", "failed", "cancelled"]);
  assert.equal(INSTANCE_POLL_STOP.has("ready"), true);
  assert.equal(INSTANCE_POLL_STOP.has(INSTANCE_CLEANUP_INCOMPLETE), true);
});

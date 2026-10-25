import test from "node:test";
import assert from "node:assert/strict";

const { captureStreamOnError, MAX_STREAM_ERRORS } =
  await import("../../../../src/astinuslab/web/static/playground/capture-stream.js");

test("closes silently after a terminal event", () => {
  const r = captureStreamOnError({ terminalReached: true, errorCount: 1, maxErrors: 5 });
  assert.deepEqual(r, { action: "close", lost: false });
});

test("retries on an early transient error", () => {
  const r = captureStreamOnError({ terminalReached: false, errorCount: 1, maxErrors: 5 });
  assert.deepEqual(r, { action: "retry", lost: false });
});

test("closes with lost flag once the error budget is exhausted", () => {
  const r = captureStreamOnError({ terminalReached: false, errorCount: 5, maxErrors: 5 });
  assert.deepEqual(r, { action: "close", lost: true });
});

test("exports a sane default budget", () => {
  assert.ok(MAX_STREAM_ERRORS >= 3);
});

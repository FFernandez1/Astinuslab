import test from "node:test";
import assert from "node:assert/strict";

const { deriveStatus, cleanupMessage } =
  await import("../../../../src/astinuslab/web/static/playground/render-top.js");

function status(vms, networks = []) {
  return deriveStatus({ session: { vms, networks } });
}

test("cleanup requires explicit successful states and keeps failures distinct", () => {
  assert.match(cleanupMessage({ phase: "requesting" }), /Requesting/);
  assert.match(cleanupMessage({ status: "destroy_requested" }), /requested/);
  for (const state of ["destroying", "failure_cleanup"])
    assert.match(cleanupMessage({ status: state }), /Cleaning up/);
  for (const state of ["completed", "cancelled"])
    assert.match(cleanupMessage({ status: state }), /Session ended/);
  for (const state of ["destroy_failed", "failed"])
    assert.match(cleanupMessage({ status: state }), /incomplete/);
  assert.match(cleanupMessage({ status: "running" }), /has not completed/);
  assert.match(
    cleanupMessage({ status: "destroying", unavailable: "missing" }),
    /record unavailable/,
  );
  assert.match(
    cleanupMessage({ status: "destroying", unavailable: "transport" }),
    /status unavailable/,
  );
});

test("a failed VM is counted, not reported as zero", () => {
  const s = status([
    { id: "a", state: "running" },
    { id: "b", state: "failed" },
  ]);
  assert.equal(s.hostsText, "1 of 2 hosts running");
  assert.equal(s.failedText, "1 failed");
});

test("teardown_failed counts as a failure too", () => {
  assert.equal(status([{ id: "a", state: "teardown_failed" }]).failedText, "1 failed");
});

test("provisioning and power-transition states count as starting", () => {
  const s = status([
    { id: "a", state: "provisioning_workload_active" },
    { id: "b", state: "queued" },
    { id: "c", state: "powering_on" },
  ]);
  assert.equal(s.startingText, "3 starting");
  assert.equal(s.hostsText, "0 of 3 hosts running");
});

test("a quiet lab shows no zero-counters", () => {
  const s = status([{ id: "a", state: "running" }]);
  assert.equal(s.startingText, "");
  assert.equal(s.failedText, "");
});

test("an empty session still reads as a sentence", () => {
  const s = status([]);
  assert.equal(s.hostsText, "no hosts yet");
  assert.equal(s.networksText, "no networks yet");
});

test("the review affordance points at the first failed machine", () => {
  const s = status([
    { id: "a", state: "running" },
    { id: "b", state: "failed" },
    { id: "c", state: "failed" },
  ]);
  assert.equal(s.firstFailedId, "b");
});

test("no failures means nothing to review", () => {
  assert.equal(status([{ id: "a", state: "running" }]).firstFailedId, null);
});

test("a single network is described in full, several are just named", () => {
  const one = status([], [{ name: "default", subnet_cidr: "10.42.1.0/24", isolated: true }]);
  assert.equal(one.networksText, "1 network · default 10.42.1.0/24 · isolated");

  const routed = status([], [{ name: "dmz", subnet_cidr: "10.42.2.0/24", isolated: false }]);
  assert.match(routed.networksText, /shared$/);

  const many = status([], [{ name: "dmz" }, { name: "inside" }]);
  assert.equal(many.networksText, "2 networks · dmz · inside");
});

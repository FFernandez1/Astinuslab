import test from "node:test";
import assert from "node:assert/strict";

const { vmLifecycle, VM_LIFECYCLE } =
  await import("../../../../src/astinuslab/web/static/playground/vm-state.js");

test("pre-running backend states all classify as starting", () => {
  for (const state of [
    "queued",
    "pending",
    "creating",
    "starting",
    "booting",
    "provisioning_workload",
    "provisioning_workload_active",
    "powering_on",
    "rebooting",
  ]) {
    assert.equal(vmLifecycle(state), VM_LIFECYCLE.STARTING, `${state} should be starting`);
  }
});

test("running, stopped and failure states are distinguished", () => {
  assert.equal(vmLifecycle("running"), VM_LIFECYCLE.RUNNING);
  for (const state of ["stopped", "shutoff", "shutdown", "stopping", "powering_off"]) {
    assert.equal(vmLifecycle(state), VM_LIFECYCLE.STOPPED, `${state} should be stopped`);
  }
  assert.equal(vmLifecycle("failed"), VM_LIFECYCLE.FAILED);
  assert.equal(vmLifecycle("teardown_failed"), VM_LIFECYCLE.FAILED);
});

test("only genuinely removed states count as gone", () => {
  for (const state of ["destroy_queued", "destroying", "destroyed"]) {
    assert.equal(vmLifecycle(state), VM_LIFECYCLE.GONE, `${state} should be gone`);
  }
  // A failure is wreckage the operator must still see — never "gone".
  assert.notEqual(vmLifecycle("failed"), VM_LIFECYCLE.GONE);
  assert.notEqual(vmLifecycle("teardown_failed"), VM_LIFECYCLE.GONE);
});

test("unknown states fall back to other rather than being hidden", () => {
  assert.equal(vmLifecycle("some_future_state"), VM_LIFECYCLE.OTHER);
  assert.equal(vmLifecycle(undefined), VM_LIFECYCLE.OTHER);
});

import test from "node:test";
import assert from "node:assert/strict";

const { reconcile } =
  await import("../../../../src/astinuslab/web/static/playground/session-identity.js");

test("an identical payload keeps every previous identity", () => {
  const previous = { id: "pg-1", vms: [{ id: "vm-a", nics: [{ ip: "10.0.0.5" }] }] };
  const next = JSON.parse(JSON.stringify(previous));
  assert.equal(reconcile(previous, next), previous);
});

test("only the branch that moved becomes a new object", () => {
  const previous = {
    vms: [
      { id: "vm-a", state: "running", cpu_pct: 3.1 },
      { id: "vm-b", state: "running", cpu_pct: 0 },
    ],
    networks: [{ id: "net-a", name: "lab" }],
  };
  const next = {
    vms: [
      { id: "vm-a", state: "running", cpu_pct: 3.1 },
      { id: "vm-b", state: "running", cpu_pct: 42.7 },
    ],
    networks: [{ id: "net-a", name: "lab" }],
  };
  const merged = reconcile(previous, next);

  assert.notEqual(merged, previous, "the session changed");
  assert.notEqual(merged.vms, previous.vms, "the vm list changed");
  assert.equal(merged.vms[0], previous.vms[0], "the untouched VM keeps its identity");
  assert.notEqual(merged.vms[1], previous.vms[1], "the busy VM does not");
  assert.equal(merged.networks, previous.networks, "networks did not move");
  assert.equal(merged.vms[1].cpu_pct, 42.7);
});

test("added, removed and reordered entries produce a new array", () => {
  const a = { id: "vm-a" };
  const b = { id: "vm-b" };
  const previous = { vms: [a, b] };

  const grown = reconcile(previous, { vms: [{ id: "vm-a" }, { id: "vm-b" }, { id: "vm-c" }] });
  assert.equal(grown.vms.length, 3);
  assert.equal(grown.vms[0], a, "a survivor at the same index is reused");

  const shrunk = reconcile(previous, { vms: [{ id: "vm-a" }] });
  assert.equal(shrunk.vms.length, 1);
  assert.equal(shrunk.vms[0], a);

  const reordered = reconcile(previous, { vms: [{ id: "vm-b" }, { id: "vm-a" }] });
  assert.notEqual(reordered.vms, previous.vms);
  assert.deepEqual(reordered.vms, [{ id: "vm-b" }, { id: "vm-a" }]);
});

test("a changed key set is a change even when both sides read undefined", () => {
  const previous = { errors: { session: undefined } };
  assert.equal(reconcile(previous, { errors: { session: undefined } }), previous);
  const widened = reconcile(previous, { errors: { session: undefined, vmAction: undefined } });
  assert.notEqual(widened.errors, previous.errors);
});

test("null, primitives and type flips fall through to the new value", () => {
  assert.equal(reconcile({ a: 1 }, null), null);
  assert.equal(reconcile(null, null), null);
  assert.equal(reconcile(undefined, null), null);
  assert.equal(reconcile("running", "stopped"), "stopped");
  const array = [1, 2];
  assert.equal(reconcile({ 0: 1, 1: 2 }, array), array, "object -> array is not a reuse");
});

test("a self-referential payload terminates instead of recursing forever", () => {
  const cyclic = { name: "loop" };
  cyclic.self = cyclic;
  const merged = reconcile({ name: "loop" }, cyclic);
  assert.equal(merged.name, "loop");
});

test("a steady running VM no longer churns once the dead metrics blob is gone", () => {
  // mergeMetrics used to copy the whole sample onto the VM. sampled_at and the
  // byte counters move on every tick and nothing renders them, so the board
  // repainted for a lab that had not changed.
  const withBlob = (sampledAt, rx) => ({
    id: "vm-a",
    cpu_pct: 0,
    mem_pct: 41.2,
    metrics: { cpu_pct: 0, mem_pct: 41.2, sampled_at: sampledAt, rx_bytes: { vnet0: rx } },
  });
  assert.notEqual(
    reconcile(withBlob("10:00:00", 100), withBlob("10:00:05", 812)),
    withBlob("10:00:00", 100),
  );

  const lean = () => ({ id: "vm-a", cpu_pct: 0, mem_pct: 41.2 });
  const previous = lean();
  assert.equal(reconcile(previous, lean()), previous);
});

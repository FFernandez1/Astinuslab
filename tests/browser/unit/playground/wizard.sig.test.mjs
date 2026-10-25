import test from "node:test";
import assert from "node:assert/strict";

const { networkSignature } =
  await import("../../../../src/astinuslab/web/static/playground/wizard.sig.js");

test("networkSignature is stable across two distinct session objects with same networks", () => {
  const a = { networks: [{ id: "n1", state: "active", isolated: true }] };
  const b = { networks: [{ id: "n1", state: "active", isolated: true }] };
  assert.equal(networkSignature(a), networkSignature(b));
});

test("networkSignature changes when a network state changes", () => {
  const a = { networks: [{ id: "n1", state: "pending", isolated: true }] };
  const b = { networks: [{ id: "n1", state: "active", isolated: true }] };
  assert.notEqual(networkSignature(a), networkSignature(b));
});

test("networkSignature changes when isolation flips", () => {
  const a = { networks: [{ id: "n1", state: "active", isolated: true }] };
  const b = { networks: [{ id: "n1", state: "active", isolated: false }] };
  assert.notEqual(networkSignature(a), networkSignature(b));
});

test("networkSignature handles null and empty networks", () => {
  assert.equal(networkSignature(null), "");
  assert.equal(networkSignature({ networks: [] }), "");
});

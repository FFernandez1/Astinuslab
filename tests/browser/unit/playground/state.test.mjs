import test from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = {
  _d: {},
  getItem(k) {
    return k in this._d ? this._d[k] : null;
  },
  setItem(k, v) {
    this._d[k] = String(v);
  },
};

const { getState, setState, subscribe } =
  await import("../../../../src/astinuslab/web/static/playground/state.js");

test("setState merges and notifies subscribers on slice change", () => {
  let seen = null;
  const unsub = subscribe(
    (s) => s.selectedId,
    (v) => {
      seen = v;
    },
  );
  setState((prev) => ({ ...prev, selectedId: "vm-1" }));
  assert.equal(getState().selectedId, "vm-1");
  assert.equal(seen, "vm-1");
  unsub();
});

test("subscriber not notified when its slice is unchanged", () => {
  let calls = 0;
  const unsub = subscribe(
    (s) => s.selectedId,
    () => {
      calls += 1;
    },
  );
  const baseline = calls;
  setState((prev) => ({ ...prev, mapScaleUnrelated: Math.random() }));
  assert.equal(calls, baseline, "unrelated change must not notify");
  unsub();
});

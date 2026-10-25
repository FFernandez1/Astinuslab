import assert from "node:assert/strict";
import test from "node:test";

import { vmsToTearDown } from "../../helpers/vm-teardown.js";

test("a VM created by the tier is destroyed even when nothing was recorded", () => {
  // The leak: the readiness poll timed out, so the test body never assigned an
  // id. Teardown must still find the machine it booted.
  assert.deepEqual(
    vmsToTearDown({
      preexistingIds: ["vm-old"],
      currentVms: [{ id: "vm-old" }, { id: "vm-new" }],
      recordedIds: [],
    }),
    ["vm-new"],
  );
});

test("a pre-existing lab is never touched", () => {
  assert.deepEqual(
    vmsToTearDown({
      preexistingIds: ["vm-a", "vm-b"],
      currentVms: [{ id: "vm-a" }, { id: "vm-b" }],
      recordedIds: ["vm-a"],
    }),
    [],
  );
});

test("a recorded id that has dropped out of the session is still destroyed", () => {
  assert.deepEqual(
    vmsToTearDown({ preexistingIds: [], currentVms: [], recordedIds: ["vm-gone"] }),
    ["vm-gone"],
  );
});

test("an id counted twice is destroyed once", () => {
  assert.deepEqual(
    vmsToTearDown({
      preexistingIds: [],
      currentVms: [{ id: "vm-1" }, { id: "vm-1" }],
      recordedIds: ["vm-1"],
    }),
    ["vm-1"],
  );
});

test("a session listing that could not be read still destroys what was recorded", () => {
  // currentVms() swallows a failed fetch and returns []; teardown must not
  // treat that as "nothing to clean up".
  assert.deepEqual(
    vmsToTearDown({ preexistingIds: ["vm-old"], currentVms: [], recordedIds: ["vm-new"] }),
    ["vm-new"],
  );
});

test("malformed rows are ignored rather than destroying undefined", () => {
  assert.deepEqual(
    vmsToTearDown({
      preexistingIds: [],
      currentVms: [{}, null, { id: "" }, { id: "vm-ok" }],
      recordedIds: [undefined],
    }),
    ["vm-ok"],
  );
});

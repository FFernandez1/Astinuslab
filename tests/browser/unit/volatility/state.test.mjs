import test from "node:test";
import assert from "node:assert/strict";
import {
  state,
  ACTIVE_RUN_KEY,
  saveActiveRun,
  readActiveRun,
  invalidateContext,
  captureContext,
  isCurrentContext,
} from "../../../../src/astinuslab/web/static/volatility/state.js";

test("active-run persistence contains identity only and explicit context replacement clears it", (t) => {
  const values = new Map();
  const previous = globalThis.sessionStorage;
  globalThis.sessionStorage = {
    getItem: (key) => values.get(key),
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  t.after(() => {
    globalThis.sessionStorage = previous;
  });
  state.dump = {
    job_id: "job",
    selection_id: "artifact-choice",
    artifact_id: "artifact",
    file: "/private/dump",
  };
  state.plugin = "yarascan.YaraScan";
  state.runId = "accepted";
  state.yaraRules = "SENSITIVE_RULE_TEXT";
  saveActiveRun();
  assert.deepEqual(readActiveRun(), {
    job_id: "job",
    selection_id: "artifact-choice",
    artifact_id: "artifact",
    plugin: "yarascan.YaraScan",
    run_id: "accepted",
  });
  assert.doesNotMatch(values.get(ACTIVE_RUN_KEY), /SENSITIVE|private/);
  const old = captureContext();
  state.observing = true;
  invalidateContext();
  assert.equal(readActiveRun(), null);
  assert.equal(state.observing, false);
  assert.equal(isCurrentContext(old), false);
  delete state.yaraRules;
  values.set(ACTIVE_RUN_KEY, '{"run_id": "incomplete"}');
  assert.equal(readActiveRun(), null);
  assert.equal(values.has(ACTIVE_RUN_KEY), false);
});

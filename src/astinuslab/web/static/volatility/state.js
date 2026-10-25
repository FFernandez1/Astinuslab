// Shared mutable state for the volatility workbench. The poll/tick timer handles
// live on the state object so any module can reassign them (module imports are
// read-only bindings).
const state = {
  dumps: [],
  dump: null,
  catalog: { categories: [], count: 0 },
  plugin: null,
  tab: "table",
  filter: "",
  osLoaded: null,
  runId: null,
  running: false,
  observing: false,
  reconnect: null,
  lastResult: null,
  pollFailures: 0,
  pollTimer: null,
  tickTimer: null,
  generation: 0,
};

export { state };

export const ACTIVE_RUN_KEY = "astinuslab.volatility.active-run.v1";
export function clearActiveRun() {
  try {
    globalThis.sessionStorage?.removeItem(ACTIVE_RUN_KEY);
  } catch (_) {
    /* optional persistence */
  }
}
export function saveActiveRun() {
  if (!state.runId || !state.dump || !state.plugin) return;
  try {
    globalThis.sessionStorage?.setItem(
      ACTIVE_RUN_KEY,
      JSON.stringify({
        job_id: state.dump.job_id,
        run_id: state.runId,
        plugin: state.plugin,
        selection_id: state.dump.selection_id || state.dump.job_id,
        artifact_id: state.dump.artifact_id || null,
      }),
    );
  } catch (_) {
    /* In-memory observation remains usable. */
  }
}
export function readActiveRun() {
  try {
    const saved = JSON.parse(globalThis.sessionStorage?.getItem(ACTIVE_RUN_KEY) || "null");
    if (
      saved &&
      ["job_id", "run_id", "plugin", "selection_id"].every(
        (key) => typeof saved[key] === "string" && saved[key],
      ) &&
      (saved.artifact_id === null || typeof saved.artifact_id === "string")
    )
      return saved;
  } catch (_) {
    /* Ignore invalid tab state. */
  }
  clearActiveRun();
  return null;
}

// Each selection/rerun owns an immutable identity. A → B → A is still a new
// generation: equality of the currently selected strings is not sufficient.
export function invalidateContext() {
  stopTimers();
  state.generation += 1;
  state.runId = null;
  state.running = false;
  state.observing = false;
  state.reconnect = null;
  clearActiveRun();
  state.lastResult = null;
}

export function captureContext() {
  return Object.freeze({
    generation: state.generation,
    jobId: state.dump?.job_id,
    artifactId: state.dump?.artifact_id,
    file: state.dump?.file,
    plugin: state.plugin,
  });
}

export function isCurrentContext(context) {
  return (
    !!context &&
    context.generation === state.generation &&
    context.jobId === state.dump?.job_id &&
    context.artifactId === state.dump?.artifact_id &&
    context.plugin === state.plugin
  );
}

export function stopTimers() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
  if (state.tickTimer) {
    clearInterval(state.tickTimer);
    state.tickTimer = null;
  }
}

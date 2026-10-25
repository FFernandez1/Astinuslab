// Single shared store for the playground page. Modules subscribe to
// selector outputs and re-render only when their slice changes.
let state = {
  session: null,
  topology: { nodes: [] },
  catalog: { target: [], attacker: [], network: [] },
  catalogStatus: { target: "loading", attacker: "loading", network: "loading" },
  selectedId: null,
  ui: {
    mapScale: 1,
    saveState: "idle",
    createRole: null,
    createWizard: null,
    stream: null,
    createNetworkOpen: false,
    lastStream: null,
    // The VM whose acquisition is in flight, or null. A capture is irreversible
    // and the endpoint has no idempotency key, so the second click has to be
    // stopped here.
    captureBusyVmId: null,
  },
  errors: {},
  cleanupFailure: null,
  cleanup: null,
  // Not every surprise is a failure: a notice reports something the backend
  // did differently from what was asked, which still has to reach the operator.
  notices: {},
  signedOut: false,
};
const listeners = [];

export const CLEANUP_STORAGE_KEY = "astinuslab.playground.cleanup-session.v1";
export const cleanupSucceeded = (cleanup) => ["cancelled", "completed"].includes(cleanup?.status);
export const cleanupTerminal = (cleanup) =>
  cleanupSucceeded(cleanup) || ["destroy_failed", "failed"].includes(cleanup?.status);

export function persistCleanup(id) {
  try {
    if (id) globalThis.sessionStorage?.setItem(CLEANUP_STORAGE_KEY, id);
    else globalThis.sessionStorage?.removeItem(CLEANUP_STORAGE_KEY);
  } catch (_) {
    /* In-memory ownership still protects this page. */
  }
}

export function restoreCleanup() {
  try {
    const id = globalThis.sessionStorage?.getItem(CLEANUP_STORAGE_KEY);
    if (id)
      setState((prev) => ({
        ...prev,
        cleanup: { session_id: id, phase: "accepted", status: null },
      }));
  } catch (_) {
    /* Storage may be disabled. */
  }
}

function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  const ak = Object.keys(a),
    bk = Object.keys(b);
  if (ak.length !== bk.length) return false;
  for (const k of ak) if (!Object.is(a[k], b[k])) return false;
  return true;
}

export function getState() {
  return state;
}

export function setState(updater) {
  const next = typeof updater === "function" ? updater(state) : { ...state, ...updater };
  if (next === state) return;
  const prev = state;
  state = next;
  for (const entry of listeners) {
    const a = entry.selector(prev);
    const b = entry.selector(next);
    if (!shallowEqual(a, b)) entry.fn(b, a);
  }
}

export function subscribe(selector, fn) {
  const entry = { selector, fn };
  listeners.push(entry);
  fn(selector(state), undefined);
  return () => {
    const i = listeners.indexOf(entry);
    if (i >= 0) listeners.splice(i, 1);
  };
}

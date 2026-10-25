// Small observable state store for the package page. It is intentionally
// serialisable so a refresh never becomes a source of truth for operations;
// durable status always comes from the API.
let state = {
  packages: [],
  inspection: null,
  selectedPackageId: null,
  preflight: {},
  prepares: {},
  operations: {},
  verification: null,
  verifications: {},
  error: "",
  busy: false,
};
const listeners = [];

function shallowEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const a = Object.keys(left);
  const b = Object.keys(right);
  return a.length === b.length && a.every((key) => Object.is(left[key], right[key]));
}

export function getState() {
  return state;
}

export function setState(updater) {
  const next = typeof updater === "function" ? updater(state) : { ...state, ...updater };
  if (next === state) return;
  const previous = state;
  state = next;
  for (const listener of listeners) {
    const before = listener.selector(previous);
    const after = listener.selector(next);
    if (!shallowEqual(before, after)) listener.callback(after, before);
  }
}

export function subscribe(selector, callback) {
  const listener = { selector, callback };
  listeners.push(listener);
  callback(selector(state), undefined);
  return () => {
    const index = listeners.indexOf(listener);
    if (index >= 0) listeners.splice(index, 1);
  };
}

export function resetState() {
  listeners.length = 0;
  state = {
    packages: [],
    inspection: null,
    selectedPackageId: null,
    preflight: {},
    prepares: {},
    operations: {},
    verification: null,
    verifications: {},
    error: "",
    busy: false,
  };
}

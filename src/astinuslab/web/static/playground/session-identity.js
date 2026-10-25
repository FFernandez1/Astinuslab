// Structural reuse for polled payloads.
//
// The store gates re-renders with a shallow-equality check on each subscriber's
// selected slice (see state.js). A poll parses fresh JSON, and normalizeSession
// then rebuilds every VM object and both arrays, so `Object.is` on
// `session.vms` never held: an idle lab repainted the map, the details panel,
// the header and the wizard every five seconds.
//
// reconcile walks the new payload against the one already in the store and
// hands back the previous reference wherever the content is identical. The gate
// then closes on an unchanged poll and opens only for what actually moved.
// Everything here is pure; the payloads are JSON, so plain objects and arrays
// are the only containers it needs to understand.

// Session -> vms[] -> nics[] -> ... . Real payloads are far shallower than this;
// the cap only keeps a malformed one from recursing without end.
const MAX_DEPTH = 12;

function isPlainObject(value) {
  if (typeof value !== "object" || value === null) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

export function reconcile(previous, next, depth = 0) {
  if (Object.is(previous, next)) return previous;
  if (depth >= MAX_DEPTH) return next;

  if (Array.isArray(previous) && Array.isArray(next)) {
    const merged = next.map((item, index) => reconcile(previous[index], item, depth + 1));
    if (merged.length !== previous.length) return merged;
    return merged.every((item, index) => Object.is(item, previous[index])) ? previous : merged;
  }

  if (isPlainObject(previous) && isPlainObject(next)) {
    const nextKeys = Object.keys(next);
    let identical = nextKeys.length === Object.keys(previous).length;
    const merged = {};
    for (const key of nextKeys) {
      const value = reconcile(previous[key], next[key], depth + 1);
      merged[key] = value;
      // A key the previous object never had is a change even when both read
      // undefined, because the key set is part of the shape.
      if (identical && (!Object.is(value, previous[key]) || !(key in previous))) identical = false;
    }
    return identical ? previous : merged;
  }

  return next;
}

// Global error surface.
//
// `state.errors` is a bag of keys written from anywhere. Most are claimed by the
// component that raised them -- a NIC attach failure belongs next to the NIC
// control -- but several keys used to be written and never read, so the failure
// was invisible: a refused destroy looked like a dead button, and a dead backend
// still rendered the lab as live.
//
// This module renders every key that no component claims. COMPONENT_OWNED is the
// only place ownership is declared, so a new error key is visible by default and
// silence has to be chosen deliberately rather than happening by omission.
import { el } from "./ui.js";
import { subscribe } from "./state.js";

const COMPONENT_OWNED = new Set([
  "vmAction", // render-details.js, in the VM panel
  "vmDelete", // render-details.js, in the VM panel
  "nicAttach", // render-details.js, beside the NIC control
  "nicDetach", // render-details.js, beside the NIC control
  "networkDelete", // render-details.js, in the network panel
  "endSession", // render-top.js, #end-session-error
  "recipeImport", // render-top.js, #import-recipe-error
  "topology", // render-map.js, #map-save-status shows "save failed"
]);

// A stale session is a state, not a message: the page must stop presenting
// itself as live while it is set.
const STALENESS_KEY = "session";

function unclaimed(errors) {
  return Object.entries(errors || {})
    .filter(([key, message]) => message && !COMPONENT_OWNED.has(key))
    .map(([, message]) => message);
}

// A 401 is not "the backend is unwell", it is "you are signed out". Saying so
// and offering the way back beats echoing the API's own wording, which tells an
// operator nothing they can act on. Navigating away on its own would be worse
// still: anything typed into an open wizard would go with it.
function signedOutNotice() {
  const tt = window.t || ((k, fallback) => fallback || k);
  const next = window.location.pathname || "/";
  return [
    el("span", { text: tt("pg_signed_out", "Your session has expired.") + " " }),
    el("a", {
      text: tt("pg_signed_out_action", "Sign in again"),
      attrs: { href: `/login?next=${next}` },
    }),
  ];
}

function render({ errors, notices, signedOut }) {
  const host = document.getElementById("pg-alerts");
  if (host) {
    host.replaceChildren();
    const failures = signedOut ? [] : unclaimed(errors);
    // Notices are not failures. A substituted subnet is the backend doing
    // something reasonable that the operator did not ask for, and colouring it
    // like an error would be as misleading as hiding it.
    const remarks = Object.values(notices || {}).filter(Boolean);
    if (signedOut) host.append(...signedOutNotice());
    else if (failures.length) host.textContent = failures.join(" · ");
    else if (remarks.length) host.textContent = remarks.join(" ");
    host.dataset.severity = signedOut || failures.length ? "error" : "notice";
    host.hidden = !signedOut && failures.length === 0 && remarks.length === 0;
  }
  const root = document.getElementById("playground-root");
  if (root) {
    if (signedOut) root.dataset.signedOut = "true";
    else delete root.dataset.signedOut;
    // A signed-out page is stale by definition: its last render predates the
    // poll that failed.
    if (signedOut || errors?.[STALENESS_KEY]) root.dataset.stale = "true";
    else delete root.dataset.stale;
  }
}

export function mountAlerts() {
  subscribe((s) => ({ errors: s.errors, notices: s.notices, signedOut: s.signedOut }), render);
}

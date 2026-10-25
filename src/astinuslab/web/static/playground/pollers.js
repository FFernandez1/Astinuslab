// Session refresh poller. Pauses when tab is hidden so we don't burn
// CPU on a backgrounded page.
import { getCleanupStatus, getRecentFailure, getSession, getSessionMetrics } from "./api.js";
import { reconcile } from "./session-identity.js";
import { getState, setState, cleanupTerminal } from "./state.js";

const INTERVAL_MS = 5000;
let timer = null;
let visHandler = null;
let active = false;
let generation = 0;
let inFlight = false;
let refreshRequested = false;
let controller = null;

// Only the two percentages are carried onto the VM. The sample also holds
// sampled_at and rx/tx byte counters, which move on every sample and nothing
// renders -- keeping them here gave each running VM a new identity every tick
// and repainted the board for a lab that had not changed.
function mergeMetrics(session, metrics) {
  if (!session?.vms || !metrics) return session;
  return {
    ...session,
    vms: session.vms.map((vm) => {
      const m = metrics[vm.id];
      return m ? { ...vm, cpu_pct: m.cpu_pct, mem_pct: m.mem_pct } : vm;
    }),
  };
}

// The poll clears any previous session error. Rebuilding the errors object when
// there was nothing to clear would re-render the alerts panel on every tick.
function clearedSessionError(errors) {
  if (!errors || errors.session === undefined) return errors;
  return { ...errors, session: undefined };
}

async function tick(owner, signal) {
  const owns = () => active && owner === generation;
  const cleanup = getState().cleanup;
  try {
    if (cleanup) {
      if (cleanup.phase === "requesting" || cleanupTerminal(cleanup)) return;
      const result = await getCleanupStatus(cleanup.session_id, { signal });
      if (!owns() || getState().cleanup?.session_id !== cleanup.session_id) return;
      if (result?.session_id !== cleanup.session_id)
        throw new Error("Cleanup response identity mismatch");
      setState((prev) => ({
        ...prev,
        signedOut: false,
        cleanup: reconcile(prev.cleanup, { ...prev.cleanup, ...result, unavailable: null }),
      }));
      return;
    }
    const session = await getSession({ signal });
    if (!owns()) return;
    let metrics = null;
    if (session?.vms?.some((vm) => vm.state === "running")) {
      try {
        metrics = await getSessionMetrics({ signal });
      } catch (error) {
        if (error.status === 401) throw error;
        metrics = null;
      }
    }
    if (!owns()) return;
    let cleanupFailure;
    try {
      cleanupFailure = await getRecentFailure({ signal });
    } catch (error) {
      if (error.status === 401) throw error;
      // Advisory failures retain the previous known result.
    }
    if (!owns()) return;
    const sessionWithMetrics = mergeMetrics(session, metrics);
    setState((prev) => {
      // Reuse the identities already in the store wherever the payload repeats
      // itself, so subscribers whose slice did not move stay unrendered.
      const nextSession = reconcile(prev.session, sessionWithMetrics);
      const ids = new Set([
        ...(nextSession?.vms || []).map((vm) => vm.id),
        ...(nextSession?.networks || []).map((net) => net.id),
      ]);
      const selectedId =
        prev.selectedId && ids.has(prev.selectedId)
          ? prev.selectedId
          : nextSession?.vms?.[0]?.id || null;
      const errors = clearedSessionError(prev.errors);
      const failure =
        cleanupFailure === undefined
          ? prev.cleanupFailure
          : reconcile(prev.cleanupFailure, cleanupFailure || null);
      // A poll that found nothing new returns the state untouched; setState then
      // notifies no one at all.
      if (
        nextSession === prev.session &&
        selectedId === prev.selectedId &&
        errors === prev.errors &&
        failure === prev.cleanupFailure &&
        prev.signedOut === false
      ) {
        return prev;
      }
      return {
        ...prev,
        session: nextSession,
        selectedId,
        signedOut: false,
        errors,
        cleanupFailure: failure,
      };
    });
  } catch (err) {
    if (!owns()) return;
    if (cleanup) {
      setState((prev) => ({
        ...prev,
        signedOut: err.status === 401 || prev.signedOut,
        cleanup: { ...prev.cleanup, unavailable: err.status === 404 ? "missing" : "transport" },
      }));
      return;
    }
    // 401 means the browser session expired, not that the backend is unwell.
    // It needs its own presentation -- an operator cannot act on "admin bearer
    // token required" -- so it is kept out of the generic error slot.
    if (err.status === 401) {
      setState((prev) => ({
        ...prev,
        signedOut: true,
        errors: { ...prev.errors, session: undefined },
      }));
      return;
    }
    setState((prev) => ({ ...prev, errors: { ...prev.errors, session: err.message } }));
  }
}

function clearTimer() {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

function requestRefresh() {
  refreshRequested = true;
  clearTimer();
  if (!active || inFlight || document.visibilityState !== "visible") return;
  refreshRequested = false;
  inFlight = true;
  controller = new AbortController();
  tick(generation, controller.signal).finally(() => {
    // Even an ignored abort keeps this physical request owned until settlement.
    inFlight = false;
    controller = null;
    if (!active || document.visibilityState !== "visible") return;
    if (refreshRequested) requestRefresh();
    else if (!cleanupTerminal(getState().cleanup)) timer = setTimeout(requestRefresh, INTERVAL_MS);
  });
}

export function refreshSessionPoll() {
  generation += 1;
  controller?.abort();
  if (active) requestRefresh();
}

export function startSessionPoll() {
  stopSessionPoll();
  active = true;
  visHandler = () => {
    if (document.visibilityState === "visible") requestRefresh();
    else clearTimer();
  };
  document.addEventListener("visibilitychange", visHandler);
  requestRefresh();
}

export function stopSessionPoll() {
  active = false;
  generation += 1;
  refreshRequested = false;
  clearTimer();
  controller?.abort();
  if (visHandler) document.removeEventListener("visibilitychange", visHandler);
  visHandler = null;
}

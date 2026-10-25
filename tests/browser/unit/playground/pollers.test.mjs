import test from "node:test";
import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { getState, setState } from "../../../../src/astinuslab/web/static/playground/state.js";

let sequence = 0;
async function fixture(t) {
  const listeners = new Set(),
    timers = new Map(),
    requests = [];
  const document = {
    visibilityState: "visible",
    addEventListener: (type, fn) => {
      if (type === "visibilitychange") listeners.add(fn);
    },
    removeEventListener: (_type, fn) => listeners.delete(fn),
  };
  const previousDocument = globalThis.document;
  globalThis.document = document;
  let timerId = 0,
    active = 0,
    maximum = 0;
  const schedule = (fn, delay) => {
    timers.set(++timerId, { fn, delay });
    return timerId;
  };
  t.mock.method(globalThis, "setTimeout", schedule);
  t.mock.method(globalThis, "setInterval", schedule);
  t.mock.method(globalThis, "clearTimeout", (id) => timers.delete(id));
  t.mock.method(globalThis, "clearInterval", (id) => timers.delete(id));
  t.mock.method(globalThis, "fetch", (url, options) => {
    active += 1;
    maximum = Math.max(maximum, active);
    return new Promise((resolve, reject) =>
      requests.push({
        url,
        options,
        resolve: (value, status = 200) => {
          active -= 1;
          resolve({ ok: status < 400, status, text: async () => JSON.stringify(value) });
        },
        reject: (error) => {
          active -= 1;
          reject(error);
        },
      }),
    );
  });
  const poller = await import(
    `../../../../src/astinuslab/web/static/playground/pollers.js?fixture=${sequence++}`
  );
  setState((prev) => ({
    ...prev,
    session: null,
    selectedId: null,
    errors: {},
    signedOut: false,
    cleanupFailure: null,
    cleanup: null,
  }));
  t.after(() => {
    poller.stopSessionPoll();
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  return {
    ...poller,
    requests,
    timers,
    document,
    maximum: () => maximum,
    visibility: () => {
      for (const fn of listeners) fn();
    },
    flush: () => setImmediate(),
    nextTimer: async () => {
      const [id, timer] = [...timers][0];
      timers.delete(id);
      timer.fn();
      await setImmediate();
    },
  };
}
const session = (id, running = false) => ({
  session_id: id,
  vms: running ? [{ id: "vm", state: "running" }] : [],
  networks: [],
});

test("startup and visibility coalesce without overlapping refreshes", async (t) => {
  const f = await fixture(t);
  f.startSessionPoll();
  f.visibility();
  f.visibility();
  assert.equal(f.requests.length, 1);
  f.requests[0].resolve(session("first"));
  await f.flush();
  assert.match(f.requests[1].url, /recent-failure/);
  f.requests[1].resolve(null);
  await f.flush();
  assert.equal(f.requests.length, 3);
  assert.equal(f.maximum(), 1);
});

test("stop fences success and restart waits for transport ignoring abort", async (t) => {
  const f = await fixture(t);
  f.startSessionPoll();
  f.stopSessionPoll();
  const before = getState();
  f.startSessionPoll();
  assert.equal(f.requests.length, 1);
  f.requests[0].resolve(session("old"));
  await f.flush();
  assert.equal(getState(), before);
  assert.match(f.requests[1].url, /current-or-null/);
  f.requests[1].resolve(session("new"));
  await f.flush();
  f.requests[2].resolve(null);
  await f.flush();
  assert.equal(getState().session.id, "new");
  assert.equal(f.maximum(), 1);
});

for (const phase of ["metrics", "failure"])
  test(`stop discards late ${phase} and makes no stale publication`, async (t) => {
    const f = await fixture(t);
    f.startSessionPoll();
    f.requests[0].resolve(session("old", phase === "metrics"));
    await f.flush();
    f.stopSessionPoll();
    const before = getState();
    f.requests[1].resolve(phase === "metrics" ? { vm: { cpu_pct: 88 } } : { session_id: "old" });
    await f.flush();
    assert.equal(getState(), before);
    assert.equal(f.requests.length, 2);
  });

for (const status of [401, 500])
  test(`stale ${status} cannot publish an error after refresh`, async (t) => {
    const f = await fixture(t);
    f.startSessionPoll();
    f.refreshSessionPoll();
    f.requests[0].resolve({ detail: "old error" }, status);
    await f.flush();
    assert.equal(getState().signedOut, false);
    assert.deepEqual(getState().errors, {});
    assert.equal(f.requests.length, 2);
  });

test("hidden page pauses scheduling and unchanged polls keep store identity", async (t) => {
  const f = await fixture(t);
  f.document.visibilityState = "hidden";
  f.startSessionPoll();
  assert.equal(f.requests.length, 0);
  f.document.visibilityState = "visible";
  f.visibility();
  f.requests[0].resolve(session("same"));
  await f.flush();
  f.requests[1].resolve(null);
  await f.flush();
  const previous = getState();
  assert.equal([...f.timers.values()][0].delay, 5000);
  await f.nextTimer();
  f.requests[2].resolve(session("same"));
  await f.flush();
  f.requests[3].resolve(null);
  await f.flush();
  assert.equal(getState(), previous);
  f.document.visibilityState = "hidden";
  f.visibility();
  assert.equal(f.timers.size, 0);
});

test("a current 401 reports signed out, while transport failure remains retryable", async (t) => {
  const f = await fixture(t);
  f.startSessionPoll();
  f.requests[0].resolve({ detail: "expired" }, 401);
  await f.flush();
  assert.equal(getState().signedOut, true);
  assert.equal(getState().errors.session, undefined);
  await f.nextTimer();
  f.requests[1].reject(new Error("offline"));
  await f.flush();
  assert.equal(getState().errors.session, "offline");
  assert.equal(f.timers.size, 1);
});

test("cleanup refresh fences old topology and polls only its retained session ID", async (t) => {
  const f = await fixture(t);
  f.startSessionPoll();
  setState((prev) => ({
    ...prev,
    cleanup: { session_id: "ended", phase: "requesting", status: null },
  }));
  f.refreshSessionPoll();
  f.requests[0].resolve(session("late-running", true));
  await f.flush();
  assert.equal(getState().session, null);
  assert.equal(f.requests.length, 1, "no read before DELETE is accepted");
  setState((prev) => ({
    ...prev,
    cleanup: { ...prev.cleanup, phase: "accepted", status: "destroy_requested" },
  }));
  f.refreshSessionPoll();
  assert.match(f.requests[1].url, /sessions\/ended\/cleanup-status$/);
  f.requests[1].resolve({
    session_id: "ended",
    status: "destroying",
    finished_at: null,
    error_code: null,
  });
  await f.flush();
  assert.equal(getState().cleanup.status, "destroying");
  await f.nextTimer();
  f.requests[2].resolve({ detail: "absent" }, 404);
  await f.flush();
  assert.equal(getState().cleanup.status, "destroying");
  assert.equal(getState().cleanup.unavailable, "missing");
  await f.nextTimer();
  f.requests[3].resolve({
    session_id: "ended",
    status: "cancelled",
    finished_at: "2026-09-06T10:00:00Z",
    error_code: null,
  });
  await f.flush();
  assert.equal(getState().cleanup.status, "cancelled");
  assert.equal(getState().cleanup.unavailable, null);
  assert.equal(f.timers.size, 0, "terminal named cleanup stops polling");
});

test("old cleanup success cannot overwrite a newly selected session", async (t) => {
  const f = await fixture(t);
  setState((prev) => ({ ...prev, cleanup: { session_id: "old", phase: "accepted" } }));
  f.startSessionPoll();
  setState((prev) => ({ ...prev, cleanup: null, session: session("new") }));
  f.refreshSessionPoll();
  f.requests[0].resolve({ session_id: "old", status: "cancelled" });
  await f.flush();
  assert.equal(getState().cleanup, null);
  assert.equal(getState().session.session_id, "new");
});

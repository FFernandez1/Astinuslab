import test from "node:test";
import assert from "node:assert/strict";
import { createPoller } from "../../../../src/astinuslab/web/static/packages/poller.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

// Drain only promise reactions: no sleeps or wall-clock scheduler dependencies.
async function flush() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

function fakeScheduler() {
  const timers = new Map();
  let nextId = 0;
  return {
    timers,
    setTimeout(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    take(delay) {
      assert.equal(timers.size, 1, "exactly one next poll is scheduled");
      const [id, timer] = timers.entries().next().value;
      assert.equal(timer.delay, delay);
      timers.delete(id);
      return timer.callback;
    },
  };
}

function harness(overrides = {}) {
  const scheduler = fakeScheduler();
  const requests = [];
  const successes = [];
  const errors = [];
  const poller = createPoller({
    scheduler,
    request(operationId, { signal }) {
      const pending = deferred();
      requests.push({ operationId, signal, ...pending });
      return pending.promise;
    },
    isTerminal: (value) => value.state === "done",
    onSuccess: (value, operationId) => successes.push({ value, operationId }),
    onError: (error, operationId) => errors.push({ error, operationId }),
    ...overrides,
  });
  return { poller, scheduler, requests, successes, errors };
}

function httpError(status) {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

test("start is immediate and idempotent; pending requests never acquire a timer", async () => {
  const h = harness();
  h.poller.start("prepare-1");
  h.poller.start("prepare-1");
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].operationId, "prepare-1");
  assert.equal(h.requests[0].signal.aborted, false);
  assert.equal(h.scheduler.timers.size, 0);
  h.requests[0].resolve({ state: "running" });
  await flush();
  assert.deepEqual(h.successes, [{ value: { state: "running" }, operationId: "prepare-1" }]);
  h.poller.start("prepare-1");
  assert.equal(h.requests.length, 1);
  h.scheduler.take(2000)();
  await flush();
  assert.equal(h.requests.length, 2);
  assert.equal(h.scheduler.timers.size, 0);
  h.poller.stop();
  h.requests[1].resolve({ state: "running" });
  await flush();
  assert.equal(h.scheduler.timers.size, 0);
});

test("network and 5xx failures keep custody with exact capped backoff; success resets it", async () => {
  const h = harness();
  h.poller.start("operation-1");
  const failures = [
    new TypeError("Failed to fetch"),
    httpError(500),
    httpError(503),
    httpError(502),
    httpError(599),
    httpError(504),
    httpError(500),
  ];
  const delays = [2000, 4000, 8000, 16000, 30000, 30000, 30000];
  for (const [index, failure] of failures.entries()) {
    h.requests[index].reject(failure);
    await flush();
    assert.deepEqual(h.errors[index], { error: failure, operationId: "operation-1" });
    assert.equal(h.successes.length, 0);
    h.poller.start("operation-1");
    assert.equal(h.requests.length, index + 1);
    h.scheduler.take(delays[index])();
    await flush();
  }
  h.requests[7].resolve({ state: "running" });
  await flush();
  h.scheduler.take(2000)();
  await flush();
  h.requests[8].reject(httpError(503));
  await flush();
  h.scheduler.take(2000)();
  await flush();
  assert.equal(h.requests.length, 10);
  h.poller.stop();
  h.requests[9].resolve({ state: "done" });
  await flush();
});

test("terminal success is delivered once and permits an explicit restart", async () => {
  const h = harness();
  h.poller.start("verification-1");
  h.requests[0].resolve({ state: "done" });
  await flush();
  assert.deepEqual(h.successes, [{ value: { state: "done" }, operationId: "verification-1" }]);
  assert.equal(h.scheduler.timers.size, 0);
  h.poller.start("verification-1");
  await flush();
  assert.equal(h.requests.length, 2);
  h.poller.stop();
  h.requests[1].resolve({ state: "done" });
  await flush();
});

for (const status of [400, 401, 403, 404, 409, 429, 499, 600]) {
  test(`HTTP ${status} reports once and stops without retry`, async () => {
    const h = harness();
    const failure = httpError(status);
    h.poller.start("operation-1");
    h.requests[0].reject(failure);
    await flush();
    assert.deepEqual(h.errors, [{ error: failure, operationId: "operation-1" }]);
    assert.equal(h.scheduler.timers.size, 0);
    h.poller.start("operation-1");
    await flush();
    assert.equal(h.requests.length, 2, "a stopped operation can be restarted explicitly");
    h.poller.stop();
    h.requests[1].resolve({ state: "done" });
    await flush();
  });
}

test("stop aborts a pending request and fences even a source that ignores abort", async () => {
  const h = harness();
  h.poller.stop();
  h.poller.start("prepare-1");
  h.poller.stop();
  h.poller.stop();
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].resolve({ state: "done" });
  await flush();
  assert.deepEqual(h.successes, []);
  assert.deepEqual(h.errors, []);
  assert.equal(h.scheduler.timers.size, 0);
});

test("stop consumes a late rejected promise without delivering an error", async () => {
  const h = harness();
  h.poller.start("prepare-1");
  h.poller.stop();
  h.requests[0].reject(httpError(404));
  await flush();
  assert.deepEqual(h.errors, []);
  assert.equal(h.scheduler.timers.size, 0);
});

for (const outcome of ["resolve", "reject"]) {
  test(`replacement waits for old ${outcome}, fences its callbacks, and retains newest ID`, async () => {
    const h = harness();
    h.poller.start("old");
    h.poller.start("middle");
    h.poller.start("new");
    await flush();
    assert.equal(h.requests[0].signal.aborted, true);
    assert.equal(h.requests.length, 1, "abort-ignoring requests must not overlap replacements");
    h.requests[0][outcome](outcome === "resolve" ? { state: "done" } : httpError(404));
    await flush();
    assert.deepEqual(h.successes, []);
    assert.deepEqual(h.errors, []);
    assert.equal(h.requests.length, 2);
    assert.equal(h.requests[1].operationId, "new");
    assert.equal(h.requests[1].signal.aborted, false);
    assert.equal(h.scheduler.timers.size, 0);
    h.requests[1].resolve({ state: "running" });
    await flush();
    assert.deepEqual(h.successes, [{ value: { state: "running" }, operationId: "new" }]);
    h.scheduler.take(2000)();
    await flush();
    assert.equal(h.requests.length, 3);
    h.poller.stop();
    assert.equal(h.requests[2].signal.aborted, true);
    h.requests[2].resolve({ state: "done" });
    await flush();
  });
}

test("stop/start of the same ID changes generation and resets retry history", async () => {
  const h = harness();
  h.poller.start("same");
  h.requests[0].reject(httpError(503));
  await flush();
  h.scheduler.take(2000)();
  h.poller.stop();
  h.poller.start("same");
  await flush();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ state: "done" });
  await flush();
  assert.deepEqual(h.successes, []);
  assert.equal(h.requests.length, 3);
  h.requests[2].reject(httpError(503));
  await flush();
  assert.equal(h.errors.length, 2);
  h.scheduler.take(2000)();
  h.poller.stop();
  h.requests[3].resolve({ state: "done" });
  await flush();
});

test("cancelled and already-fired timer callbacks cannot disturb a replacement timer", async () => {
  const h = harness();
  h.poller.start("old");
  h.requests[0].resolve({ state: "running" });
  await flush();
  const cancelledCallback = h.scheduler.timers.values().next().value.callback;
  h.poller.start("new");
  assert.equal(h.scheduler.timers.size, 0, "replacement clears the old timer");
  h.requests[1].resolve({ state: "running" });
  await flush();
  cancelledCallback();
  assert.equal(h.requests.length, 2);
  const firedCallback = h.scheduler.take(2000);
  firedCallback();
  h.requests[2].resolve({ state: "running" });
  await flush();
  firedCallback();
  assert.equal(h.requests.length, 3, "a duplicate stale timer must not start another request");
  assert.equal(h.scheduler.timers.size, 1);
  h.poller.stop();
  assert.equal(h.scheduler.timers.size, 0);
  cancelledCallback();
  firedCallback();
  assert.equal(h.requests.length, 3);
});

for (const callback of ["onSuccess", "onError"]) {
  for (const action of ["stop", "replace", "restart"]) {
    test(`${callback} may ${action} reentrantly without old cleanup touching new custody`, async () => {
      let first = true;
      const h = harness({
        [callback]() {
          if (!first) return;
          first = false;
          if (action === "stop" || action === "restart") h.poller.stop();
          if (action === "replace") h.poller.start("new");
          if (action === "restart") h.poller.start("old");
        },
      });
      h.poller.start("old");
      if (callback === "onSuccess") h.requests[0].resolve({ state: "done" });
      else h.requests[0].reject(httpError(404));
      await flush();
      assert.equal(h.scheduler.timers.size, 0);
      if (action === "stop") {
        assert.equal(h.requests.length, 1);
        return;
      }
      assert.equal(h.requests.length, 2);
      assert.equal(h.requests[1].operationId, action === "replace" ? "new" : "old");
      assert.equal(h.requests[1].signal.aborted, false);
      h.poller.start(action === "replace" ? "new" : "old");
      assert.equal(h.requests.length, 2);
      h.requests[1].resolve({ state: "running" });
      await flush();
      h.scheduler.take(2000)();
      h.poller.stop();
      assert.equal(h.requests[2].signal.aborted, true);
      h.requests[2].resolve({ state: "done" });
      await flush();
    });
  }
}

test("a reentrant terminal predicate cannot deliver old success or stop its replacement", async () => {
  const h = harness({
    isTerminal() {
      h.poller.start("new");
      return true;
    },
  });
  h.poller.start("old");
  h.requests[0].resolve({ state: "done" });
  await flush();
  assert.deepEqual(h.successes, []);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].operationId, "new");
  h.poller.stop();
  h.requests[1].resolve({ state: "done" });
  await flush();
});

test("async success callbacks delay scheduling and stale rejection cannot stop replacement", async () => {
  const callback = deferred();
  const h = harness({ onSuccess: () => callback.promise });
  h.poller.start("old");
  h.requests[0].resolve({ state: "running" });
  await flush();
  assert.equal(h.scheduler.timers.size, 0);
  h.poller.start("new");
  assert.equal(h.requests.length, 1);
  callback.reject(new Error("old rendering failed"));
  await flush();
  assert.deepEqual(h.errors, []);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].signal.aborted, false);
  h.poller.stop();
  h.requests[1].reject(httpError(404));
  await flush();
});

for (const callback of ["onSuccess", "isTerminal"]) {
  test(`${callback} exception is reported and stops instead of becoming a network retry`, async () => {
    const failure = new Error("rendering failed");
    const h = harness({
      [callback]() {
        throw failure;
      },
    });
    h.poller.start("operation-1");
    h.requests[0].resolve({ state: "running" });
    await flush();
    assert.deepEqual(h.errors, [{ error: failure, operationId: "operation-1" }]);
    assert.equal(h.scheduler.timers.size, 0);
  });
}

for (const asynchronous of [false, true]) {
  test(`error callback ${asynchronous ? "rejection" : "exception"} is consumed and stops polling`, async () => {
    const h = harness({
      onError() {
        const failure = new Error("error display failed");
        if (asynchronous) return Promise.reject(failure);
        throw failure;
      },
    });
    h.poller.start("operation-1");
    h.requests[0].reject(httpError(503));
    await flush();
    assert.equal(h.scheduler.timers.size, 0);
    h.poller.start("operation-1");
    await flush();
    assert.equal(h.requests.length, 2);
    h.poller.stop();
    h.requests[1].resolve({ state: "done" });
    await flush();
  });
}

test("synchronous request throws are reported and retried without unhandled rejection", async () => {
  const failure = new TypeError("network unavailable");
  const h = harness({
    request() {
      throw failure;
    },
  });
  h.poller.start("operation-1");
  await flush();
  assert.deepEqual(h.errors, [{ error: failure, operationId: "operation-1" }]);
  h.scheduler.take(2000)();
  await flush();
  assert.equal(h.errors.length, 2);
  h.scheduler.take(4000);
  h.poller.stop();
});

test("an unsolicited current AbortError is reported and stops", async () => {
  const h = harness();
  const failure = Object.assign(new Error("aborted upstream"), { name: "AbortError" });
  h.poller.start("operation-1");
  h.requests[0].reject(failure);
  await flush();
  assert.deepEqual(h.errors, [{ error: failure, operationId: "operation-1" }]);
  assert.equal(h.scheduler.timers.size, 0);
});

test("empty or non-string identifiers cannot replace a valid owner", async () => {
  const h = harness();
  h.poller.start("valid");
  for (const invalid of [undefined, null, "", 0, {}]) {
    assert.throws(() => h.poller.start(invalid), TypeError);
  }
  assert.equal(h.requests[0].signal.aborted, false);
  assert.equal(h.requests.length, 1);
  h.poller.stop();
  h.requests[0].resolve({ state: "done" });
  await flush();
});

for (const trigger of ["stop", "replace"]) {
  for (const action of ["stop", "start"]) {
    test(`a synchronous abort listener may ${action} during ${trigger} without losing custody`, async () => {
      const h = harness({
        request(operationId, { signal }) {
          const pending = deferred();
          h.requests.push({ operationId, signal, ...pending });
          if (operationId === "old") {
            signal.addEventListener("abort", () => {
              if (action === "start") h.poller.start("listener");
              else h.poller.stop();
            });
          }
          return pending.promise;
        },
      });
      h.poller.start("old");
      if (trigger === "replace") h.poller.start("outer");
      else h.poller.stop();
      assert.equal(h.requests[0].signal.aborted, true);
      assert.equal(h.requests.length, 1, "the old request must settle before any restart");
      h.requests[0].resolve({ state: "done" });
      await flush();
      assert.deepEqual(h.successes, []);
      assert.deepEqual(h.errors, []);
      assert.equal(h.scheduler.timers.size, 0);
      if (action === "stop") {
        assert.equal(h.requests.length, 1, "the listener's stop must supersede outer replacement");
        return;
      }
      assert.equal(h.requests.length, 2);
      assert.equal(h.requests[1].operationId, "listener");
      h.requests[1].resolve({ state: "running" });
      await flush();
      assert.deepEqual(h.successes, [{ value: { state: "running" }, operationId: "listener" }]);
      h.scheduler.take(2000)();
      h.poller.stop();
      h.requests[2].resolve({ state: "done" });
      await flush();
    });
  }
}

for (const strictTimer of ["setTimeout", "clearTimeout"]) {
  test(`default scheduler preserves the native ${strictTimer} receiver`, async () => {
    const originalSetTimeout = globalThis.setTimeout;
    const originalClearTimeout = globalThis.clearTimeout;
    const scheduler = fakeScheduler();
    function checkReceiver(name, receiver) {
      // Browser timer functions accept their global receiver (or a detached
      // invocation), but reject being called as methods of an unrelated object.
      if (name === strictTimer && receiver !== globalThis && receiver != null) {
        throw new TypeError(`Illegal invocation: ${name}`);
      }
    }
    globalThis.setTimeout = function (callback, delay) {
      checkReceiver("setTimeout", this);
      return scheduler.setTimeout(callback, delay);
    };
    globalThis.clearTimeout = function (handle) {
      checkReceiver("clearTimeout", this);
      scheduler.clearTimeout(handle);
    };
    try {
      const h = harness({ scheduler: undefined });
      h.poller.start("native-timer-operation");
      h.requests[0].resolve({ state: "running" });
      await flush();
      scheduler.take(2000)();
      assert.equal(h.requests.length, 2, "the default scheduler must deliver the next poll");
      h.requests[1].resolve({ state: "running" });
      await flush();
      assert.equal(scheduler.timers.size, 1);
      assert.doesNotThrow(() => h.poller.stop());
      assert.equal(scheduler.timers.size, 0, "stop must cancel the native timer");
      assert.equal(h.successes.length, 2);
      assert.deepEqual(h.errors, []);
    } finally {
      globalThis.setTimeout = originalSetTimeout;
      globalThis.clearTimeout = originalClearTimeout;
    }
  });
}

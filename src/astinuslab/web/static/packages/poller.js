// One poller owns one UI slot. Reuse it and call start(newId) to replace custody;
// stop/start also restarts the same ID. Repeated start for the current ID is inert.
// request(id, { signal }) matches the package status API. Callbacks receive the
// captured ID; perform ownership-sensitive UI updates synchronously (async
// callback internals must guard their own later side effects). isTerminal is a
// synchronous predicate. Success/error callbacks may return promises.
export function createPoller({
  request,
  isTerminal,
  onSuccess,
  onError,
  scheduler = {
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
  },
}) {
  let generation = 0;
  let current = null;
  let inFlight = null;

  function owns(session) {
    return current === session && session.generation === generation;
  }

  function clearTimer(session) {
    if (session?.timer) {
      scheduler.clearTimeout(session.timer.handle);
      session.timer = null;
    }
  }

  function abortPending() {
    if (inFlight?.pending) inFlight.controller.abort();
  }

  function stop() {
    const previous = current;
    current = null;
    generation += 1;
    clearTimer(previous);
    // Fence first: abort listeners can synchronously call start/stop again.
    abortPending();
  }

  function stopOwned(session) {
    if (owns(session)) stop();
  }

  function schedule(session, delay) {
    const timer = { handle: null };
    session.timer = timer;
    timer.handle = scheduler.setTimeout(() => {
      if (!owns(session) || session.timer !== timer) return;
      session.timer = null;
      run(session);
    }, delay);
  }

  async function report(session, error) {
    if (!owns(session)) return;
    try {
      await onError(error, session.operationId);
    } catch (_) {
      // Error display failures must not become unhandled rejections or retries.
      stopOwned(session);
    }
  }

  async function execute(flight) {
    const { session, controller } = flight;
    let delay = null;
    try {
      let value;
      try {
        value = await request(session.operationId, { signal: controller.signal });
      } catch (error) {
        flight.pending = false;
        if (!owns(session)) return;
        const retryable =
          error?.name !== "AbortError" &&
          (error?.status == null || (error.status >= 500 && error.status <= 599));
        await report(session, error);
        if (!owns(session)) return;
        if (retryable) {
          delay = Math.min(2000 * 2 ** session.failures, 30000);
          session.failures = Math.min(session.failures + 1, 4);
        } else {
          stopOwned(session);
        }
        return;
      }
      flight.pending = false;
      if (!owns(session)) return;
      session.failures = 0;
      try {
        const terminal = isTerminal(value);
        if (!owns(session)) return;
        await onSuccess(value, session.operationId);
        if (!owns(session)) return;
        if (terminal) stopOwned(session);
        else delay = 2000;
      } catch (error) {
        // Predicate/rendering failures are not transient transport failures.
        await report(session, error);
        stopOwned(session);
      }
    } finally {
      // Keep the physical request lock even after stop/abort until the source
      // and callbacks settle. An abort-ignoring source cannot overlap a restart.
      if (inFlight === flight) inFlight = null;
      if (owns(session)) {
        if (delay !== null) schedule(session, delay);
      } else if (current) {
        run(current);
      }
    }
  }

  function run(session) {
    if (!owns(session) || inFlight || session.timer) return;
    const flight = { session, controller: new AbortController(), pending: true };
    inFlight = flight;
    void execute(flight);
  }

  function start(operationId) {
    if (typeof operationId !== "string" || operationId.length === 0) {
      throw new TypeError("Polling requires a nonempty operation identifier");
    }
    if (current?.operationId === operationId) return;
    const previous = current;
    const session = { operationId, generation: ++generation, failures: 0, timer: null };
    current = session;
    clearTimer(previous);
    abortPending();
    run(session);
  }

  return { start, stop };
}

// Run lifecycle: enqueue a plugin run, poll it, and drive the running/scaffold
// UI states. Also loads cached results and renders the failure state.
import { t, $, el } from "./utils.js";
import {
  state,
  invalidateContext,
  captureContext,
  isCurrentContext,
  stopTimers,
  saveActiveRun,
  clearActiveRun,
} from "./state.js";
import { getJSON, postJSON } from "./api.js";
import { isYaraPlugin, symbolsAvailable, renderCmd, updateRunButton } from "./catalog.js";
import { renderResult, renderGate, clearGate, clearResult } from "./results.js";
import { refreshQueue } from "./queue.js";
// Same import cycle results.js already relies on: the tab view lives with the
// page wiring, and both result states need to apply it.
import { applyTab } from "./index.js";

const POLL_MS = 900;
const MAX_POLL_FAILURES = 5;

export function setRunningUI(on) {
  state.running = on;
  var wrap = $("tablewrap");
  if (on) {
    if (wrap) {
      wrap.classList.add("vol-running");
      if (!wrap.querySelector(".vol-scanline")) wrap.appendChild(el("div", "vol-scanline"));
    }
  } else if (wrap) {
    wrap.classList.remove("vol-running");
    var s = wrap.querySelector(".vol-scanline");
    if (s) s.remove();
  }
  updateRunButton();
}

export function showScaffold() {
  $("results-empty").hidden = true;
  var notrun = $("results-notrun");
  if (notrun) notrun.hidden = true;
  $("results-scaffold").hidden = false;
}

export function showRunPrompt() {
  invalidateContext();
  clearResult();
  setRunningUI(false);
  clearGate();
  $("results-scaffold").hidden = true;
  $("results-empty").hidden = true;
  var notrun = $("results-notrun");
  if (notrun) {
    notrun.hidden = false;
    notrun.textContent = t("vol_not_run_yet");
  }
  if ($("check-run-status")) $("check-run-status").hidden = true;
  $("task-pill").className = "vol-pill vol-pill--queued";
  $("task-state").textContent = t("vol_state_idle");
  $("task-id").textContent = "—";
  $("task-elapsed").textContent = "—";
  $("findings").innerHTML = "";
}

export function loadCachedResult() {
  var jobId = state.dump.job_id;
  var plugin = state.plugin;
  showRunPrompt();
  var context = captureContext();
  state.observing = true;
  updateRunButton();
  getJSON(
    "/api/v1/volatility/runs/latest?job_id=" +
      encodeURIComponent(jobId) +
      "&plugin=" +
      encodeURIComponent(plugin),
  )
    .then(function (res) {
      // Drop the response if the user changed dump/plugin while it was in flight.
      if (!isCurrentContext(context)) return;
      observeResponse(res, context);
    })
    .catch(function (error) {
      if (isCurrentContext(context)) statusUnavailable(context, error);
    });
}

export function runPlugin() {
  if (!state.plugin || !state.dump || state.observing || state.running) return;
  if (!symbolsAvailable()) {
    renderGate();
    return;
  }
  invalidateContext();
  clearResult();
  var context = captureContext();
  showScaffold();
  renderCmd();

  $("task-pill").className = "vol-pill vol-pill--run";
  $("task-state").textContent = t("vol_state_running");
  $("task-id").textContent = t("vol_state_queued");
  $("task-elapsed").textContent = "0.0s";
  $("findings").innerHTML = "";
  setRunningUI(true);

  var t0 = Date.now();
  state.tickTimer = setInterval(function () {
    $("task-elapsed").textContent = ((Date.now() - t0) / 1000).toFixed(1) + "s";
  }, 100);

  postJSON("/api/v1/volatility/runs", {
    job_id: context.jobId,
    plugin: context.plugin,
    yara_rules: isYaraPlugin(context.plugin) ? $("yara-rules").value : undefined,
  })
    .then(function (run) {
      if (!isCurrentContext(context)) return;
      if (typeof run.run_id !== "string" || !run.run_id) {
        failUI(t("vol_identity_mismatch"));
        return;
      }
      state.runId = run.run_id;
      state.observing = true;
      saveActiveRun();
      state.pollFailures = 0;
      $("task-id").textContent = "run:" + run.run_id;
      pollRun(Object.freeze({ ...context, runId: run.run_id }));
    })
    .catch(function (err) {
      if (!isCurrentContext(context)) return;
      failUI(err.detail || t("vol_enqueue_failed") + " (" + (err.status || "?") + ")");
    });
}

function noRetainedResult() {
  showRunPrompt();
  $("results-notrun").textContent = t("vol_no_retained_result");
}

function observeResponse(res, context) {
  if (!isCurrentContext(context)) return;
  if (res && ["none", "deleted"].includes(res.status)) {
    noRetainedResult();
    return;
  }
  if (
    !res?.run_id ||
    res.job_id !== context.jobId ||
    res.plugin !== context.plugin ||
    (context.runId && res.run_id !== context.runId)
  ) {
    failUI(t("vol_identity_mismatch"));
    return;
  }
  state.runId = res.run_id;
  const owned = Object.freeze({ ...context, runId: res.run_id });
  $("task-id").textContent = "run:" + res.run_id;
  if (res.status === "completed") {
    state.observing = false;
    state.reconnect = null;
    clearActiveRun();
    if ($("check-run-status")) $("check-run-status").hidden = true;
    renderResult(res, owned);
  } else if (res.status === "failed") {
    failUI(res.error || t("vol_run_failed"));
  } else if (["queued", "running"].includes(res.status)) {
    state.observing = true;
    state.reconnect = null;
    saveActiveRun();
    showScaffold();
    setRunningUI(true);
    $("task-pill").className = "vol-pill vol-pill--run";
    $("task-state").textContent = t(
      res.status === "queued" ? "vol_state_queued" : "vol_state_running",
    );
    if ($("check-run-status")) $("check-run-status").hidden = true;
    state.pollTimer = setTimeout(() => pollRun(owned), POLL_MS);
  } else {
    statusUnavailable(owned);
  }
}

function statusUnavailable(context, error) {
  if (!isCurrentContext(context)) return;
  stopTimers();
  clearResult();
  state.observing = true;
  setRunningUI(false);
  showScaffold();
  $("task-pill").className = "vol-pill vol-pill--queued";
  $("task-state").textContent = t("vol_status_unavailable");
  $("task-id").textContent = context.runId ? "run:" + context.runId : context.jobId;
  $("view-raw").textContent =
    t([401, 403].includes(error?.status) ? "vol_status_auth" : "vol_status_unavailable_detail") +
    "\n" +
    [context.jobId, context.runId, context.plugin].filter(Boolean).join(" · ");
  state.tab = "raw";
  applyTab();
  state.reconnect = () => {
    if (!isCurrentContext(context)) return;
    state.reconnect = null;
    $("check-run-status").hidden = true;
    if (!context.runId) {
      loadCachedResult();
      return;
    }
    state.pollFailures = 0;
    setRunningUI(true);
    pollRun(context);
  };
  $("check-run-status").hidden = false;
}

export function restoreActiveRun(saved) {
  // The controller has already matched the exact input and fenced user intent.
  state.plugin = saved.plugin;
  state.runId = saved.run_id;
  state.observing = true;
  showScaffold();
  renderCmd();
  setRunningUI(true);
  $("task-id").textContent = "run:" + saved.run_id;
  const context = Object.freeze({ ...captureContext(), runId: saved.run_id });
  pollRun(context);
}

function pollRun(context) {
  if (!isCurrentContext(context)) return;
  getJSON(
    "/api/v1/volatility/runs/" +
      encodeURIComponent(context.jobId) +
      "/" +
      encodeURIComponent(context.runId),
  )
    .then(function (res) {
      if (!isCurrentContext(context) || context.runId !== state.runId) return;
      state.pollFailures = 0;
      observeResponse(res, context);
    })
    .catch(function (error) {
      if (!isCurrentContext(context) || context.runId !== state.runId) return;
      if (error.status === 404) {
        noRetainedResult();
        return;
      }
      state.pollFailures += 1;
      if ([401, 403].includes(error.status) || state.pollFailures >= MAX_POLL_FAILURES) {
        statusUnavailable(context, error);
        return;
      }
      state.pollTimer = setTimeout(function () {
        pollRun(context);
      }, POLL_MS);
    });
}

export function failUI(message) {
  invalidateContext();
  clearResult();
  setRunningUI(false);
  if ($("check-run-status")) $("check-run-status").hidden = true;
  showScaffold();
  $("task-pill").className = "vol-pill vol-pill--failed";
  $("task-state").textContent = t("vol_state_failed");
  $("res-head").innerHTML = "";
  $("res-body").innerHTML = "";
  $("view-raw").textContent = String(message);
  $("foot-summary").textContent = t("vol_run_failed");
  // The reason is the only thing on this screen the operator can act on, and
  // the raw console is where it is written -- so show it. Left on the table
  // tab it was a hidden element behind an unmarked click, and the page said
  // only "failed". The tab choice is not preserved across a failure: a failed
  // run has no table to return to, and one click restores it.
  state.tab = "raw";
  applyTab();
  var evf = $("evidence-link");
  if (evf) {
    evf.hidden = true;
    evf.removeAttribute("href");
  }
  $("findings").innerHTML = "";
  refreshQueue();
}

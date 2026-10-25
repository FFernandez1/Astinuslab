/* AstinusLab — Volatility 3 Analysis Workbench.
   Fetches real backend data (dumps, plugin catalog, analysis queue) and runs
   plugins on demand: enqueue → poll → render Table / Raw console results.
   Entry module: wires DOM events and boots the page. */
import { t, $, el } from "./utils.js";
import {
  state,
  captureContext,
  isCurrentContext,
  readActiveRun,
  clearActiveRun,
  stopTimers,
} from "./state.js";
import { getJSON, deleteJSON, postJSON } from "./api.js";
import {
  buildDumpSelect,
  dumpByJobId,
  renderContext,
  renderCmd,
  renderCatalog,
  updateRunButton,
  loadCatalog,
  selectPlugin,
} from "./catalog.js";
import { runPlugin, showRunPrompt, restoreActiveRun } from "./run.js";
import { downloadCsv, clearGate } from "./results.js";
import { refreshQueue } from "./queue.js";
import { wireIsfBuildForms, wireIsfUploadButtons } from "./isf.js";

/* ── Result view tabs (Table / Raw) ─────────────────────────────────── */
export function applyTab() {
  var tableBtn = $("tab-table-btn"),
    rawBtn = $("tab-raw-btn");
  if (!tableBtn) return;
  var vt = $("view-table"),
    vr = $("view-raw");
  if (state.tab === "raw") {
    tableBtn.classList.remove("is-active");
    rawBtn.classList.add("is-active");
    vt.hidden = true;
    vr.hidden = false;
  } else {
    rawBtn.classList.remove("is-active");
    tableBtn.classList.add("is-active");
    vr.hidden = true;
    vt.hidden = false;
  }
}

/* ── Page-level section tabs (Workbench / Activity) ─────────────────── */
var PAGE_TABS = [
  { btn: "ptab-workbench-btn", panel: "ptab-workbench", name: "workbench" },
  { btn: "ptab-activity-btn", panel: "ptab-activity", name: "activity" },
];

export function activateTab(name) {
  PAGE_TABS.forEach(function (t) {
    var btn = $(t.btn),
      panel = $(t.panel);
    if (!btn || !panel) return;
    var on = t.name === name;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
    btn.tabIndex = on ? 0 : -1;
    panel.hidden = !on;
  });
}

function wirePageTabs() {
  var tabs = PAGE_TABS.filter(function (t) {
    return $(t.btn) && $(t.panel);
  });
  if (!tabs.length) return;
  tabs.forEach(function (t, i) {
    var btn = $(t.btn);
    btn.addEventListener("click", function () {
      activateTab(t.name);
    });
    btn.addEventListener("keydown", function (e) {
      var dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!dir) return;
      e.preventDefault();
      var next = tabs[(i + dir + tabs.length) % tabs.length];
      activateTab(next.name);
      $(next.btn).focus();
    });
  });
}

/* ── Dump change ────────────────────────────────────────────────────── */
function onDumpChange() {
  var d = dumpByJobId($("dump-select").value);
  if (!d) return;
  $("analysis-context-form").reset();
  $("context-status").textContent = "";
  showRunPrompt();
  state.dump = d;
  state.plugin = null;
  state.runId = null;
  state.filter = "";
  var search = $("plugin-search");
  if (search) search.value = "";
  clearGate();
  $("results-scaffold").hidden = true;
  $("results-empty").hidden = false;
  var notrun = $("results-notrun");
  if (notrun) notrun.hidden = true;
  $("task-pill").className = "vol-pill vol-pill--queued";
  $("task-state").textContent = t("vol_state_idle");
  $("task-id").textContent = "—";
  $("task-elapsed").textContent = "—";
  $("findings").innerHTML = "";
  var yaraInput = $("yara-input");
  if (yaraInput) yaraInput.hidden = true;
  renderContext();
  renderCmd();
  updateRunButton();
  loadCatalog(d.os_family);
}

function copyCmd() {
  var text = $("cmd").textContent.replace(/^\$\s*/, "");
  var btn = $("copy-cmd");
  var done = function (ok) {
    btn.textContent = ok ? t("vol_copied") : t("vol_copy");
    btn.classList.toggle("copied", ok);
    setTimeout(function () {
      btn.textContent = t("vol_copy");
      btn.classList.remove("copied");
    }, 1400);
  };
  if (navigator.clipboard)
    navigator.clipboard
      .writeText(text)
      .then(function () {
        done(true);
      })
      .catch(function () {
        done(false);
      });
  else done(false);
}

/* ── Init ───────────────────────────────────────────────────────────── */
function wireAnalysisContext() {
  var form = $("analysis-context-form");
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var context = captureContext();
    var button = $("context-submit");
    var status = $("context-status");
    button.disabled = true;
    status.textContent = "";
    postJSON("/api/v1/volatility/analysis-contexts", {
      artifact_id: context.artifactId,
      kernel_release: $("context-kernel").value.trim(),
      architecture: $("context-architecture").value.trim(),
      verified: $("context-verified").checked,
      provenance: $("context-note").value.trim(),
    })
      .then(function (dump) {
        if (!isCurrentContext(context) || dump.artifact_id !== context.artifactId) return;
        var plugin = state.plugin;
        state.dumps = state.dumps.map(function (row) {
          return row.artifact_id === dump.artifact_id ? dump : row;
        });
        showRunPrompt();
        state.dump = dump;
        buildDumpSelect();
        renderContext();
        renderCmd();
        if (plugin) selectPlugin(plugin);
        updateRunButton();
      })
      .catch(function (error) {
        if (isCurrentContext(context)) status.textContent = error.detail || t("vol_enqueue_failed");
      })
      .finally(function () {
        button.disabled = false;
      });
  });
}

function init() {
  $("check-run-status")?.addEventListener("click", () => state.reconnect?.());
  window.addEventListener("pagehide", () => {
    stopTimers();
    state.generation += 1;
  });
  var root = $("vol-workbench");
  if (!root || root.dataset.workbenchInitialized) return;
  root.dataset.workbenchInitialized = "1";

  $("dump-select").addEventListener("change", onDumpChange);
  $("plugin-search").addEventListener("input", function (e) {
    state.filter = e.target.value.trim().toLowerCase();
    renderCatalog();
  });
  $("plugin-search").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      var first = document.querySelector(".vol-plugin[data-plugin]");
      if (first) selectPlugin(first.getAttribute("data-plugin"));
    }
  });
  var runBtn = $("run-btn");
  if (runBtn)
    runBtn.addEventListener("click", function () {
      if (state.plugin) runPlugin();
    });
  var rerunBtn = $("rerun-cmd");
  if (rerunBtn)
    rerunBtn.addEventListener("click", function () {
      if (state.plugin) runPlugin();
    });
  var yaraRules = $("yara-rules");
  if (yaraRules)
    yaraRules.addEventListener("input", function () {
      if (state.plugin) showRunPrompt();
      updateRunButton();
    });
  var copyBtn = $("copy-cmd");
  if (copyBtn) copyBtn.addEventListener("click", copyCmd);
  wireAnalysisContext();
  wireIsfBuildForms();
  wireIsfUploadButtons(async function () {
    var context = captureContext();
    var data = await getJSON("/api/v1/volatility/dumps");
    if (!isCurrentContext(context)) return;
    var selected = state.dump?.selection_id || state.dump?.job_id;
    state.dumps = data.dumps || [];
    state.dump = dumpByJobId(selected);
    buildDumpSelect();
    renderContext();
    renderCmd();
    if (state.plugin) selectPlugin(state.plugin);
    updateRunButton();
  });
  wirePageTabs();
  var csvBtn = $("export-csv");
  if (csvBtn) csvBtn.addEventListener("click", downloadCsv);
  var tBtn = $("tab-table-btn"),
    rBtn = $("tab-raw-btn");
  if (tBtn)
    tBtn.addEventListener("click", function () {
      state.tab = "table";
      applyTab();
    });
  if (rBtn)
    rBtn.addEventListener("click", function () {
      state.tab = "raw";
      applyTab();
    });

  // Symbol status badge jumps to the Symbol-tables · ISF panel (its role=button affordance).
  var symBadge = $("m-sym");
  if (symBadge) {
    var gotoSymbols = function () {
      activateTab("activity");
      $("ptab-activity-btn")?.focus();
      var panel = $("isf-list");
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    symBadge.addEventListener("click", gotoSymbols);
    symBadge.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        gotoSymbols();
      }
    });
  }

  // Destructive controls on this page act on a list that stays on screen, so a
  // refusal has nowhere to render itself. The app-wide toast is that surface;
  // the server's own detail is the message whenever it sent one.
  function fail(err, key, fallback) {
    var say = window.showToast;
    if (say) say((err && err.detail) || t(key, fallback), "error");
  }

  $("queue-body").addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".vol-queue-del");
    if (!btn) return;
    var jobId = btn.getAttribute("data-job");
    var runId = btn.getAttribute("data-run");
    if (!jobId || !runId) return;
    btn.disabled = true;
    deleteJSON(
      "/api/v1/volatility/runs/" + encodeURIComponent(jobId) + "/" + encodeURIComponent(runId),
    )
      .then(refreshQueue)
      .catch(function (err) {
        // The row is still on screen because it was not deleted. Re-arming the
        // control without saying why turns a refusal the server explained into
        // a button the operator presses again.
        fail(err, "vol_queue_delete_failed", "Could not delete that run record.");
        btn.disabled = false;
      });
  });
  var clearBtn = $("vol-queue-clear");
  if (clearBtn)
    clearBtn.addEventListener("click", function () {
      if (clearBtn.disabled) return;
      window
        .confirmDialog(t("vol_queue_clear_confirm"))
        .then(function (ok) {
          if (!ok) {
            return;
          }
          clearBtn.disabled = true;
          return deleteJSON("/api/v1/volatility/queue")
            .then(function () {
              return refreshQueue();
            })
            .catch(function (err) {
              // Worst of the two to swallow: the operator answered a
              // confirmation dialog, so silence reads as "done" while the log
              // is exactly as it was.
              fail(err, "vol_queue_clear_failed", "Could not clear the analysis history.");
            })
            .then(function () {
              clearBtn.disabled = false;
            });
        })
        .catch(function (err) {
          fail(err, "vol_queue_clear_failed", "Could not clear the analysis history.");
          clearBtn.disabled = false;
        });
    });

  refreshQueue();

  const saved = readActiveRun();
  const bootGeneration = state.generation;
  getJSON("/api/v1/volatility/dumps")
    .then(function (data) {
      state.dumps = data.dumps || [];
      var requestedJob = new URLSearchParams(window.location.search).get("job");
      const savedDump =
        saved &&
        state.dumps.find(
          (d) =>
            d.job_id === saved.job_id &&
            (d.selection_id || d.job_id) === saved.selection_id &&
            (d.artifact_id || null) === saved.artifact_id,
        );
      const restore =
        savedDump &&
        (!requestedJob || requestedJob === saved.job_id || requestedJob === saved.selection_id);
      if (saved && !restore) clearActiveRun();
      if (state.dumps.length) {
        state.dump =
          (restore && savedDump) || (requestedJob && dumpByJobId(requestedJob)) || state.dumps[0];
      }
      buildDumpSelect();
      if (state.dump) {
        renderContext();
        renderCmd();
        updateRunButton();
        loadCatalog(state.dump.os_family).then(() => {
          if (!restore || state.generation !== bootGeneration || state.dump !== savedDump) return;
          restoreActiveRun(saved);
          renderCatalog();
          const yaraInput = $("yara-input");
          if (yaraInput) yaraInput.hidden = !saved.plugin.toLowerCase().includes("yara");
          if ($("yara-rules")) $("yara-rules").value = "";
        });
      } else {
        renderContext();
      }
    })
    .catch(function () {
      var sel = $("dump-select");
      sel.innerHTML = "";
      sel.appendChild(el("option", null, t("vol_dumps_load_failed")));
      sel.disabled = true;
    });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();

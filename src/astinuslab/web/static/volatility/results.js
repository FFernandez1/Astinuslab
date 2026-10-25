// Result rendering: table + raw console views, findings chips, the symbol gate,
// and CSV export.
import { t, fmt, $, el, escapeHtml } from "./utils.js";
import { state, stopTimers, invalidateContext, isCurrentContext } from "./state.js";
import { setRunningUI, showScaffold, failUI } from "./run.js";
import { updateRunButton } from "./catalog.js";
import { refreshQueue } from "./queue.js";
import { applyTab, activateTab } from "./index.js";

export function clearResult() {
  state.lastResult = null;
  $("export-csv").disabled = true;
  for (const id of ["res-head", "res-body", "view-raw", "tab-rows", "foot-summary", "findings"]) {
    $(id).textContent = "";
  }
  const evidence = $("evidence-link");
  if (evidence) {
    evidence.hidden = true;
    evidence.removeAttribute("href");
  }
}

export function renderResult(res, context) {
  if (!isCurrentContext(context)) return;
  if (
    !res.run_id ||
    (context.runId && res.run_id !== context.runId) ||
    (res.job_id && res.job_id !== context.jobId) ||
    (context.artifactId && res.source_artifact_id !== context.artifactId) ||
    (res.plugin && res.plugin !== context.plugin)
  ) {
    failUI(t("vol_identity_mismatch"));
    return;
  }
  context = Object.freeze({ ...context, runId: res.run_id });
  res = Object.freeze({ ...res, plugin: context.plugin, context });
  stopTimers();
  setRunningUI(false);
  state.lastResult = res;
  $("export-csv").disabled = false;
  showScaffold();

  var cols = res.columns || [];
  var rows = res.rows || [];
  var flags = res.flags || []; // optional per-row severity (crit/warn/null)

  var head = $("res-head");
  var htr = el("tr");
  cols.forEach(function (c) {
    htr.appendChild(el("th", null, c));
  });
  head.innerHTML = "";
  head.appendChild(htr);

  var tb = $("res-body");
  tb.innerHTML = "";
  rows.forEach(function (r, ri) {
    var f = flags[ri] || null;
    var tr = el("tr");
    if (f) tr.className = "flag-" + f;
    for (var i = 0; i < cols.length; i++) {
      var td = el("td");
      var val = r[i] != null ? r[i] : "";
      if (i === 0) {
        td.appendChild(el("span", "hl", String(val)));
      } else {
        td.textContent = String(val);
      }
      tr.appendChild(td);
    }
    tb.appendChild(tr);
  });

  $("tab-rows").textContent = rows.length;
  $("foot-summary").textContent = res.summary || rows.length + " rows";
  $("view-raw").textContent = buildRaw(res);

  $("task-pill").className = "vol-pill vol-pill--done";
  $("task-state").textContent = res.cached ? t("vol_state_cached") : t("vol_state_completed");
  $("task-id").textContent = "run:" + (res.run_id || "—");
  $("task-elapsed").textContent = res.elapsed || "—";
  renderFindings(res.findings, rows.length);
  if (res.isf_low_trust) {
    var lt = el("span", "vol-fchip warn");
    lt.innerHTML =
      '<span class="d"></span>symbols: ' +
      escapeHtml(res.isf_source || "unknown") +
      " (" +
      t("vol_low_trust") +
      ")";
    $("findings").appendChild(lt);
  }

  var ev = $("evidence-link");
  if (ev) {
    if (res.artifact_id) {
      ev.href = "/api/v1/artifacts/" + encodeURIComponent(res.artifact_id) + "/download";
      ev.hidden = false;
    } else {
      ev.hidden = true;
      ev.removeAttribute("href");
    }
  }

  applyTab();
  refreshQueue();
}

function renderFindings(findings, rowCount) {
  var fc = $("findings");
  fc.innerHTML = "";
  var crit = findings && findings.crit ? findings.crit : 0;
  var warn = findings && findings.warn ? findings.warn : 0;
  if (crit) {
    var a = el("span", "vol-fchip crit");
    a.innerHTML = '<span class="d"></span>' + fmt(t("vol_finding_crit"), { n: crit });
    fc.appendChild(a);
  }
  if (warn) {
    var b = el("span", "vol-fchip warn");
    b.innerHTML = '<span class="d"></span>' + fmt(t("vol_finding_warn"), { n: warn });
    fc.appendChild(b);
  }
  if (!crit && !warn && rowCount) {
    var c = el("span", "vol-fchip clean");
    c.innerHTML = '<span class="d"></span>' + t("vol_finding_clean");
    fc.appendChild(c);
  }
}

function buildRaw(res) {
  var cols = res.columns || [];
  var rows = res.rows || [];
  var widths = cols.map(function (c, i) {
    var w = String(c).length;
    rows.forEach(function (r) {
      var len = String(r[i] == null ? "" : r[i]).length;
      if (len > w) w = len;
    });
    return Math.min(w, 40);
  });
  var pad = function (s, w) {
    s = String(s == null ? "" : s);
    if (s.length > w) s = s.slice(0, w);
    while (s.length < w) s += " ";
    return s;
  };
  var out = [];
  out.push("$ vol3 -r json -f " + (res.context?.file || "<dump>") + " " + (res.plugin || ""));
  out.push("");
  out.push(
    cols
      .map(function (c, i) {
        return pad(c, widths[i]);
      })
      .join("  "),
  );
  rows.forEach(function (r) {
    out.push(
      cols
        .map(function (c, i) {
          return pad(r[i], widths[i]);
        })
        .join("  "),
    );
  });
  out.push("");
  out.push(
    "— " +
      (res.summary || rows.length + " rows") +
      " · run:" +
      (res.run_id || "—") +
      " · " +
      (res.elapsed || "") +
      " —",
  );
  return out.join("\n");
}

/* ── Symbol gate ────────────────────────────────────────────────────── */
export function renderGate() {
  invalidateContext();
  clearResult();
  setRunningUI(false);
  $("results-empty").hidden = true;
  $("results-scaffold").hidden = true;
  var notrun = $("results-notrun");
  if (notrun) notrun.hidden = true;
  var body = $("results-body");
  var old = body.querySelector(".vol-gate");
  if (old) old.remove();

  var d = state.dump;
  var gate = el("div", "vol-gate");
  gate.innerHTML =
    '<div class="vol-gate__icon"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square"><rect x="4" y="10" width="16" height="10" rx="1"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg></div>' +
    "<h3>" +
    escapeHtml(t("vol_gate_title")) +
    "</h3>" +
    "<p>Linux dump <b>" +
    escapeHtml(d ? d.file : "") +
    "</b> has no matching symbol table. Volatility 3 cannot resolve kernel structures for <code>" +
    escapeHtml(d ? d.kernel : "") +
    "</code> until its ISF is built.</p>";
  var actions = el("div", "vol-gate__actions");
  var docs = el("button", "wiz-btn wiz-btn--ghost", t("vol_gate_docs_btn"));
  docs.type = "button";
  docs.addEventListener("click", function () {
    activateTab("activity");
    var panel = $("isf-list");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  if (d?.context_required) {
    gate.querySelector("p").textContent = t("vol_context_help");
  }
  actions.appendChild(docs);
  gate.appendChild(actions);
  body.appendChild(gate);

  $("task-pill").className = "vol-pill vol-pill--blocked";
  $("task-state").textContent = t("vol_state_blocked");
  $("task-id").textContent = "—";
  $("task-elapsed").textContent = "—";
  $("findings").innerHTML = "";
  updateRunButton();
}

export function clearGate() {
  var body = $("results-body");
  var gate = body.querySelector(".vol-gate");
  if (gate) gate.remove();
}

/* ── CSV export ─────────────────────────────────────────────────────── */
export function downloadCsv() {
  var res = state.lastResult;
  if (!res || !res.columns || !isCurrentContext(res.context)) return;
  var esc = function (v) {
    v = String(v == null ? "" : v);
    return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  };
  var lines = [res.columns.map(esc).join(",")];
  (res.rows || []).forEach(function (r) {
    lines.push(r.map(esc).join(","));
  });
  var blob = new Blob([lines.join("\n")], { type: "text/csv" });
  var url = URL.createObjectURL(blob);
  var a = el("a");
  a.href = url;
  a.download = res.context.plugin + "-" + res.context.jobId + "-" + res.context.runId + ".csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

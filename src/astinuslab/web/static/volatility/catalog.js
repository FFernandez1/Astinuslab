// Dump selector, context bar, plugin catalog + command preview, and plugin
// selection (cache-first: selecting never auto-runs).
import { t, fmt, CAT_COLORS, $, el, escapeHtml } from "./utils.js";
import { state } from "./state.js";
import { getJSON } from "./api.js";
import { renderGate, clearGate } from "./results.js";
import { showRunPrompt, loadCachedResult } from "./run.js";
import { analysisReady } from "./readiness.js";

/* ── Dump select + context bar ──────────────────────────────────────── */
export function buildDumpSelect() {
  var sel = $("dump-select");
  sel.innerHTML = "";
  if (!state.dumps.length) {
    var o = el("option", null, t("vol_no_dumps"));
    o.value = "";
    sel.appendChild(o);
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  state.dumps.forEach(function (d) {
    var opt = el(
      "option",
      null,
      d.file + "  ·  " + d.os_label + (d.artifact_id ? " · " + d.source + " · " + d.captured : ""),
    );
    opt.value = d.selection_id || d.job_id;
    sel.appendChild(opt);
  });
  sel.value = state.dump
    ? state.dump.selection_id || state.dump.job_id
    : state.dumps[0].selection_id || state.dumps[0].job_id;
}

export function dumpByJobId(jobId) {
  for (var i = 0; i < state.dumps.length; i++)
    if ((state.dumps[i].selection_id || state.dumps[i].job_id) === jobId) return state.dumps[i];
  return null;
}

export function isYaraPlugin(name) {
  return name === "yarascan.YaraScan";
}

export function symbolsAvailable() {
  return !!(state.dump && state.dump.symbols === "ready");
}

export function renderContext() {
  var d = state.dump;
  $("analysis-context-form").hidden = !d?.context_required;
  if (!d) {
    $("m-os").textContent = "—";
    $("m-src").textContent = "—";
    $("m-size").textContent = "—";
    $("m-when").textContent = "—";
    return;
  }
  $("m-os").textContent = d.os_label + (d.kernel && d.kernel !== "—" ? " · " + d.kernel : "");
  $("m-src").textContent = d.source || "—";
  $("m-size").textContent = d.size || "—";
  $("m-when").textContent = d.captured ? d.captured.replace("T", " ").slice(0, 16) : "—";

  var badge = $("m-sym");
  badge.className = "vol-symbadge";
  var icon, label;
  if (d.symbols === "missing") {
    badge.classList.add("is-missing");
    icon = '<span class="tick">!</span>';
    label = d.sym_label || t("vol_isf_needed");
  } else {
    icon = '<span class="tick">✓</span>';
    label = d.sym_label || t("vol_symbols");
  }
  badge.innerHTML = icon + '<span id="m-sym-txt">' + escapeHtml(label) + "</span>";
}

/* ── Plugin catalog ─────────────────────────────────────────────────── */
export function loadCatalog(osFamily) {
  if (state.osLoaded === osFamily) {
    renderCatalog();
    return Promise.resolve();
  }
  state.catalog = { count: 0, categories: [] };
  state.plugin = null;
  state.osLoaded = null;
  renderCatalog();
  renderCmd();
  updateRunButton();
  return getJSON("/api/v1/volatility/catalog?os=" + encodeURIComponent(osFamily))
    .then(function (cat) {
      if (state.dump?.os_family !== osFamily) return;
      state.catalog = cat;
      state.osLoaded = osFamily;
      renderCatalog();
    })
    .catch(function (error) {
      if (state.dump?.os_family !== osFamily) return;
      var wrap = $("catalog");
      wrap.textContent = t("vol_catalog_error", "Could not load plugins. ") + (error.message || "");
      var retry = el("button", "wiz-btn wiz-btn--secondary", t("vol_retry", "Retry"));
      retry.type = "button";
      retry.addEventListener("click", function () {
        loadCatalog(osFamily);
      });
      wrap.appendChild(retry);
      updateRunButton();
    });
}

export function renderCatalog() {
  var wrap = $("catalog");
  var focusedPlugin = wrap.contains(document.activeElement)
    ? document.activeElement.getAttribute("data-plugin")
    : null;
  wrap.innerHTML = "";
  $("plugin-count").textContent = state.catalog.count || 0;
  var matches = function (p) {
    if (!state.filter) return true;
    return (p.name + " " + (p.description || "")).toLowerCase().indexOf(state.filter) !== -1;
  };
  var any = false;
  state.catalog.categories.forEach(function (cat) {
    var inCat = cat.plugins.filter(matches);
    if (!inCat.length) return;
    any = true;
    var title = el("div", "vol-cat-title");
    var sw = el("span", "swatch");
    sw.style.background = CAT_COLORS[cat.id] || "var(--text-muted)";
    title.appendChild(sw);
    title.appendChild(document.createTextNode(cat.label));
    wrap.appendChild(title);
    inCat.forEach(function (p) {
      var btn = el("button", "vol-plugin");
      btn.type = "button";
      btn.setAttribute("data-plugin", p.name);
      btn.setAttribute("aria-pressed", String(p.name === state.plugin));
      if (p.name === state.plugin) btn.classList.add("is-active");
      var name = el("div", "vol-plugin__name");
      name.appendChild(document.createTextNode(p.name));
      if (p.severity_hint === "crit") name.appendChild(el("span", "vol-plugin__flag crit", "!"));
      else if (p.severity_hint === "warn")
        name.appendChild(el("span", "vol-plugin__flag warn", "!"));
      btn.appendChild(name);
      if (p.description) btn.appendChild(el("span", "vol-plugin__desc", p.description));
      btn.addEventListener("click", function () {
        selectPlugin(p.name);
      });
      wrap.appendChild(btn);
    });
  });
  if (!any) {
    wrap.appendChild(
      el(
        "div",
        "vol-cat-empty",
        state.filter ? fmt(t("vol_cat_no_match"), { q: state.filter }) : t("vol_cat_no_plugins"),
      ),
    );
  }
  if (focusedPlugin)
    Array.from(wrap.querySelectorAll("[data-plugin]"))
      .find(function (button) {
        return button.getAttribute("data-plugin") === focusedPlugin;
      })
      ?.focus({ preventScroll: true });
}

export function renderCmd() {
  var d = state.dump;
  var suffix = isYaraPlugin(state.plugin)
    ? ' <span class="flag">--yara-file</span> &lt;rules&gt;'
    : "";
  $("cmd").innerHTML =
    '<span class="prompt">$</span> vol3 <span class="flag">-f</span> ' +
    escapeHtml(d ? d.file : "<dump>") +
    ' <span class="plug">' +
    escapeHtml(state.plugin || "<plugin>") +
    "</span>" +
    suffix;
}

export function selectPlugin(name) {
  if ($("check-run-status")) $("check-run-status").hidden = true;
  state.plugin = name;
  var yaraInput = $("yara-input");
  if (yaraInput) yaraInput.hidden = !isYaraPlugin(state.plugin);
  renderCatalog();
  renderCmd();
  updateRunButton();
  if (!symbolsAvailable()) {
    renderGate();
    return;
  }
  clearGate();
  // Cache-first: never auto-run. Show the last saved result if one exists,
  // otherwise prompt for a manual run. Yara results depend on the supplied
  // rules, so they are never restored from cache.
  if (isYaraPlugin(state.plugin)) showRunPrompt();
  else loadCachedResult();
}

export function updateRunButton() {
  var yaraRules = $("yara-rules");
  var missingYaraRules = isYaraPlugin(state.plugin) && (!yaraRules || !yaraRules.value.trim());
  var disabled =
    !analysisReady() ||
    state.running ||
    state.observing ||
    !state.plugin ||
    !symbolsAvailable() ||
    missingYaraRules;
  var btn = $("run-btn");
  if (btn) btn.disabled = disabled;
  var rerun = $("rerun-cmd");
  if (rerun) rerun.disabled = disabled;
}

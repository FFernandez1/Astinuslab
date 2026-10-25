// Top of page: action buttons + the one-line lab status strip + session footer.
import { el } from "./ui.js";
import {
  getState,
  subscribe,
  setState,
  cleanupSucceeded,
  cleanupTerminal,
  persistCleanup,
} from "./state.js";
import { endSession, exportLabPackage, exportRecipe, importRecipe, refreshSession } from "./api.js";
import { refreshSessionPoll } from "./pollers.js";
import { VM_LIFECYCLE, vmLifecycle } from "./vm-state.js";

function sessionId(session) {
  return session ? session.session_id || session.job_id || session.id || null : null;
}

// One line of lab state, replacing the metric-card strip: the numbers are the
// same, but they cost ~92px of vertical budget as cards and one row as a line.
export function deriveStatus(state) {
  const vms = state.session?.vms || [],
    networks = state.session?.networks || [];
  const count = (kind) => vms.filter((v) => vmLifecycle(v.state) === kind).length;
  const running = count(VM_LIFECYCLE.RUNNING);
  const starting = count(VM_LIFECYCLE.STARTING);
  const failed = count(VM_LIFECYCLE.FAILED);
  const firstFailed = vms.find((v) => vmLifecycle(v.state) === VM_LIFECYCLE.FAILED);
  return {
    running,
    starting,
    failed,
    total: vms.length,
    hostsText: vms.length
      ? tt("pg_status_hosts", "{running} of {total} hosts running")
          .replace("{running}", running)
          .replace("{total}", vms.length)
      : tt("pg_status_no_hosts", "no hosts yet"),
    startingText: starting
      ? tt("pg_status_starting", "{count} starting").replace("{count}", starting)
      : "",
    failedText: failed ? tt("pg_status_failed", "{count} failed").replace("{count}", failed) : "",
    networksText: networksText(networks),
    firstFailedId: firstFailed ? firstFailed.id : null,
  };
}

function networksText(networks) {
  if (!networks.length) return tt("pg_status_no_networks", "no networks yet");
  if (networks.length === 1) {
    const net = networks[0];
    const mode =
      net.isolated === false ? tt("pg_mode_shared", "shared") : tt("pg_mode_isolated", "isolated");
    return tt("pg_status_network", "1 network · {name} {cidr} · {mode}")
      .replace("{name}", net.name)
      .replace("{cidr}", net.subnet_cidr || "")
      .replace("{mode}", mode)
      .replace("  ", " ");
  }
  return tt("pg_status_networks", "{count} networks · {names}")
    .replace("{count}", networks.length)
    .replace("{names}", networks.map((n) => n.name).join(" · "));
}

function statusSep() {
  return el("span", { className: "pg-status-sep", text: "·", attrs: { "aria-hidden": "true" } });
}
function statusDot(tone) {
  return el("span", { className: "pg-status-dot", dataset: { tone } });
}

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback;
export function cleanupMessage(cleanup) {
  if (cleanup.unavailable === "missing")
    return tt(
      "pg_cleanup_missing",
      "Cleanup record unavailable. Check Logs using this session ID.",
    );
  if (cleanup.unavailable)
    return tt(
      "pg_cleanup_unavailable",
      "Cleanup status unavailable. Check your connection or sign in again.",
    );
  if (cleanup.phase === "requesting") return tt("pg_cleanup_requesting", "Requesting cleanup…");
  if (cleanupSucceeded(cleanup))
    return tt("pg_cleanup_ended", "Session ended. Captured evidence is kept.");
  if (["destroy_failed", "failed"].includes(cleanup.status))
    return tt(
      "pg_cleanup_attention",
      "Cleanup incomplete. Check Logs using this session ID; host resources may still be reserved.",
    );
  if (cleanup.status === "destroy_requested")
    return tt("pg_cleanup_requested", "Cleanup requested");
  if (["destroying", "failure_cleanup"].includes(cleanup.status))
    return tt("pg_cleanup_running", "Cleaning up…");
  return tt("pg_cleanup_pending", "Cleanup has not completed");
}

function clearCleanup() {
  persistCleanup(null);
  setState((prev) => ({
    ...prev,
    cleanup: null,
    session: null,
    selectedId: null,
    topology: { nodes: [] },
  }));
  refreshSessionPoll();
}

function selectNewLab() {
  const cleanup = getState().cleanup;
  if (!cleanup) return true;
  if (!cleanupSucceeded(cleanup) || cleanup.unavailable) return false;
  clearCleanup();
  return true;
}

function renderStatus(state) {
  const root = document.getElementById("pg-top-metrics");
  if (!root) return;
  if (state.cleanup) {
    root.replaceChildren(
      el("span", {
        attrs: { role: "status", "aria-live": "polite" },
        text: cleanupMessage(state.cleanup),
      }),
      el("code", { text: state.cleanup.session_id }),
      el("a", { attrs: { href: "/storage" }, text: tt("nav_storage", "Storage") }),
      el("a", { attrs: { href: "/logs" }, text: tt("nav_logs", "Logs") }),
      ...(cleanupTerminal(state.cleanup)
        ? [
            el("button", {
              attrs: { type: "button" },
              text: tt("pg_cleanup_dismiss", "Dismiss"),
              onClick: clearCleanup,
            }),
          ]
        : []),
    );
    return;
  }
  const s = deriveStatus(state);
  const children = [
    el("span", {
      className: "pg-status-item",
      children: [statusDot(s.running ? "ok" : "idle"), el("span", { text: s.hostsText })],
    }),
  ];
  if (s.startingText) {
    children.push(statusSep());
    children.push(
      el("span", {
        className: "pg-status-item",
        dataset: { tone: "starting" },
        children: [statusDot("starting"), el("span", { text: s.startingText })],
      }),
    );
  }
  if (s.failedText) {
    children.push(statusSep());
    children.push(
      el("span", {
        className: "pg-status-item",
        dataset: { tone: "danger" },
        children: [
          statusDot("danger"),
          el("span", { text: s.failedText }),
          el("button", {
            className: "pg-status-review",
            attrs: { type: "button" },
            text: tt("pg_wiz_review", "Review"),
            onClick: () =>
              setState((prev) => ({ ...prev, selectedId: s.firstFailedId || prev.selectedId })),
          }),
        ],
      }),
    );
  }
  children.push(statusSep());
  children.push(el("span", { className: "pg-status-nets", text: s.networksText }));
  children.push(el("span", { className: "pg-status-spacer" }));
  children.push(
    el("span", {
      className: "pg-status-elapsed",
      children: [
        el("span", { className: "pg-status-elapsed-k", text: tt("pg_status_elapsed", "elapsed") }),
        el("span", { dataset: { elapsed: "true" }, text: currentElapsedText() }),
      ],
    }),
  );
  root.replaceChildren(...children);
}
function renderSessionVisibility(id) {
  const cleanup = getState().cleanup;
  const live = document.getElementById("pg-foot-live-meta");
  if (live) live.hidden = Boolean(cleanup);
  const packageBtn = document.getElementById("btn-export-package");
  if (packageBtn) packageBtn.hidden = !id || Boolean(cleanup);
  const exportBtn = document.getElementById("btn-export-recipe");
  if (exportBtn) exportBtn.hidden = !id || Boolean(cleanup);
  const endBtn = document.getElementById("btn-end-session");
  if (endBtn) {
    endBtn.hidden = !id || Boolean(cleanup);
    endBtn.disabled = Boolean(cleanup);
  }
  const sidEl = document.getElementById("pg-foot-sid");
  if (sidEl)
    sidEl.textContent = cleanup?.session_id || id || tt("pg_foot_no_session", "no session");
  const footer = document.getElementById("pg-footer");
  if (footer) footer.dataset.active = id ? "true" : "false";
  document.querySelectorAll("[data-open-create], #btn-import-recipe").forEach((btn) => {
    btn.disabled = Boolean(cleanup && (!cleanupSucceeded(cleanup) || cleanup.unavailable));
  });
}
function setPackageExportOpen(open, opener) {
  const modal = document.getElementById("export-package-modal");
  if (!modal) return;
  if (open) {
    window.AlDialog.open(modal, { returnTo: opener, initial: "#export-package-id" });
  } else if (window.AlDialog?.current === modal) {
    window.AlDialog.close();
  } else {
    modal.hidden = true;
  }
}
function setImportOpen(open, opener) {
  const modal = document.getElementById("import-recipe-modal");
  if (!modal) return;
  if (open) {
    window.AlDialog.open(modal, {
      returnTo: opener,
      initial: "#import-recipe-text",
    });
  } else if (window.AlDialog?.current === modal) {
    window.AlDialog.close();
  } else {
    modal.hidden = true;
  }
}
function downloadJson(name, data) {
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name || "playground"}.recipe.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function wireRecipeButtons() {
  const exportBtn = document.getElementById("btn-export-recipe");
  if (exportBtn)
    exportBtn.addEventListener("click", async () => {
      try {
        const recipe = await exportRecipe();
        downloadJson(recipe.name, recipe);
      } catch (err) {
        setState((prev) => ({ ...prev, errors: { ...prev.errors, recipeExport: err.message } }));
      }
    });
  const importBtn = document.getElementById("btn-import-recipe");
  if (importBtn)
    importBtn.addEventListener("click", () => {
      if (selectNewLab()) setImportOpen(true, importBtn);
    });
  for (const id of ["import-recipe-close", "import-recipe-cancel"]) {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener("click", () => setImportOpen(false));
  }
  const submit = document.getElementById("import-recipe-submit");
  if (submit)
    submit.addEventListener("click", async () => {
      const textarea = document.getElementById("import-recipe-text"),
        errEl = document.getElementById("import-recipe-error");
      try {
        const recipe = JSON.parse(textarea?.value || "{}");
        await importRecipe(recipe);
        const session = await refreshSession();
        setState((prev) => ({
          ...prev,
          session,
          selectedId: session?.vms?.[0]?.id || session?.networks?.[0]?.id || null,
          errors: { ...prev.errors, recipeImport: undefined },
        }));
        if (errEl) {
          errEl.hidden = true;
          errEl.textContent = "";
        }
        setImportOpen(false);
      } catch (err) {
        if (errEl) {
          errEl.textContent = err.message;
          errEl.hidden = false;
        }
        setState((prev) => ({ ...prev, errors: { ...prev.errors, recipeImport: err.message } }));
      }
    });
}

function wirePackageButton() {
  const exportBtn = document.getElementById("btn-export-package");
  if (exportBtn)
    exportBtn.addEventListener("click", () => {
      const state = getState();
      const title = state.session?.name || "Local forensic lab";
      const idField = document.getElementById("export-package-id");
      const titleField = document.getElementById("export-package-title-field");
      if (idField && !idField.value) idField.value = "local-lab";
      if (titleField && !titleField.value) titleField.value = title;
      setPackageExportOpen(true, exportBtn);
    });
  for (const id of ["export-package-close", "export-package-cancel"]) {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener("click", () => setPackageExportOpen(false));
  }
  const form = document.getElementById("export-package-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorEl = document.getElementById("export-package-error");
    const submit = document.getElementById("export-package-submit");
    if (submit) submit.disabled = true;
    const value = (name) => form.elements.namedItem(name)?.value || "";
    const integer = (name) => Number.parseInt(value(name), 10);
    const jsonValue = (name, fallback) => {
      const raw = value(name).trim();
      if (!raw) return fallback;
      const parsed = JSON.parse(raw);
      return parsed;
    };
    const checked = (name) => Boolean(form.elements.namedItem(name)?.checked);
    try {
      const payload = {
        session_id: sessionId(getState().session),
        package_id: value("package_id"),
        version: value("version"),
        title: value("title"),
        description: value("description"),
        creator: value("creator"),
        license: value("license"),
        runbook: value("runbook"),
        reproducibility: form.elements.namedItem("reproducibility")?.value || "unlocked",
        verification_id: value("verification_id") || null,
        embed_assets: checked("embed_assets"),
        redistribution_confirmed: checked("redistribution_confirmed"),
        experience: {
          objectives: jsonValue("objectives", []),
          hints: jsonValue("hints", []),
          checkpoints: jsonValue("checkpoints", []),
        },
        resources: {
          minimum: {
            cpu_threads: integer("minimum_cpu"),
            memory_mb: integer("minimum_memory"),
            free_disk_bytes: integer("minimum_disk") * 1024 ** 3,
          },
          recommended: {
            cpu_threads: integer("recommended_cpu"),
            memory_mb: integer("recommended_memory"),
            free_disk_bytes: integer("recommended_disk") * 1024 ** 3,
          },
        },
      };
      const result = await exportLabPackage(payload);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      if (errorEl) {
        errorEl.hidden = true;
        errorEl.textContent = "";
      }
      setPackageExportOpen(false);
    } catch (err) {
      if (errorEl) {
        errorEl.textContent = err.message;
        errorEl.hidden = false;
      }
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

function wireHeaderButtons() {
  document.querySelectorAll("[data-open-create]").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (selectNewLab())
        setState((prev) => ({ ...prev, ui: { ...prev.ui, createRole: btn.dataset.openCreate } }));
    }),
  );
  const endBtn = document.getElementById("btn-end-session");
  let ending = false;
  if (endBtn)
    endBtn.addEventListener("click", async () => {
      const st = getState(),
        id = sessionId(st.session);
      if (!id || ending || st.cleanup) return;
      ending = true;
      const tt = window.t || ((k) => k);
      try {
        if (!(await window.confirmDialog(tt("pg_end_session_confirm")))) return;
        if (sessionId(getState().session) !== id || getState().cleanup) return;
        setState((prev) => ({
          ...prev,
          cleanup: { session_id: id, phase: "requesting", status: null },
          ui: { ...prev.ui, createRole: null, createWizard: null, createNetworkOpen: false },
          errors: { ...prev.errors, endSession: undefined },
        }));
        refreshSessionPoll();
        const accepted = await endSession(id);
        if (getState().cleanup?.session_id !== id) return;
        if (accepted?.session_id !== id) throw new Error("Cleanup response identity mismatch");
        persistCleanup(id);
        setState((prev) => ({
          ...prev,
          cleanup: { session_id: id, phase: "accepted", status: "destroy_requested" },
        }));
        refreshSessionPoll();
      } catch (err) {
        setState((prev) => ({
          ...prev,
          cleanup: null,
          errors: { ...prev.errors, endSession: err.message },
        }));
        refreshSessionPoll();
        const errEl = document.getElementById("end-session-error");
        if (errEl) {
          errEl.textContent = err.message;
          errEl.hidden = false;
        }
        if (sessionId(getState().session) === id && document.activeElement === document.body)
          endBtn.focus();
      } finally {
        ending = false;
      }
    });
}

let elapsedStartMs = null;
function currentElapsedText() {
  return elapsedStartMs == null ? "00:00:00" : formatElapsed(Date.now() - elapsedStartMs);
}
function formatElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
function formatUtc(d) {
  const yyyy = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${yyyy}-${mo}-${dd}T${hh}:${mm}Z`;
}

function parseStartedAt(session) {
  if (!session?.started_at) return null;
  const t = new Date(session.started_at).getTime();
  return Number.isFinite(t) ? t : null;
}

function renderCleanupWarning(failure) {
  const warning = document.getElementById("pg-cleanup-warning");
  if (!warning) return;
  const tt = window.t || ((k) => k);
  const key = failure ? failure.session_id + ":" + (failure.finished_at || "") : "";
  let dismissed = "";
  try {
    dismissed = localStorage.getItem("pg.cleanupDismissed") || "";
  } catch (_) {}
  if (!failure || key === dismissed) {
    warning.hidden = true;
    warning.textContent = "";
    return;
  }
  warning.hidden = false;
  const dismiss = () => {
    try {
      localStorage.setItem("pg.cleanupDismissed", key);
    } catch (_) {}
    warning.hidden = true;
  };
  warning.replaceChildren(
    el("span", { text: tt("pg_cleanup_failed") + " (" + failure.session_id + "). " }),
    el("a", { attrs: { href: "/logs" }, text: "Logs" }),
    el("button", { attrs: { type: "button" }, text: tt("pg_cleanup_dismiss"), onClick: dismiss }),
  );
}

function mountFooter() {
  const utcEl = document.getElementById("pg-foot-utc");
  function tickElapsed() {
    const text = currentElapsedText();
    // The status line is rebuilt on every session change, so write to whatever
    // elapsed readouts exist right now rather than holding one element.
    document.querySelectorAll("[data-elapsed]").forEach((node) => {
      node.textContent = text;
    });
  }
  function tickUtc() {
    if (utcEl) utcEl.textContent = formatUtc(new Date());
  }
  tickUtc();
  setInterval(tickElapsed, 1000);
  setInterval(tickUtc, 60_000);
  subscribe(
    (s) => parseStartedAt(s.session) || (sessionId(s.session) ? -1 : null),
    (sig) => {
      if (sig == null) {
        elapsedStartMs = null;
      } else if (sig === -1) {
        elapsedStartMs = elapsedStartMs ?? Date.now();
      } else {
        elapsedStartMs = sig;
      }
      tickElapsed();
    },
  );
}

export function mountTop() {
  wireHeaderButtons();
  wireRecipeButtons();
  wirePackageButton();
  subscribe(
    (s) => ({ vms: s.session?.vms, networks: s.session?.networks, cleanup: s.cleanup }),
    () => renderStatus(getState()),
  );
  subscribe(
    (s) => ({ id: sessionId(s.session), cleanup: s.cleanup }),
    ({ id }) => renderSessionVisibility(id),
  );
  subscribe(
    (s) => ({ vmCount: s.session?.vms?.length || 0, netCount: s.session?.networks?.length || 0 }),
    ({ vmCount, netCount }) => {
      const root = document.getElementById("playground-root");
      if (root) root.dataset.populated = vmCount > 0 || netCount > 0 ? "true" : "false";
    },
  );
  subscribe(
    (s) => s.cleanupFailure,
    (failure) => renderCleanupWarning(failure),
  );
  mountFooter();
}

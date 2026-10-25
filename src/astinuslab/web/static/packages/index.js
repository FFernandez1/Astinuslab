import {
  cancelPrepare,
  cancelVerification,
  commitInspection,
  deletePackage,
  exportVerification,
  getOperationStatus,
  getPrepareStatus,
  getVerificationStatus,
  getPackageExperience,
  getCurrentPlaygroundSession,
  inspectPackage,
  instanceAction,
  launchPackage,
  listPackages,
  listActivity,
  preflightPackage,
  preparePackage,
  saveDraft,
  verifyCleanRun,
} from "./api.js";
import { wireCreatorHandoff } from "./creator.js";
import { selectActivity } from "./activity.js";
import { createPoller } from "./poller.js";
import {
  renderVerification,
  renderVerificationHistory,
  renderVerificationSelector,
} from "./render-verification.js";
import { getState, setState, subscribe } from "./state.js";
import { renderInspection, renderPackageExperience } from "./render-inspection.js";
import { renderPackageList } from "./render-library.js";
import { renderOperation, renderPrepareStatus, renderPreflight } from "./render-status.js";
import { INSTANCE_POLL_STOP, PREPARE_TERMINAL, VERIFICATION_TERMINAL } from "./states.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;
const root = () => document.getElementById("packages-root");
const byId = (id) => document.getElementById(id);
const errorText = (error) => error?.message || tt("pkg_request_failed", "Package request failed");
let libraryGeneration = 0;
let preflightGeneration = 0;
let detailGeneration = 0;
const deletedPackages = new Set();
const pendingInstanceActions = new Set();

function packageLabel(id) {
  const pkg = getState().packages.find((record) => record.id === id);
  return pkg ? `${pkg.title} · ${pkg.package_id}@${pkg.version}` : id || "";
}

async function handleOpenPackage(recordId, opener) {
  const token = ++detailGeneration;
  preflightGeneration++;
  setState((prev) => ({ ...prev, selectedPackageId: recordId }));
  resetPanel("package-detail", `${packageLabel(recordId)} · ${tt("pkg_loading", "Loading…")}`);
  const preflight = getState().preflight[recordId];
  if (preflight) renderPreflightFor(recordId, preflight);
  else
    resetPanel(
      "package-action-status",
      `${packageLabel(recordId)} · ${tt("pkg_preflight_idle", "No preflight selected.")}`,
    );
  try {
    const record = await getPackageExperience(recordId);
    if (
      token !== detailGeneration ||
      getState().selectedPackageId !== recordId ||
      deletedPackages.has(recordId)
    )
      return;
    const selected = getState().packages.find((pkg) => pkg.id === recordId);
    if (
      record.package_record_id !== recordId ||
      (selected?.archive_sha256 && record.archive_sha256 !== selected.archive_sha256)
    )
      throw new Error(
        tt("pkg_detail_mismatch", "Package instructions do not match the selected package."),
      );
    renderPackageExperience(byId("package-detail"), record);
    if (opener && document.activeElement === opener)
      byId("package-detail").querySelector("h3")?.focus();
  } catch (error) {
    if (token === detailGeneration) {
      resetPanel("package-detail", `${packageLabel(recordId)} · ${errorText(error)}`);
      showError(errorText(error));
    }
  }
}

function wirePackageViews() {
  const modes = ["use", "create"];
  const select = (mode) => {
    for (const value of modes) {
      const tab = byId(`package-${value}-tab`);
      if (!tab) continue;
      tab.setAttribute("aria-selected", String(value === mode));
      tab.tabIndex = value === mode ? 0 : -1;
      const panel = byId(value === "use" ? "package-recipient-panel" : "package-creator-panel");
      if (panel) panel.hidden = value !== mode;
    }
  };
  for (const mode of modes) {
    const tab = byId(`package-${mode}-tab`);
    tab?.addEventListener("click", () => select(mode));
    tab?.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next =
        event.key === "Home"
          ? "use"
          : event.key === "End"
            ? "create"
            : mode === "use"
              ? "create"
              : "use";
      select(next);
      byId(`package-${next}-tab`).focus();
    });
  }
  select("use");

  const fileInput = byId("package-file");
  const fileName = byId("package-file-name");
  const fileDrop = byId("package-file-drop");
  if (fileInput && fileName && fileDrop) {
    fileInput.addEventListener("change", () => {
      const chosen = fileInput.files?.[0]?.name || "";
      // The placeholder comes from the attribute, not from the node's text at
      // wire time: the text has already been replaced once a file is chosen.
      fileName.textContent = chosen || fileName.dataset.placeholder || "";
      fileDrop.classList.toggle("is-selected", Boolean(chosen));
    });
  }
}

// A slot's generation fences user intent immediately, before the action reply
// supplies an operation ID. The poller separately fences status transport.
function createSlot({ request, terminal, render, save, panelId, idle }) {
  let generation = 0;
  let selection = null;
  const poller = createPoller({
    request,
    isTerminal: (record) => terminal.has(record.state),
    onSuccess(record, id) {
      if (selection?.id !== id) return;
      selection.record = record;
      save(record);
      render(record);
    },
    onError(error, id) {
      if (selection?.id === id) showError(errorText(error));
    },
  });
  return {
    token: () => generation,
    owns: (token) => token === generation,
    selected: (id) => selection?.id === id,
    rerender() {
      if (selection?.record) render(selection.record);
    },
    claim(packageId = selection?.packageId) {
      generation += 1;
      selection = { id: null, packageId, fallback: selection?.record || selection?.fallback };
      poller.stop();
      return generation;
    },
    accept(token, record) {
      if (token !== generation || deletedPackages.has(record.package_record_id)) return;
      selection = {
        id: record.id,
        packageId: record.package_record_id || selection?.packageId,
        record,
      };
      save(record);
      render(record);
      if (terminal.has(record.state)) poller.stop();
      else poller.start(record.id);
    },
    fail(token, error) {
      if (token !== generation) return;
      showError(errorText(error));
      // A failed mutation leaves the durable result usable and resumes polling
      // under fresh custody, even when its ID was already terminal before retry.
      const record = selection?.record || selection?.fallback;
      if (record) this.accept(token, record);
    },
    removePackage(packageId) {
      if (selection?.packageId !== packageId) return;
      generation += 1;
      selection = null;
      poller.stop();
      resetPanel(panelId, idle());
    },
  };
}

const prepareSlot = createSlot({
  request: getPrepareStatus,
  terminal: PREPARE_TERMINAL,
  render: (record) =>
    renderPrepareStatus(
      byId("package-prepare-status"),
      record,
      packageLabel(record.package_record_id),
    ),
  save: (record) =>
    setState((previous) => ({
      ...previous,
      prepares: { ...previous.prepares, [record.id]: record },
    })),
  panelId: "package-prepare-status",
  idle: () => tt("pkg_prepare_idle", "No package preparation selected."),
});
const operationSlot = createSlot({
  request: getOperationStatus,
  terminal: INSTANCE_POLL_STOP,
  render: (record) =>
    renderOperation(
      byId("package-operation-status"),
      record,
      packageLabel(record.package_record_id),
    ),
  save: (record) =>
    setState((previous) => ({
      ...previous,
      operations: { ...previous.operations, [record.id]: record },
    })),
  panelId: "package-operation-status",
  idle: () => tt("pkg_operation_idle", "No package operation selected."),
});
const verificationSlot = createSlot({
  request: getVerificationStatus,
  terminal: VERIFICATION_TERMINAL,
  render: (record) =>
    renderVerification(byId("verification-status"), record, handleCancelVerification),
  save: (verification) =>
    setState((previous) => ({
      ...previous,
      verification,
      verifications: { ...previous.verifications, [verification.id]: verification },
    })),
});

async function confirmAction(message) {
  if (typeof window.confirmDialog !== "function") {
    showError(tt("pkg_confirm_unavailable", "Confirmation dialog unavailable"));
    return false;
  }
  return window.confirmDialog(message);
}

function resetPanel(id, message) {
  const panel = byId(id);
  if (!panel) return;
  const idle = document.createElement("p");
  idle.className = "text-muted small";
  idle.textContent = message;
  panel.replaceChildren(idle);
}

function showError(message) {
  const node = byId("package-error");
  if (!node) return;
  node.textContent = message || "";
  node.hidden = !message;
}

function announce(message) {
  const node = byId("package-status-message");
  if (node) node.textContent = message || "";
}

function randomKey(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function showInspection(inspection) {
  renderInspection(byId("package-inspection"), inspection, async (inspectionId) => {
    try {
      await commitInspection(inspectionId);
      setState((previous) => ({ ...previous, inspection: null, error: "" }));
      announce(tt("pkg_imported", "Package imported into the local library."));
      await refreshPackages();
    } catch (error) {
      showError(errorText(error));
    }
  });
}

async function refreshPackages() {
  const token = ++libraryGeneration;
  try {
    const packages = await listPackages();
    if (token !== libraryGeneration) return;
    setState((previous) => ({
      ...previous,
      packages: Array.isArray(packages) ? packages : [],
      error: "",
    }));
  } catch (error) {
    if (token === libraryGeneration) showError(errorText(error));
  }
}

async function handleInspect(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.namedItem("file")?.files?.[0];
  if (!file)
    return showError(tt("pkg_archive_required", "Choose an .astinuslab archive to inspect."));
  const submit = form.querySelector("button[type=submit]");
  if (submit) submit.disabled = true;
  try {
    const inspection = await inspectPackage(file);
    setState((previous) => ({ ...previous, inspection, error: "" }));
    announce(
      tt("pkg_inspection_complete", "Inspection complete. No package content has been executed."),
    );
  } catch (error) {
    showError(errorText(error));
  } finally {
    if (submit) submit.disabled = false;
  }
}

function renderPreflightFor(recordId, result) {
  const status = byId("package-action-status");
  if (!status) return;
  status.dataset.recordId = recordId;
  renderPreflight(status, result, packageLabel(recordId));
}

async function handlePreflight(recordId) {
  const token = ++preflightGeneration;
  try {
    announce(tt("pkg_preflight_started", "Checking host prerequisites and package warnings…"));
    const result = await preflightPackage(recordId);
    if (token !== preflightGeneration || deletedPackages.has(recordId)) return;
    setState((previous) => ({
      ...previous,
      selectedPackageId: recordId,
      preflight: { ...previous.preflight, [recordId]: result },
      error: "",
    }));
    renderPreflightFor(recordId, result);
  } catch (error) {
    if (token === preflightGeneration && !deletedPackages.has(recordId))
      showError(errorText(error));
  }
}

async function handlePrepare(recordId) {
  const token = prepareSlot.claim(recordId);
  try {
    announce(tt("pkg_prepare_started", "Preparing immutable package assets…"));
    const preparation = await preparePackage(recordId, {
      idempotency_key: randomKey("prepare"),
      network_opt_in: Boolean(byId("prepare-network")?.checked),
    });
    prepareSlot.accept(token, preparation);
  } catch (error) {
    prepareSlot.fail(token, error);
  }
}

async function handleDelete(recordId) {
  try {
    if (
      !(await confirmAction(
        tt(
          "pkg_delete_confirm",
          "Remove this package from the local library? Running or referenced content is protected; unreferenced content may be reclaimed by garbage collection.",
        ),
      ))
    )
      return;
    await deletePackage(recordId);
    const current = getState();
    deletedPackages.add(recordId);
    if (getState().selectedPackageId === recordId) {
      detailGeneration++;
      resetPanel("package-detail", tt("pkg_select_package", "Select a package"));
    }
    libraryGeneration += 1;
    prepareSlot.removePackage(recordId);
    operationSlot.removePackage(recordId);
    setState((previous) => ({
      ...previous,
      packages: previous.packages.filter((item) => item.id !== recordId),
      selectedPackageId:
        previous.selectedPackageId === recordId ? null : previous.selectedPackageId,
      preflight: Object.fromEntries(
        Object.entries(previous.preflight).filter(([id]) => id !== recordId),
      ),
      prepares: Object.fromEntries(
        Object.entries(previous.prepares).filter(
          ([, item]) => item?.package_record_id !== recordId,
        ),
      ),
      operations: Object.fromEntries(
        Object.entries(previous.operations).filter(
          ([, item]) => item?.package_record_id !== recordId,
        ),
      ),
      error: "",
    }));
    if (current.selectedPackageId === recordId) {
      resetPanel("package-action-status", tt("pkg_select_package", "Select a package"));
    }
    announce(tt("pkg_package_deleted", "Package deleted."));
  } catch (error) {
    showError(errorText(error));
  }
}

async function handleLaunch(recordId) {
  const preflight = getState().preflight[recordId];
  if (!preflight?.ready)
    return showError(
      tt("pkg_launch_requires_preflight", "Run a successful preflight before launching."),
    );
  const status = byId("package-action-status");
  const acknowledged = [...(status?.querySelectorAll("[data-warning-index]") || [])].every(
    (item) => item.checked,
  );
  const egress = status?.querySelector("[data-egress-ack]");
  if (!acknowledged || (egress && !egress.checked))
    return showError(
      tt("pkg_ack_warnings", "Review and acknowledge every preflight warning before launching."),
    );
  const token = operationSlot.claim(recordId);
  try {
    const operation = await launchPackage(recordId, {
      acknowledged_warning_token: preflight.warning_token,
      acknowledge_guest_egress: Boolean(egress?.checked),
      idempotency_key: randomKey("launch"),
    });
    operationSlot.accept(token, operation);
  } catch (error) {
    operationSlot.fail(token, error);
  }
}

async function handleInstanceAction(instanceId, action) {
  if (!operationSlot.selected(instanceId) || pendingInstanceActions.has(instanceId)) return;
  pendingInstanceActions.add(instanceId);
  let token = operationSlot.token();
  try {
    if (action === "open") {
      const operation = getState().operations[instanceId];
      if (operation?.state !== "ready" || !operation.session_id) return;
      const current = await getCurrentPlaygroundSession();
      if (!operationSlot.owns(token) || !operationSlot.selected(instanceId)) return;
      const latest = getState().operations[instanceId];
      if (
        latest?.state !== "ready" ||
        latest.session_id !== operation.session_id ||
        (current?.session_id || current?.job_id || current?.id) !== operation.session_id
      )
        throw new Error(
          tt("pkg_inactive_lab", "This instance is no longer the active Playground session."),
        );
      window.location.assign("/playground");
      return;
    }
    if (["reset", "teardown"].includes(action)) {
      const packageId = getState().operations[instanceId]?.package_record_id;
      const identity =
        getState().packages.find((record) => record.id === packageId)?.title || instanceId;
      const message =
        action === "reset"
          ? tt(
              "pkg_reset_confirm",
              "Reset this lab? Current VM changes will be discarded and the lab will restart from its saved starting state. Captured evidence is kept.",
            )
          : tt(
              "pkg_teardown_confirm",
              "End this lab instance? Running VMs and networks will be removed. Imported package content and captured evidence are kept.",
            );
      if (!(await confirmAction(`${identity}: ${message}`))) return;
    }
    if (!operationSlot.owns(token) || !operationSlot.selected(instanceId)) return;
    token = operationSlot.claim();
    const operation = await instanceAction(instanceId, action);
    operationSlot.accept(token, operation);
  } catch (error) {
    operationSlot.fail(token, error);
  } finally {
    pendingInstanceActions.delete(instanceId);
  }
}

function jsonLines(value) {
  if (!value.trim()) return [];
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || !parsed.every((item) => typeof item === "string"))
    throw new Error(
      tt("pkg_json_arrays_error", "Objectives and hints must be JSON string arrays."),
    );
  return parsed;
}

async function handleVerify(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const token = verificationSlot.claim();
  try {
    const definition = JSON.parse(form.elements.namedItem("definition")?.value || "{}");
    definition.experience = {
      ...(definition.experience || {}),
      objectives: jsonLines(form.elements.namedItem("objectives")?.value || "[]"),
      hints: jsonLines(form.elements.namedItem("hints")?.value || "[]"),
      checkpoints: JSON.parse(form.elements.namedItem("checkpoints")?.value || "[]"),
    };
    const draft = await saveDraft({
      definition,
      runbook: form.elements.namedItem("runbook")?.value || "",
      supporting_files: JSON.parse(form.elements.namedItem("supporting_files")?.value || "{}"),
      authored_session_id: form.elements.namedItem("authored_session_id")?.value || null,
    });
    if (!verificationSlot.owns(token)) return;
    const verification = await verifyCleanRun({
      draft_id: draft.id,
    });
    verificationSlot.accept(token, verification);
  } catch (error) {
    verificationSlot.fail(token, error);
  }
}

// The browser reads an object URL after the click returns, so the URL outlives
// the call that started the download rather than being revoked under it.
const OBJECT_URL_TTL_MS = 60000;

// The browser owns the file: hand it the bytes the server already authorized.
// A missing URL or anchor API throws here instead of reporting a download that
// never happened.
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), OBJECT_URL_TTL_MS);
}

async function handleExportVerification(id, options) {
  try {
    showError("");
    announce(tt("pkg_export_started", "Building the verified package archive…"));
    const artifact = await exportVerification(id, {
      embed_assets: Boolean(options?.embed_assets),
      redistribution_confirmed: Boolean(options?.redistribution_confirmed),
    });
    saveBlob(artifact.blob, artifact.filename);
    announce(tt("pkg_export_downloaded", "Verified package downloaded."));
  } catch (error) {
    // A refused export must stay readable: the server explains what consent or
    // custody check failed, and the completed verification remains exportable.
    announce("");
    showError(errorText(error));
    return { error: errorText(error) };
  }
}

async function handleCancelVerification(id) {
  if (!verificationSlot.selected(id)) return;
  const token = verificationSlot.claim();
  try {
    verificationSlot.accept(token, await cancelVerification(id));
  } catch (error) {
    verificationSlot.fail(token, error);
  }
}

function wireEvents() {
  wirePackageViews();
  wireCreatorHandoff();
  byId("package-inspect-form")?.addEventListener("submit", handleInspect);
  byId("creator-verify-form")?.addEventListener("submit", handleVerify);
  const packageAction = (event) => {
    const action = event.target.closest("[data-package-action]");
    if (!action) return;
    const recordId = action.dataset.recordId;
    if (action.dataset.packageAction === "open") handleOpenPackage(recordId, action);
    else if (action.dataset.packageAction === "preflight") handlePreflight(recordId);
    else if (action.dataset.packageAction === "prepare") handlePrepare(recordId);
    else if (action.dataset.packageAction === "delete") handleDelete(recordId);
  };
  byId("package-library")?.addEventListener("click", packageAction);
  byId("package-detail")?.addEventListener("click", packageAction);
  byId("package-prepare-status")?.addEventListener("click", async (event) => {
    const cancel = event.target.closest("[data-prepare-cancel]");
    if (!cancel) return;
    const id = cancel.dataset.prepareCancel;
    if (!prepareSlot.selected(id)) return;
    const token = prepareSlot.claim();
    try {
      prepareSlot.accept(token, await cancelPrepare(id));
    } catch (error) {
      prepareSlot.fail(token, error);
    }
  });
  byId("package-action-status")?.addEventListener("click", (event) => {
    const launch = event.target.closest("[data-warning-token]");
    if (launch) handleLaunch(byId("package-action-status").dataset.recordId);
  });
  byId("package-operation-status")?.addEventListener("click", (event) => {
    const action = event.target.closest("[data-instance-action]");
    if (action) handleInstanceAction(action.dataset.instanceId, action.dataset.instanceAction);
  });
}

async function refreshActivity() {
  const prepareToken = prepareSlot.token();
  const operationToken = operationSlot.token();
  const verificationToken = verificationSlot.token();
  try {
    const activity = selectActivity(await listActivity());
    const retained = (records) =>
      Object.fromEntries(
        Object.entries(records).filter(
          ([, record]) => !deletedPackages.has(record.package_record_id),
        ),
      );
    setState((previous) => ({
      ...previous,
      prepares: { ...retained(activity.prepares), ...previous.prepares },
      operations: { ...retained(activity.operations), ...previous.operations },
      verifications: { ...activity.verifications, ...previous.verifications },
    }));
    if (activity.prepare) prepareSlot.accept(prepareToken, activity.prepare);
    if (activity.operation) operationSlot.accept(operationToken, activity.operation);
    if (activity.verification && verificationToken === 0)
      verificationSlot.accept(verificationToken, activity.verification);
  } catch (error) {
    if (
      prepareSlot.owns(prepareToken) &&
      operationSlot.owns(operationToken) &&
      verificationSlot.owns(verificationToken)
    )
      showError(errorText(error));
  }
}

function boot() {
  if (!root()) return;
  byId("verification-attempt")?.addEventListener("change", (event) => {
    const record = getState().verifications[event.target.value];
    if (record) verificationSlot.accept(verificationSlot.claim(), record);
  });
  subscribe(
    (state) => state.packages,
    (packages) => {
      renderPackageList(byId("package-library"), packages);
      prepareSlot.rerender();
      operationSlot.rerender();
    },
  );
  subscribe((state) => state.inspection, showInspection);
  subscribe(
    (state) => state.verifications,
    (verifications) =>
      renderVerificationHistory(
        byId("verification-history"),
        Object.values(verifications).filter((record) => record?.state === "verified"),
        handleExportVerification,
      ),
  );
  subscribe((state) => state.error, showError);
  subscribe(
    (state) => state.verifications,
    (records) =>
      renderVerificationSelector(
        byId("verification-attempt"),
        Object.values(records),
        getState().verification?.id,
      ),
  );
  wireEvents();
  refreshPackages();
  refreshActivity();
}

if (document.readyState === "loading")
  document.addEventListener("DOMContentLoaded", boot, { once: true });
else boot();

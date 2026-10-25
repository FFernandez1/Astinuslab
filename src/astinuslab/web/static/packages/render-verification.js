import { el } from "../lib/dom.js";
import { badgeVariant } from "./render-library.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;
// Feedback belongs to an ID within a panel, never to a disposable button.
const panels = new WeakMap();

export function renderVerification(container, verification, onCancel) {
  if (!container || !verification) return;
  let panel = panels.get(container);
  if (!panel) {
    panel = { feedback: new Map(), id: null, update: null };
    panels.set(container, panel);
  }
  const id = verification.id;
  const signature = JSON.stringify([
    id,
    verification.state,
    verification.progress_percent,
    verification.error_code,
    verification.error_summary,
  ]);
  panel.onCancel = onCancel;
  if (panel.signature === signature) return;
  panel.signature = signature;
  panel.id = id;
  const label = el("span", {
    className: `wiz-badge ${badgeVariant(verification.state)}`,
    text:
      verification.state === "failed"
        ? tt("pkg_verification_failed", "Verification failed")
        : verification.state === "cancelled"
          ? tt("pkg_verification_cancelled", "Verification cancelled")
          : `${verification.state} · ${verification.progress_percent || 0}%`,
  });
  const identifier = el("code", {
    className: "package-verification-id",
    text: id,
    attrs: { "aria-label": tt("pkg_verification_id", "Clean-run verification ID") },
    dataset: { verificationId: id },
  });
  identifier.tabIndex = 0;
  const copyLabel = tt("pkg_copy_verification_id", "Copy verification ID");
  const copy = el("button", {
    className: "wiz-btn wiz-btn--ghost",
    text: copyLabel,
    attrs: { type: "button", "aria-label": copyLabel },
    dataset: { copyVerificationId: id },
  });
  const feedback = el("span", { attrs: { role: "status", "aria-live": "polite" } });
  panel.update = () => {
    const state = panel.feedback.get(id) || "";
    copy.dataset.copyState = state;
    feedback.className = state === "failed" ? "form-error" : "text-muted small";
    feedback.textContent =
      state === "copied"
        ? tt("pkg_copy_succeeded", "Verification ID copied.")
        : state === "failed"
          ? tt("pkg_copy_failed", "Copy failed; select the verification ID above.")
          : "";
  };
  panel.update();
  copy.addEventListener("click", async () => {
    let state;
    try {
      if (!globalThis.navigator?.clipboard?.writeText)
        throw new Error(tt("pkg_clipboard_unavailable", "Clipboard unavailable"));
      await navigator.clipboard.writeText(id);
      state = "copied";
    } catch (_) {
      state = "failed";
    }
    panel.feedback.set(id, state);
    if (panel.id === id) panel.update();
  });
  // No <br>: .package-status is a grid, so each child already starts a row and
  // the break would only add an empty one.
  const children = [label, identifier, copy, feedback];
  if (verification.state === "failed") {
    children.push(
      el("p", {
        className: "form-error",
        text:
          verification.error_summary ||
          tt(
            "pkg_verification_failure_fallback",
            "The verification failed without a detailed reason. Use the verification ID when checking runner logs.",
          ),
      }),
    );
  }
  if (["reserved", "running"].includes(verification.state)) {
    children.push(
      el("button", {
        className: "wiz-btn wiz-btn--ghost",
        text: tt("pkg_cancel", "Cancel"),
        attrs: { type: "button" },
        dataset: { verificationCancel: id },
        onClick: () => panel.onCancel?.(id),
      }),
    );
  }
  container.replaceChildren(...children);
}

const selectors = new WeakMap();
export function renderVerificationSelector(select, records, selectedId) {
  if (!select) return;
  const signature = JSON.stringify(records.map(({ id, state }) => [id, state]));
  if (selectors.get(select) !== signature) {
    select.replaceChildren(
      ...records.map(({ id, state }) =>
        el("option", {
          attrs: { value: id },
          text: `${id} · ${state}`,
        }),
      ),
    );
    selectors.set(select, signature);
  }
  select.disabled = records.length === 0;
  select.value = selectedId || "";
}

// Completed attempts stay readable after expiry. Embedding remains opt-in.
const histories = new WeakMap();

// A row belongs to its attempt ID, not to a render pass. Consent the creator
// has ticked and an export already in flight both outlive unrelated polling.
function exportRow(id) {
  const embed = el("input", { attrs: { type: "checkbox" }, dataset: { exportEmbed: id } });
  const confirm = el("input", { attrs: { type: "checkbox" }, dataset: { exportConfirm: id } });
  const exportLabel = tt("pkg_export_verified", "Export verified package");
  const button = el("button", {
    className: "wiz-btn wiz-btn--ghost",
    text: exportLabel,
    attrs: { type: "button", "aria-label": `${exportLabel} ${id}` },
    dataset: { verificationExport: id },
  });
  const title = el("strong");
  const identity = el("span", { className: "text-muted small" });
  const deadline = el("p", { className: "text-muted small" });
  const refusal = el("p", { className: "form-error", attrs: { role: "status" } });
  const entry = { id, busy: false, onExport: null, record: null, timer: null };
  const eligible = () =>
    entry.record?.attestation_status === "unexpired" &&
    Date.parse(entry.record.attestation_expires_at) > Date.now();
  entry.dispose = () => clearTimeout(entry.timer);
  entry.update = (record) => {
    entry.record = record;
    entry.dispose();
    const setText = (node, text) => {
      if (node.textContent !== text) node.textContent = text;
    };
    setText(title, record.package_title || "");
    setText(
      identity,
      record.package_id ? `${record.package_id}@${record.package_version || ""}` : "",
    );
    const expiry = Date.parse(record.attestation_expires_at);
    const expired =
      record.attestation_status === "expired" ||
      (record.attestation_status === "unexpired" &&
        Number.isFinite(expiry) &&
        expiry <= Date.now());
    setText(
      deadline,
      eligible()
        ? tt("pkg_export_until", "Export until {utc_time}").replace(
            "{utc_time}",
            record.attestation_expires_at,
          )
        : expired
          ? tt(
              "pkg_attestation_expired",
              "Attestation expired — rebuild the authored lab and verify again.",
            )
          : tt(
              "pkg_attestation_unavailable",
              "Attestation unavailable. Check runner logs using this verification ID.",
            ),
    );
    button.disabled = entry.busy || !eligible();
    if (eligible())
      entry.timer = setTimeout(
        () => entry.update(entry.record),
        Math.min(expiry - Date.now(), 2147483647),
      );
  };
  const setBusy = (busy) => {
    entry.busy = busy;
    button.disabled = busy || !eligible();
    button.dataset.exportState = busy ? "running" : "";
  };
  button.addEventListener("click", async () => {
    // One archive per click: a second request would rebuild the same large
    // artifact and race the first download.
    if (entry.busy || !eligible()) return;
    setBusy(true);
    refusal.textContent = "";
    try {
      const result = await entry.onExport?.(id, {
        embed_assets: Boolean(embed.checked),
        redistribution_confirmed: Boolean(confirm.checked),
      });
      refusal.textContent = result?.error || "";
    } catch (error) {
      refusal.textContent = error.message || String(error);
    } finally {
      // A refused export stays retryable: the control returns either way.
      setBusy(false);
    }
  });
  setBusy(false);
  entry.node = el("div", {
    className: "package-verification-row",
    dataset: { verificationRow: id },
    children: [
      title,
      identity,
      el("code", { className: "package-verification-id", text: id }),
      deadline,
      el("label", {
        className: "package-check",
        children: [embed, el("span", { text: tt("pkg_export_embed", "Embed clean-start layers") })],
      }),
      el("label", {
        className: "package-check",
        children: [
          confirm,
          el("span", {
            text: tt(
              "pkg_export_confirm",
              "I hold the rights to redistribute these embedded bytes",
            ),
          }),
        ],
      }),
      button,
      refusal,
    ],
  });
  return entry;
}

export function renderVerificationHistory(container, verifications, onExport) {
  if (!container) return;
  const records = verifications || [];
  let history = histories.get(container);
  if (!history) {
    history = { rows: new Map(), rendered: null };
    histories.set(container, history);
  }
  const nodes = records.map((record) => {
    let entry = history.rows.get(record.id);
    if (!entry) {
      entry = exportRow(record.id);
      history.rows.set(record.id, entry);
    }
    entry.onExport = onExport;
    entry.update(record);
    return entry.node;
  });
  const present = new Set(records.map((record) => record.id));
  for (const [id, entry] of history.rows)
    if (!present.has(id)) {
      entry.dispose();
      history.rows.delete(id);
    }
  // Rewriting an unchanged list would blur whatever the creator is using, so
  // the container is only touched when the visible rows actually differ.
  if (
    history.rendered &&
    history.rendered.length === nodes.length &&
    history.rendered.every((node, index) => node === nodes[index])
  )
    return;
  history.rendered = nodes;
  container.replaceChildren(
    ...(nodes.length
      ? nodes
      : [
          el("p", {
            className: "text-muted small",
            text: tt("pkg_verification_history_empty", "No completed verifications yet."),
          }),
        ]),
  );
}

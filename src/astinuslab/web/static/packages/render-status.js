import { el } from "../lib/dom.js";
import { formatBytes, badgeVariant } from "./render-library.js";
import { INSTANCE_CLEANUP_INCOMPLETE, INSTANCE_TERMINAL, PREPARE_TERMINAL } from "./states.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;

function line(label, value) {
  return el("div", {
    className: "package-status__line",
    children: [
      el("span", { text: label }),
      el("strong", { text: value == null ? tt("pkg_missing_value", "—") : String(value) }),
    ],
  });
}

export function renderPrepareStatus(container, prepare, packageLabel = prepare?.package_record_id) {
  if (!container || !prepare) return;
  const assets = Array.isArray(prepare.assets) ? prepare.assets : [];
  const cancel = !PREPARE_TERMINAL.has(prepare.state)
    ? el("button", {
        className: "wiz-btn wiz-btn--ghost",
        text: tt("pkg_cancel", "Cancel"),
        attrs: { type: "button", "data-prepare-cancel": prepare.id },
      })
    : null;
  container.replaceChildren(
    el("div", {
      className: "package-status package-status--prepare",
      children: [
        el("h3", { text: packageLabel || "" }),
        line(tt("pkg_prepare", "Prepare"), prepare.state),
        line(tt("pkg_progress", "Progress"), `${prepare.progress_percent || 0}%`),
        line(
          tt("pkg_assets", "Assets"),
          assets.map((asset) => `${asset.logical_path}: ${asset.state}`).join(" · ") ||
            tt("pkg_none", "none"),
        ),
        prepare.error_summary
          ? el("p", { className: "form-error", text: prepare.error_summary })
          : null,
        cancel,
      ],
    }),
  );
}

export function renderPreflight(container, result, packageLabel = "") {
  if (!container || !result) return;
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  const errors = Array.isArray(result.errors) ? result.errors : [];
  const requiresEgressAck = warnings.some((warning) => /egress/i.test(String(warning.code || "")));
  const warningList = warnings.length
    ? warnings.map((warning, index) =>
        el("label", {
          className: "package-warning",
          children: [
            el("input", {
              attrs: { type: "checkbox", required: "required", "data-warning-index": index },
            }),
            el("span", {
              children: [
                el("strong", { text: warning.code || tt("pkg_warning", "warning") }),
                el("span", {
                  text: ` · ${warning.message || tt("pkg_review_warning", "Review this warning.")}`,
                }),
              ],
            }),
          ],
        }),
      )
    : [el("p", { className: "text-muted small", text: tt("pkg_no_warnings", "No warnings.") })];
  const errorList = errors.length
    ? errors.map((item) =>
        el("li", { text: `${item.code || tt("pkg_error", "error")}: ${item.message || ""}` }),
      )
    : [];
  const launch = el("button", {
    className: "wiz-btn wiz-btn--primary",
    text: tt("pkg_launch", "Launch package"),
    attrs: { type: "button", disabled: !result.ready || !warnings.length },
  });
  const egressAck = requiresEgressAck
    ? el("label", {
        className: "package-warning package-warning--egress",
        children: [
          el("input", {
            attrs: { type: "checkbox", required: "required", "data-egress-ack": "true" },
          }),
          el("span", {
            text: tt("pkg_egress_ack", "I acknowledge that this lab may reach external networks."),
          }),
        ],
      })
    : null;
  container.replaceChildren(
    el("section", {
      className: "package-preflight",
      children: [
        el("div", {
          className: "package-preflight__head",
          children: [
            el("h3", { text: `${packageLabel} · ${tt("pkg_preflight", "Preflight")}` }),
            el("span", {
              className: `wiz-badge ${badgeVariant(result.ready ? "ready" : "blocked")}`,
              text: result.ready ? tt("pkg_ready", "ready") : tt("pkg_blocked", "blocked"),
            }),
          ],
        }),
        el("div", {
          className: "package-status__grid",
          children: [
            line(
              tt("pkg_retained_storage", "Retained storage"),
              formatBytes(result.retained_disk_bytes),
            ),
            line(tt("pkg_launch_storage", "Launch storage"), formatBytes(result.launch_disk_bytes)),
            line(
              tt("pkg_required_storage", "Required storage"),
              formatBytes(result.required_disk_bytes),
            ),
            line(tt("pkg_host", "Host"), result.host?.architecture || tt("pkg_unknown", "unknown")),
          ],
        }),
        errors.length ? el("ul", { className: "package-error-list", children: errorList }) : null,
        el("details", {
          children: [
            el("summary", { text: tt("pkg_exact_bytes", "Exact bytes") }),
            el("pre", {
              className: "log-pre",
              text: JSON.stringify(
                {
                  retained: result.retained_disk_bytes || 0,
                  launch: result.launch_disk_bytes || 0,
                  required: result.required_disk_bytes || 0,
                },
                null,
                2,
              ),
            }),
          ],
        }),
        el("div", { className: "package-warning-list", children: warningList }),
        egressAck,
        el("div", { className: "package-preflight__actions", children: [launch] }),
      ],
    }),
  );
  const allAcknowledged = () => {
    const warningUnchecked = [...container.querySelectorAll("[data-warning-index]")].some(
      (item) => !item.checked,
    );
    const egressUnchecked = Boolean(
      container.querySelector("[data-egress-ack]") &&
      !container.querySelector("[data-egress-ack]").checked,
    );
    return !warningUnchecked && !egressUnchecked;
  };
  launch.disabled = !result.ready || !allAcknowledged();
  container.querySelectorAll("[data-warning-index], [data-egress-ack]").forEach((checkbox) =>
    checkbox.addEventListener("change", () => {
      launch.disabled = !result.ready || !allAcknowledged();
    }),
  );
  launch.dataset.warningToken = result.warning_token || "";
}

const operationPanels = new WeakMap();
export function renderOperation(container, operation, packageLabel = operation?.package_record_id) {
  if (!container || !operation) return;
  const signature = JSON.stringify([operation, packageLabel]);
  if (operationPanels.get(container) === signature) return;
  operationPanels.set(container, signature);
  const actions = [];
  if (operation.state === "ready" && operation.session_id)
    actions.push(
      el("button", {
        className: "wiz-btn wiz-btn--primary",
        text: tt("pkg_open_lab", "Open lab in Playground"),
        attrs: { type: "button", "data-instance-action": "open", "data-instance-id": operation.id },
      }),
    );
  if (operation.state === "ready")
    actions.push(
      el("button", {
        className: "wiz-btn wiz-btn--ghost",
        text: tt("pkg_reset", "Reset"),
        attrs: {
          type: "button",
          "data-instance-action": "reset",
          "data-instance-id": operation.id,
        },
      }),
    );
  if (!INSTANCE_TERMINAL.has(operation.state) && operation.state !== INSTANCE_CLEANUP_INCOMPLETE)
    actions.push(
      el("button", {
        className: "wiz-btn wiz-btn--danger",
        text: tt("pkg_teardown", "Teardown"),
        attrs: {
          type: "button",
          "data-instance-action": "teardown",
          "data-instance-id": operation.id,
        },
      }),
    );
  container.replaceChildren(
    el("div", {
      className: "package-operation",
      children: [
        el("h3", { text: packageLabel || "" }),
        line(tt("pkg_operation", "Operation"), operation.id),
        line(tt("pkg_state", "State"), operation.state),
        operation.error_summary
          ? el("p", { className: "form-error", text: operation.error_summary })
          : null,
        operation.state === INSTANCE_CLEANUP_INCOMPLETE
          ? el("p", {
              className: "form-error",
              text: tt(
                "pkg_cleanup_incomplete",
                "Cleanup incomplete. Resources may still be reserved.",
              ),
            })
          : null,
        operation.state === INSTANCE_CLEANUP_INCOMPLETE
          ? el("a", { attrs: { href: "/logs" }, text: tt("nav_logs", "Logs") })
          : null,
        el("div", { className: "package-card__actions", children: actions }),
      ],
    }),
  );
}

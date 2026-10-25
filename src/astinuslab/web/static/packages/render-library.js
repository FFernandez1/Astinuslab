import { el } from "../lib/dom.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;

function text(value, fallback = tt("pkg_missing_value", "—")) {
  return value == null || value === "" ? fallback : String(value);
}

function button(label, action, recordId, extra = {}) {
  return el("button", {
    className: `wiz-btn wiz-btn--${extra.variant || "ghost"}`,
    text: label,
    attrs: {
      type: "button",
      "data-package-action": action,
      "data-record-id": recordId,
      disabled: extra.disabled,
    },
  });
}

const BADGE_VARIANTS = new Map([
  ["trusted", "wiz-badge--success"],
  ["valid", "wiz-badge--success"],
  ["ready", "wiz-badge--success"],
  ["verified", "wiz-badge--success"],
  ["unknown", "wiz-badge--warning"],
  ["untrusted", "wiz-badge--warning"],
  ["invalid", "wiz-badge--danger"],
  ["blocked", "wiz-badge--danger"],
  ["failed", "wiz-badge--danger"],
]);

/**
 * Map a package signature status or a preflight/verification outcome onto a
 * shared badge variant class. Anything unrecognised, including an in-flight
 * state, is neutral: a running verification is not a warning.
 * @param {string|null|undefined} status
 * @returns {string}
 */
export function badgeVariant(status) {
  return BADGE_VARIANTS.get(String(status || "").toLowerCase()) || "wiz-badge--neutral";
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  const power = n > 0 ? Math.min(4, Math.floor(Math.log(n) / Math.log(1024))) : 0;
  return `${(n / 1024 ** power).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${["B", "KiB", "MiB", "GiB", "TiB"][power]}`;
}

export function packageActions(pkg) {
  return el("div", {
    className: "package-card__actions",
    children: [
      button(tt("pkg_preflight", "Preflight"), "preflight", pkg.id, { variant: "primary" }),
      button(tt("pkg_prepare", "Prepare"), "prepare", pkg.id),
      el("a", {
        className: "wiz-btn wiz-btn--ghost",
        text: tt("pkg_download", "Download"),
        attrs: {
          href: `/api/v1/lab-packages/${encodeURIComponent(pkg.id)}/download`,
          download: "",
        },
      }),
      button(tt("pkg_delete", "Delete"), "delete", pkg.id, { variant: "danger" }),
    ],
  });
}

function packageCard(pkg) {
  const trust = String(pkg.signature_status || "unknown").toLowerCase();
  const identity = `${text(pkg.package_id)}@${text(pkg.version)}`;
  const metadata = el("dl", {
    className: "package-card__metadata",
    children: [
      el("div", {
        children: [
          el("dt", { text: tt("pkg_creator", "Creator") }),
          el("dd", { text: text(pkg.creator) }),
        ],
      }),
      el("div", {
        children: [
          el("dt", { text: tt("pkg_license", "License") }),
          el("dd", { text: text(pkg.license) }),
        ],
      }),
      el("div", {
        children: [
          el("dt", { text: tt("pkg_mode", "Mode") }),
          el("dd", { text: text(pkg.reproducibility) }),
        ],
      }),
      el("div", {
        children: [
          el("dt", { text: tt("pkg_archive", "Archive") }),
          el("dd", {
            text: formatBytes(pkg.archive_size_bytes),
            attrs: { title: `${pkg.archive_size_bytes || 0} ${tt("pkg_bytes", "bytes")}` },
          }),
        ],
      }),
    ],
  });
  const actions = el("div", {
    className: "package-card__actions",
    children: [
      button(tt("pkg_open_package", "Open package"), "open", pkg.id, { variant: "primary" }),
    ],
  });
  return el("article", {
    className: "package-card",
    attrs: { "data-package-id": pkg.id },
    children: [
      el("div", {
        className: "package-card__head",
        children: [
          el("div", {
            children: [
              el("h3", { className: "package-card__title", text: identity }),
              el("p", { className: "package-card__description", text: text(pkg.title) }),
            ],
          }),
          el("span", {
            className: `wiz-badge ${badgeVariant(trust)}`,
            text: text(pkg.signature_status, tt("pkg_unknown", "unknown")),
          }),
        ],
      }),
      el("p", {
        className: "package-card__summary",
        text: text(pkg.description, tt("pkg_no_description", "No description provided.")),
      }),
      metadata,
      actions,
    ],
  });
}

export function renderPackageList(container, packages) {
  if (!container) return;
  if (!Array.isArray(packages) || packages.length === 0) {
    container.replaceChildren(
      el("p", {
        className: "empty-state",
        text: tt("pkg_no_packages", "No imported packages yet."),
      }),
    );
    return;
  }
  container.replaceChildren(...packages.map(packageCard));
}

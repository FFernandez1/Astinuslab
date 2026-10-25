import { el } from "../lib/dom.js";
import { renderMarkdown } from "./render-markdown.js";
import { packageActions, formatBytes, badgeVariant } from "./render-library.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;

function value(value, fallback = tt("pkg_missing_value", "—")) {
  return value == null || value === "" ? fallback : String(value);
}

function detailsRow(label, content) {
  return el("div", {
    className: "package-inspection__row",
    children: [el("dt", { text: label }), el("dd", { children: [content] })],
  });
}

function safeRunbook(markdown) {
  const rendered = el("div", { className: "package-markdown" });
  // renderMarkdown escapes raw HTML and admits only HTTP(S) links. The sole
  // innerHTML assignment in this page consumes that trusted renderer output.
  rendered.innerHTML = renderMarkdown(markdown);
  return rendered;
}

export function renderPackageExperience(container, record) {
  if (!container) return;
  const list = (values) => el("ul", { children: (values || []).map((text) => el("li", { text })) });
  const resources = record.resources?.minimum || {};
  container.replaceChildren(
    el("h3", {
      attrs: { tabindex: "-1" },
      text: `${record.title} · ${record.package_id}@${record.version}`,
    }),
    el("p", {
      text: `${tt("pkg_mode", "Mode")}: ${record.reproducibility || "—"} · ${tt("pkg_signature", "Signature")}: ${record.signature_status || "unknown"}`,
    }),
    packageActions(record),
    el("h4", { text: tt("pkg_runbook", "Runbook") }),
    safeRunbook(record.runbook || ""),
    el("h4", { text: tt("pkg_objectives", "Objectives") }),
    list(record.experience?.objectives),
    el("h4", { text: tt("pkg_hints", "Hints") }),
    list(record.experience?.hints),
    el("p", {
      text: `${resources.cpu_threads || 0} CPU · ${resources.memory_mb || 0} MiB RAM · ${formatBytes(resources.free_disk_bytes)} ${tt("pkg_required_storage", "Required storage")}`,
    }),
    el("details", {
      children: [
        el("summary", { text: tt("pkg_declared_details", "Declared resources and checkpoints") }),
        el("pre", {
          className: "log-pre",
          text: JSON.stringify(
            {
              resources: record.resources,
              checkpoints: record.experience?.checkpoints || [],
              supporting_files: record.supporting_files || [],
              archive_sha256: record.archive_sha256,
              manifest_sha256: record.manifest_sha256,
            },
            null,
            2,
          ),
        }),
      ],
    }),
    el("a", {
      attrs: { href: "#library-heading" },
      text: tt("pkg_back_library", "Back to local library"),
    }),
  );
}

export function renderInspection(container, inspection, onCommit) {
  if (!container) return;
  if (!inspection) {
    container.replaceChildren();
    return;
  }
  const assets = Array.isArray(inspection.assets) ? inspection.assets : [];
  const assetList = el("ul", {
    className: "package-asset-list",
    children: assets.length
      ? assets.map((asset) =>
          el("li", {
            children: [
              el("strong", { text: value(asset.path) }),
              el("span", {
                text: ` · ${value(asset.embedded ? tt("pkg_embedded", "embedded") : tt("pkg_external", "external"))} · ${value(asset.size_bytes)} ${tt("pkg_bytes", "bytes")}`,
              }),
            ],
          }),
        )
      : [el("li", { text: tt("pkg_no_boot_assets", "No boot-layer assets declared.") })],
  });
  const commit = el("button", {
    className: "wiz-btn wiz-btn--primary",
    text: tt("pkg_imported_action", "Import inspected package"),
    attrs: {
      type: "button",
      "data-commit-inspection": inspection.inspection_id || "",
      disabled: !inspection.inspection_id,
    },
  });
  if (typeof onCommit === "function")
    commit.addEventListener("click", () => onCommit(inspection.inspection_id));
  const resources = el("pre", {
    className: "log-pre",
    text: JSON.stringify(inspection.resources || {}, null, 2),
  });
  container.replaceChildren(
    el("div", {
      className: "package-inspection",
      children: [
        el("div", {
          className: "package-inspection__head",
          children: [
            el("h3", { text: `${value(inspection.package_id)}@${value(inspection.version)}` }),
            el("span", {
              className: `wiz-badge ${badgeVariant(inspection.signature_status)}`,
              text: value(inspection.signature_status, tt("pkg_unknown", "unknown")),
            }),
          ],
        }),
        el("p", { className: "package-card__description", text: value(inspection.title) }),
        el("dl", {
          className: "package-inspection__metadata",
          children: [
            detailsRow(
              tt("pkg_creator", "Creator"),
              el("span", { text: value(inspection.creator) }),
            ),
            detailsRow(
              tt("pkg_license", "License"),
              el("span", { text: value(inspection.license) }),
            ),
            detailsRow(
              tt("pkg_reproducibility", "Reproducibility"),
              el("span", { text: value(inspection.reproducibility) }),
            ),
            detailsRow(
              tt("pkg_required_capabilities", "Required capabilities"),
              el("span", {
                text: (inspection.required_capabilities || []).join(", ") || tt("pkg_none", "none"),
              }),
            ),
          ],
        }),
        el("h4", { text: tt("pkg_assets", "Assets") }),
        assetList,
        el("h4", { text: tt("pkg_host_resources", "Host resources") }),
        resources,
        el("h4", { text: tt("pkg_runbook", "Runbook") }),
        safeRunbook(inspection.runbook),
        el("div", { className: "package-inspection__footer", children: [commit] }),
      ],
    }),
  );
}

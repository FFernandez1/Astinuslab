// Create-VM modal flow (Manifest HUD): target wizard (Basics/Workload/Resources/
// Review) plus the minimal attacker / network-node creators. Reads/writes
// ui.createRole + ui.createWizard. This module restyles around the existing state
// + AlDialog contract — it does not rewire the create payload or validation.
import { el } from "./ui.js";
import { getState, setState, subscribe } from "./state.js";
import { getCatalogWorkloads, createVm, refreshSession } from "./api.js";
import {
  buildVerticalRail,
  buildImageGrid,
  buildNetworkRows,
  buildHudSlider,
} from "./render-modal.js";
import { generatedPlaygroundName } from "./name-generator.js";
import { networkSignature } from "./wizard.sig.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;
const STEPS = [
  tt("pg_wiz_basics", "Basics"),
  "Workload",
  tt("pg_wiz_resources", "Resources"),
  tt("pg_wiz_review", "Review"),
];
let retryCatalog = () => {};
// Mirrors backend DEFAULT_PLAYGROUND_NETWORK_CIDR for the auto-created default network.
const DEFAULT_NETWORK_CIDR = "10.42.1.0/24";
// Preset ladders for the resource sliders. vCPU/disk are stored as-is; memory is
// shown in GB but kept in MB everywhere in state/payload.
const VCPU_OPTIONS = [1, 2, 4, 8];
const RAM_GB_OPTIONS = [2, 4, 8, 16];
const DISK_PRESETS = [20, 40, 80, 160];

const STEP_HINT = {
  0: tt("pg_wiz_hint_basics", "name the target and pick its base image"),
  1: tt("pg_wiz_hint_workload", "drop a vulnerable service onto the target (optional)"),
  2: tt("pg_wiz_hint_resources", "attach networks and size the VM"),
  3: tt("pg_wiz_hint_review", "confirm and add to the live session"),
};

// "<role>:<step>" of the last full render, so renderModal can tell a genuine
// step/role change (animate) from a same-step re-render (don't — it flickers).
let lastPaintKey = null;

function roleLabel(role) {
  if (role === "network") return tt("pg_role_network_node", "Network Node");
  return role === "attacker"
    ? tt("pg_wiz_role_attacker", "attacker")
    : tt("pg_wiz_role_target", "target");
}
function createText(role) {
  if (role === "network") return tt("pg_wiz_create_network_node", "Create Network Node");
  return role === "attacker"
    ? tt("pg_wiz_create_attacker", "Create attacker")
    : tt("pg_wiz_create_target", "Create target");
}
function roleCrumb(role) {
  return roleLabel(role);
}
function roleHint(role) {
  if (role === "network")
    return tt("pg_wiz_hint_node", "a lightweight VM that routes or inspects lab traffic");
  if (role === "attacker")
    return tt("pg_wiz_hint_attacker", "an attacker box you'll drive against the targets");
  return STEP_HINT[0];
}
function singleTickLabel(role) {
  return role === "network" ? tt("pg_role_network_node", "Network Node") : "Attacker";
}
function $(id) {
  return document.getElementById(id);
}
function setErr(msg) {
  const e = $("spawn-error");
  if (e) {
    e.hidden = !msg;
    e.textContent = msg || "";
  }
}
function setWarning(msg) {
  const e = $("create-vm-warning");
  if (e) {
    e.hidden = !msg;
    e.textContent = msg || "";
  }
}
function clearModal() {
  setState((prev) => ({ ...prev, ui: { ...prev.ui, createRole: null, createWizard: null } }));
  setErr("");
  setWarning("");
  lastPaintKey = null;
}
function close() {
  const modal = $("create-vm-modal");
  if (modal && window.AlDialog?.current === modal) window.AlDialog.close();
  else {
    clearModal();
    if (modal) modal.hidden = true;
  }
}
function initWizard(role, st) {
  const images = st.catalog?.[role] || [];
  const isNetwork = role === "network";
  const isAttacker = role === "attacker";
  const defaults = {
    role,
    step: 0,
    label: "",
    generatedLabel: generatedPlaygroundName(role),
    imageId: images[0]?.id || "",
    workloadId: "",
    workloads: [],
    query: "",
    networkIds: [],
    newNetworks: [],
    diskGb: isNetwork ? 4 : 20,
    vcpus: isNetwork ? 1 : isAttacker ? 2 : 2,
    memoryMb: isNetwork ? 512 : isAttacker ? undefined : 4096,
  };
  setState((prev) => ({ ...prev, ui: { ...prev.ui, createWizard: defaults } }));
  if (role === "target" && defaults.imageId) loadWorkloads(defaults.imageId);
}
async function loadWorkloads(imageId) {
  try {
    const ws = await getCatalogWorkloads(imageId);
    const st = getState();
    const w = st.ui.createWizard;
    if (!w || w.imageId !== imageId) return; // stale: image changed before fetch resolved
    // Mutate in place (no setState) so loading workloads after an image pick does
    // not re-render — and replay the entry animation — on the Basics panel.
    w.workloads = ws;
    const grid = $("create-vm-modal")?.querySelector(".pg-hud-wlgrid");
    if (grid) grid.replaceChildren(...buildWorkloadTiles(w));
    refreshManifest();
  } catch (err) {
    setErr(err.message);
  }
}
function setW(patch) {
  setState((prev) =>
    prev.ui.createWizard
      ? { ...prev, ui: { ...prev.ui, createWizard: { ...prev.ui.createWizard, ...patch } } }
      : prev,
  );
}
function memoryDisplay(mb) {
  return mb >= 1024 ? `${mb / 1024} GB` : `${mb} MB`;
}
function setModalChrome(role, w) {
  const modal = $("create-vm-modal");
  const title = $("create-vm-title");
  const eyebrow = $("create-vm-eyebrow");
  const counter = $("create-vm-step-counter");
  const crumb = $("create-vm-crumb-role");
  const hint = $("create-vm-hint");
  if (modal) modal.dataset.role = role;
  if (eyebrow) eyebrow.textContent = tt("pg_eyebrow_new_vm", "PLAYGROUND · NEW VM");
  if (title) title.textContent = createText(role);
  if (crumb) crumb.textContent = roleCrumb(role);
  if (counter) {
    const isTarget = role === "target";
    counter.hidden = !isTarget;
    counter.textContent = isTarget
      ? tt("pg_wiz_step_count", "step {step} / {total}")
          .replace("{step}", (w?.step || 0) + 1)
          .replace("{total}", STEPS.length)
      : "";
  }
  if (hint) hint.textContent = role === "target" ? STEP_HINT[w?.step || 0] || "" : roleHint(role);
}

// ── Basics ───────────────────────────────────────────────────────────────
function buildLabelInput({ value, generatedLabel, placeholder, role }) {
  return el("input", {
    className: "pg-net-input",
    attrs: {
      type: "text",
      name: "label",
      placeholder: placeholder || generatedLabel,
      value,
      pattern: "[a-zA-Z0-9_-]+",
      autocomplete: "off",
      "aria-label": tt("pg_wiz_role_label", "{role} label").replace("{role}", roleLabel(role)),
    },
    onInput: (e) => {
      const current = getState().ui.createWizard;
      if (current) {
        current.label = e.currentTarget.value.trim();
        refreshManifest();
      }
    },
  });
}
function nameSection(role, w) {
  const input = buildLabelInput({
    value: w.label,
    generatedLabel: w.generatedLabel,
    placeholder: w.generatedLabel,
    role,
  });
  return el("div", {
    children: [
      el("p", { className: "pg-hud-lbl", text: tt("pg_wiz_label", "Label") }),
      el("div", {
        className: "pg-hud-field-meta",
        children: [
          el("span", { text: tt("pg_wiz_pattern", "pattern [a-z0-9_-]") }),
          el("span", {
            text: tt("pg_wiz_suggested", "suggested · {name}").replace("{name}", w.generatedLabel),
          }),
        ],
      }),
      el("div", {
        className: "pg-hud-row",
        children: [
          input,
          el("button", {
            className: "wiz-btn wiz-btn--ghost wiz-btn--xs",
            attrs: { type: "button", title: tt("pg_wiz_regenerate", "Regenerate name") },
            text: tt("pg_wiz_new_name", "⟳ name"),
            onClick: () => setW({ generatedLabel: generatedPlaygroundName(role), label: "" }),
          }),
        ],
      }),
    ],
  });
}
function imageSection(role, w, st, { reloadWorkloads }) {
  const images = st.catalog?.[role] || [];
  const status = st.catalogStatus?.[role] || (images.length ? "loaded" : "loaded-empty");
  const roleName =
    role === "network"
      ? tt("pg_role_network_node", "Network Node")
      : role === "attacker"
        ? "Attacker"
        : "Target";
  const message =
    status === "loading"
      ? tt("pg_images_loading", "Loading {role} images…")
      : status === "failed"
        ? tt("pg_images_failed", "Could not load {role} images.")
        : tt("pg_images_empty", "No {role} images available.");
  const notice =
    status === "loaded"
      ? null
      : el("div", {
          className: "pg-v2-infobox",
          children: [
            el("p", { attrs: { role: "status" }, text: message.replace("{role}", roleName) }),
            status === "failed"
              ? el("button", {
                  className: "wiz-btn wiz-btn--ghost",
                  attrs: { type: "button" },
                  text: tt("pg_images_retry", "Retry loading images"),
                  onClick: () => retryCatalog(role),
                })
              : null,
            status === "loaded-empty"
              ? el("p", {
                  children: [
                    el("a", {
                      attrs: { href: "/images" },
                      text: tt("pg_manage_images", "Manage images"),
                    }),
                    el("span", { text: " · " }),
                    el("a", {
                      attrs: {
                        href: "/help/user/PREBAKED_IMAGES.md",
                        target: "_blank",
                        rel: "noopener",
                      },
                      text: tt("pg_image_guide", "Image guide"),
                    }),
                  ],
                })
              : null,
          ].filter(Boolean),
        });
  return el("div", {
    className: "pg-hud-gap",
    children: [
      el("p", {
        className: "pg-hud-lbl",
        children: [
          el("span", {
            attrs: { id: "create-vm-image-label" },
            text: tt("pg_role_image", "{role} OS image").replace("{role}", roleName),
          }),
        ],
      }),
      notice,
      buildImageGrid({
        images,
        value: w.imageId,
        labelledBy: "create-vm-image-label",
        onChange: (v) => {
          // buildImageGrid already moved the highlight; mutate state in place and
          // refresh the manifest, avoiding a full re-render (the blink).
          w.imageId = v;
          if (reloadWorkloads) {
            w.workloadId = "";
            loadWorkloads(v);
          }
          refreshManifest();
        },
      }),
      el("p", {
        className: "pg-hud-help",
        text: tt("pg_wiz_image_help", "Base image AstinusLab boots into the lab network."),
      }),
    ].filter(Boolean),
  });
}
function renderBasics(role, w, st) {
  return el("section", {
    className: "pg-hud-panel",
    children: [nameSection(role, w), imageSection(role, w, st, { reloadWorkloads: true })],
  });
}

// ── Workload ─────────────────────────────────────────────────────────────
function workloadHaystack(wl) {
  return [
    wl.id,
    wl.display_name,
    ...(wl.tags || []),
    ...(wl.service_labels || []),
    ...(wl.cve_ids || []),
  ].join(" ");
}
function workloadDesc(wl) {
  return (
    wl.cve_context ||
    (wl.tags || []).join(", ") ||
    (wl.service_labels || []).join(", ") ||
    tt("pg_wiz_workload_fallback", "Vulnerable workload.")
  );
}
function buildWorkloadTile({
  value,
  name,
  desc,
  services,
  flaw,
  statusIdle,
  active,
  disabled,
  searchText,
}) {
  // Move the "selected" highlight across sibling cards in place, then
  // refreshManifest() — no full re-render, so picking a workload never blinks
  // (and the scroll position in a long list is preserved).
  const select = (cardEl) => {
    if (disabled) return;
    const w = getState().ui.createWizard;
    if (!w) return;
    w.workloadId = value;
    cardEl
      .closest(".pg-hud-wlgrid")
      ?.querySelectorAll(".pg-hud-wlcard")
      .forEach((c) => {
        const on = c.dataset.value === value;
        c.dataset.on = String(on);
        c.setAttribute("aria-pressed", String(on));
        const stEl = c.querySelector(".pg-hud-wlcard__st");
        if (stEl)
          stEl.textContent = on ? tt("pg_wiz_selected", "selected") : c.dataset.statusidle || "";
      });
    refreshManifest();
  };
  return el("div", {
    className: "pg-v2-workload-card pg-hud-wlcard",
    dataset: {
      on: String(!!active),
      disabled: String(!!disabled),
      value,
      search: searchText || "",
      statusidle: statusIdle || "",
    },
    attrs: {
      role: "button",
      tabindex: disabled ? "-1" : "0",
      "aria-pressed": String(!!active),
      "aria-disabled": String(!!disabled),
    },
    children: [
      el("div", {
        className: "pg-hud-wlcard__top",
        children: [
          el("span", { className: "pg-hud-wlcard__n", text: name }),
          flaw ? el("span", { className: "pg-hud-flaw", text: flaw }) : null,
        ].filter(Boolean),
      }),
      desc ? el("div", { className: "pg-hud-wlcard__desc", text: desc }) : null,
      el("div", {
        className: "pg-hud-wlcard__foot",
        children: [
          ...(services || []).map((s) => el("span", { className: "pg-hud-chip", text: s })),
          el("span", {
            className: "pg-hud-wlcard__st",
            text: active ? tt("pg_wiz_selected", "selected") : statusIdle,
          }),
        ],
      }),
    ].filter(Boolean),
    onClick: (e) => select(e.currentTarget),
    onKeyDown: (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        select(e.currentTarget);
      }
    },
  });
}
// Workload tiles, factored out so loadWorkloads() can refresh the grid in place
// (.pg-hud-wlgrid) without re-rendering the whole step.
function buildWorkloadTiles(w) {
  const q = (w.query || "").toLowerCase();
  const list = (w.workloads || []).filter((wl) => workloadHaystack(wl).toLowerCase().includes(q));
  return [
    buildWorkloadTile({
      value: "",
      name: tt("pg_wiz_no_workload", "No workload"),
      desc: tt("pg_wiz_bare_target", "Plain target VM — boot the image bare."),
      active: !w.workloadId,
      statusIdle: tt("pg_wiz_available", "available"),
    }),
    ...list.map((wl) => {
      const disabled = wl.compatibility_status === "incompatible";
      const active = w.workloadId === wl.id;
      const statusIdle = disabled
        ? `${tt("pg_wiz_incompatible", "incompatible")}${wl.compatibility_reason ? ` · ${wl.compatibility_reason}` : ""}`
        : wl.compatibility_status && wl.compatibility_status !== "compatible"
          ? wl.compatibility_status
          : tt("pg_wiz_compatible", "compatible");
      return buildWorkloadTile({
        value: wl.id,
        name: wl.display_name || wl.id,
        desc: workloadDesc(wl),
        services: (wl.service_labels || []).slice(0, 4),
        flaw: (wl.cve_ids || []).length ? "CVE" : null,
        active,
        disabled,
        statusIdle,
        searchText: workloadHaystack(wl),
      });
    }),
  ];
}
function workloadPreparationNotice() {
  return el("aside", {
    className: "pg-v2-infobox",
    children: [
      el("p", { text: window.t("pg_workload_prerequisite") }),
      el("a", {
        text: window.t("pg_prepare_workloads"),
        attrs: { href: "/help/user/PREBAKED_IMAGES.md", target: "_blank", rel: "noopener" },
      }),
    ],
  });
}
function renderWorkload(w, st) {
  const image = (st.catalog?.target || []).find((i) => i.id === w.imageId);
  const search = el("input", {
    className: "workload-search-input",
    attrs: {
      type: "text",
      placeholder: tt("pg_wiz_search_workloads", "Search workloads by service, CVE or name"),
      "aria-label": tt("pg_wiz_search_workloads", "Search workloads by service, CVE or name"),
      value: w.query || "",
    },
    onInput: (e) => {
      const query = e.currentTarget.value.toLowerCase();
      w.query = e.currentTarget.value;
      e.currentTarget
        .closest(".pg-v2-workload-picker")
        ?.querySelectorAll(".pg-v2-workload-card")
        .forEach((card) => {
          const haystack = (card.dataset.search || card.textContent).toLowerCase();
          card.hidden = card.dataset.value !== "" && !haystack.includes(query);
        });
    },
  });
  const tiles = buildWorkloadTiles(w);
  return el("section", {
    className: "pg-hud-panel",
    children: [
      el("div", {
        className: "pg-v2-workload-picker",
        children: [
          el("div", { className: "pg-hud-wlhead", children: [search] }),
          el("div", { className: "pg-v2-workload-grid pg-hud-wlgrid", children: tiles }),
          workloadPreparationNotice(),
          el("p", {
            className: "pg-hud-help",
            text: tt(
              "pg_wiz_compatible_help",
              "Workloads compatible with {image}. Incompatible builds are shown but locked.",
            ).replace("{image}", image ? image.label || image.id : tt("pg_wiz_image", "image")),
          }),
        ],
      }),
    ],
  });
}

// ── Networks (shared by all roles) ─────────────────────────────────────────
function compactNetworkOptions(st, w) {
  const existing = (st.session?.networks || []).map((n) => ({
    value: n.id,
    name: n.name,
    cidr: n.subnet_cidr || "",
    mode: n.isolated ? "isolated" : "shared",
    state: n.state || "unknown",
    disabled: n.state !== "active",
  }));
  const drafts = (w.newNetworks || []).map((n) => ({
    value: n.id,
    name: n.name,
    cidr: n.subnet_cidr || "",
    mode: "draft",
    state: "new",
    disabled: false,
  }));
  return [...existing, ...drafts];
}
function draftNetwork(w, id) {
  return (w.newNetworks || []).find((n) => n.id === id);
}
function sessionNetwork(st, id) {
  return (st.session?.networks || []).find((n) => n.id === id);
}
function networkLabel(st, w, id) {
  return draftNetwork(w, id)?.name || sessionNetwork(st, id)?.name || id;
}
function networkCidr(st, w, id) {
  return draftNetwork(w, id)?.subnet_cidr || sessionNetwork(st, id)?.subnet_cidr || "";
}
function networkReviewText(st, w, id) {
  const draft = draftNetwork(w, id);
  const parts = [networkLabel(st, w, id), networkCidr(st, w, id)].filter(Boolean);
  if (draft?.cidr_auto) parts.push(tt("pg_wiz_auto_assigned", "auto-assigned"));
  return parts.join(" · ");
}
function autoAssignedNetworks(st, w) {
  if (!(w.networkIds || []).length) return [{ name: "default", cidr: DEFAULT_NETWORK_CIDR }];
  return (w.networkIds || [])
    .map((id) => draftNetwork(w, id))
    .filter((n) => n?.cidr_auto)
    .map((n) => ({ name: n.name, cidr: n.subnet_cidr }));
}
function renderAutoAssignedInfoBox(st, w) {
  const networks = autoAssignedNetworks(st, w);
  if (!networks.length) return null;
  return el("div", {
    className: "pg-v2-infobox",
    children: networks.map((n, i) =>
      el("div", {
        className: "pg-v2-infobox-line",
        text: `${i === 0 ? tt("pg_wiz_auto_assigned_prefix", "Auto-assigned: ") : ""}${n.name} → ${n.cidr}`,
      }),
    ),
  });
}
function networkField(st, w) {
  const countLbl = el("p", {
    className: "pg-hud-lbl",
    text: tt("pg_wiz_network_count", "Networks · {count} selected").replace(
      "{count}",
      w.networkIds.length,
    ),
  });
  let autoInfo = renderAutoAssignedInfoBox(st, w);
  const field = el("div", {
    className: "pg-hud-netfield pg-v2-field-networks",
    children: [
      el("div", {
        className: "pg-hud-nethead",
        children: [
          countLbl,
          el("button", {
            className: "pg-hud-netadd",
            attrs: { type: "button", id: "create-vm-add-network" },
            text: tt("pg_wiz_new", "+ New"),
            onClick: () =>
              setState((prev) => ({ ...prev, ui: { ...prev.ui, createNetworkOpen: true } })),
          }),
        ],
      }),
      buildNetworkRows({
        options: compactNetworkOptions(st, w),
        selected: w.networkIds || [],
        // buildNetworkRows flips the row; here we update the derived UI (count +
        // auto-assigned box + manifest) in place rather than full re-rendering.
        onToggle: (v) => {
          w.networkIds = w.networkIds.includes(v)
            ? w.networkIds.filter((x) => x !== v)
            : [...w.networkIds, v];
          countLbl.textContent = tt("pg_wiz_network_count", "Networks · {count} selected").replace(
            "{count}",
            w.networkIds.length,
          );
          const next = renderAutoAssignedInfoBox(st, w);
          if (autoInfo && next) autoInfo.replaceWith(next);
          else if (autoInfo && !next) autoInfo.remove();
          else if (!autoInfo && next) field.appendChild(next);
          autoInfo = next;
          refreshManifest();
        },
      }),
      autoInfo,
      el("p", {
        className: "pg-hud-help",
        text: tt(
          "pg_network_egress_notice",
          "Egress-enabled networks allow outbound traffic. Isolated networks do not provide internet access.",
        ),
      }),
    ].filter(Boolean),
  });
  return field;
}

// ── Resources (target only) ────────────────────────────────────────────────
function vmResources(st, w) {
  const image = (st.catalog?.target || []).find((i) => i.id === w.imageId);
  return el("div", {
    className: "pg-hud-vm",
    children: [
      el("div", {
        className: "pg-hud-vmcol",
        children: [
          el("div", {
            className: "pg-hud-vmcol__h",
            children: [
              el("span", { text: roleLabel("target") }),
              el("span", {
                className: "pg-hud-vmcol__sub",
                text: image ? image.label || image.id : "",
              }),
            ],
          }),
          buildHudSlider({
            label: "vcpus",
            presets: VCPU_OPTIONS,
            value: w.vcpus || 2,
            unit: tt("pg_wiz_cores", "cores"),
            onChange: (v) => {
              w.vcpus = v;
              refreshManifest();
            },
          }),
          buildHudSlider({
            label: tt("pg_wiz_field_memory", "memory"),
            presets: RAM_GB_OPTIONS,
            value: Math.round((w.memoryMb || 4096) / 1024),
            unit: "GB",
            onChange: (gb) => {
              w.memoryMb = gb * 1024;
              refreshManifest();
            },
          }),
          buildHudSlider({
            label: tt("pg_wiz_field_disk", "disk"),
            presets: DISK_PRESETS,
            value: w.diskGb || 20,
            unit: "GB",
            onChange: (gb) => {
              w.diskGb = gb;
              refreshManifest();
            },
          }),
        ],
      }),
    ],
  });
}
function renderResources(st, w) {
  return el("section", {
    className: "pg-hud-panel",
    children: [
      networkField(st, w),
      el("div", {
        className: "pg-hud-gap",
        children: [
          el("p", { className: "pg-hud-lbl", text: tt("pg_wiz_vm_resources", "VM resources") }),
          vmResources(st, w),
          el("p", {
            className: "pg-hud-help",
            text: tt(
              "pg_wiz_resources_help",
              "Sizing for the target VM. Memory shown in GB. Destroyed when the session ends.",
            ),
          }),
        ],
      }),
    ],
  });
}

// ── Review (target only) ───────────────────────────────────────────────────
function fieldLabel(key) {
  return tt(`pg_wiz_field_${key}`, key);
}
function seqRow(key, val) {
  return el("div", {
    className: "pg-hud-seqrow",
    children: [
      el("span", { className: "pg-hud-seqrow__ok", text: "[ · ]" }),
      el("span", { className: "pg-hud-seqrow__k", text: fieldLabel(key) }),
      el("span", { className: "pg-hud-seqrow__v", text: val }),
    ],
  });
}
function reviewSeqRows(role, w, st) {
  const image = (st.catalog?.[role] || []).find((i) => i.id === w.imageId);
  const wl = (w.workloads || []).find((x) => x.id === w.workloadId);
  const services = (wl?.service_labels || []).join(", ");
  const rows = [
    seqRow("label", w.label || w.generatedLabel),
    seqRow("image", image ? image.label || image.id : "—"),
  ];
  if (role === "target")
    rows.push(
      seqRow("workload", wl ? wl.display_name || wl.id : tt("pg_wiz_no_workload", "No workload")),
    );
  if (services) rows.push(seqRow("services", services));
  if ((w.networkIds || []).length)
    w.networkIds.forEach((id) => rows.push(seqRow("network", networkReviewText(st, w, id))));
  else
    rows.push(
      seqRow(
        "network",
        `default · ${DEFAULT_NETWORK_CIDR} · ${tt("pg_wiz_auto_assigned", "auto-assigned")}`,
      ),
    );
  if (w.vcpus) rows.push(seqRow("vcpu", String(w.vcpus)));
  if (w.memoryMb) rows.push(seqRow("memory", memoryDisplay(w.memoryMb)));
  rows.push(seqRow("disk", `${w.diskGb} GB`));
  return rows;
}
function renderReview(role, w, st) {
  return el("section", {
    className: "pg-hud-panel",
    children: [
      el("p", { className: "pg-hud-lbl", text: tt("pg_wiz_launch_sequence", "Launch sequence") }),
      role === "target" && w.workloadId ? workloadPreparationNotice() : null,
      el("p", {
        className: "pg-hud-help",
        text: tt(
          "pg_network_egress_notice",
          "Egress-enabled networks allow outbound traffic. Isolated networks do not provide internet access.",
        ),
      }),
      el("div", {
        className: "pg-hud-seqbox",
        children: [
          el("div", {
            className: "pg-hud-seqbox__h",
            children: [
              el("span", { text: window.t("pg_form_review") }),
              el("span", { text: window.t("pg_host_not_checked") }),
            ],
          }),
          el("div", { className: "pg-hud-seqbody", children: reviewSeqRows(role, w, st) }),
          el("div", {
            className: "pg-hud-seq-auth",
            text: tt("pg_wiz_awaiting_confirmation", "awaiting confirmation"),
          }),
        ],
      }),
    ],
  });
}

// ── Manifest (presentational, every role) ──────────────────────────────────
function manifestReady(role, w, st) {
  const label = (w.label || w.generatedLabel || "").trim();
  if (!w.imageId || !/^[a-zA-Z0-9_-]+$/.test(label)) return false;
  const active = new Set(
    (st.session?.networks || []).filter((n) => n.state === "active").map((n) => n.id),
  );
  const drafts = new Set((w.newNetworks || []).map((n) => n.id));
  return (w.networkIds || []).every((id) => active.has(id) || drafts.has(id));
}
function manifestSizing(role, w) {
  const vcpu = w.vcpus || (role === "network" ? 1 : 2);
  const mem =
    w.memoryMb != null ? memoryDisplay(w.memoryMb) : tt("pg_wiz_image_default", "image default");
  return `${vcpu} vCPU · ${mem} · ${w.diskGb} GB`;
}
function maniRow(key, val, set) {
  return el("div", {
    className: "pg-hud-mani__row",
    dataset: { set: String(!!set), k: key },
    children: [
      el("span", { className: "pg-hud-mani__tick", text: set ? "▣" : "▢" }),
      el("div", {
        className: "pg-hud-mani__kv",
        children: [
          el("div", { className: "pg-hud-mani__k", text: fieldLabel(key) }),
          el("div", { className: "pg-hud-mani__v", text: val }),
        ],
      }),
    ],
  });
}
// Manifest content as plain data, so render (build) and refresh (in-place update)
// share one source of truth.
function manifestData(role, w, st) {
  const images = st.catalog?.[role] || [];
  const image = images.find((i) => i.id === w.imageId);
  const wl = (w.workloads || []).find((x) => x.id === w.workloadId);
  const label = (w.label || w.generatedLabel || "").trim();
  const netSel = (w.networkIds || []).length;
  const netTxt = netSel
    ? w.networkIds.map((id) => networkLabel(st, w, id)).join(", ")
    : tt("pg_wiz_default_auto", "default (auto)");
  const word = roleLabel(role);
  const rows = [
    { key: "label", val: label || "—", set: !!label },
    { key: "image", val: image ? image.label || image.id : "—", set: !!image },
  ];
  if (role === "target")
    rows.push({
      key: "workload",
      val: wl ? wl.display_name || wl.id : tt("pg_wiz_none", "none"),
      set: !!wl,
    });
  rows.push({ key: "networks", val: netTxt, set: netSel > 0 });
  rows.push({ key: "sizing", val: manifestSizing(role, w), set: true });
  const go = manifestReady(role, w, st);
  const readyText = go
    ? tt("pg_form_complete", "Form complete — host readiness not checked")
    : tt("pg_wiz_complete_fields", "complete the highlighted fields");
  return { rows, go, readyText, word };
}
function renderManifest(role, w, st) {
  const { rows, go, readyText, word } = manifestData(role, w, st);
  return el("aside", {
    className: "pg-hud-manifest",
    attrs: { "aria-label": tt("pg_wiz_manifest", "{role} manifest").replace("{role}", word) },
    children: [
      el("div", {
        className: "pg-hud-mani__h",
        text: tt("pg_wiz_manifest", "{role} manifest").replace("{role}", word),
      }),
      el("div", {
        className: "pg-hud-mani__body",
        children: rows.map((r) => maniRow(r.key, r.val, r.set)),
      }),
      el("div", {
        className: "pg-hud-mani__ready",
        dataset: { go: String(go) },
        text: readyText,
      }),
    ],
  });
}
function refreshManifest() {
  const st = getState();
  const role = st.ui.createRole;
  const w = st.ui.createWizard;
  if (!role || !w) return;
  const aside = $("create-vm-modal")?.querySelector(".pg-hud-manifest");
  if (aside) {
    const { rows, go, readyText } = manifestData(role, w, st);
    const rowEls = aside.querySelectorAll(".pg-hud-mani__row");
    // Same row set (no role change): patch text/state in place so the column is
    // never torn down and rebuilt — that wholesale swap is what flickered.
    if (rowEls.length === rows.length) {
      rows.forEach((r, i) => {
        const rowEl = rowEls[i];
        rowEl.dataset.set = String(!!r.set);
        const tick = rowEl.querySelector(".pg-hud-mani__tick");
        if (tick) tick.textContent = r.set ? "▣" : "▢";
        const v = rowEl.querySelector(".pg-hud-mani__v");
        if (v) v.textContent = r.val;
      });
      const ready = aside.querySelector(".pg-hud-mani__ready");
      if (ready) {
        ready.dataset.go = String(go);
        ready.textContent = readyText;
      }
    } else {
      aside.replaceWith(renderManifest(role, w, st));
    }
  }
  renderFooter(role, w);
}

// ── Attacker / Network Node (Label + Image + Networks only) ────────────────
function renderFlatRole(st, w, role) {
  return el("section", {
    className: "pg-hud-panel",
    children: [
      nameSection(role, w),
      imageSection(role, w, st, { reloadWorkloads: false }),
      el("div", { className: "pg-hud-gap", children: [networkField(st, w)] }),
    ],
  });
}

// ── Validation + footer (unchanged contract) ───────────────────────────────
function validateStep(role, w, st) {
  if (!w.imageId)
    return {
      ok: false,
      message: tt("pg_wiz_select_image", "Select an OS image before continuing."),
    };
  if (role !== "target") return { ok: true, message: "" };
  if (w.step === 0) {
    const label = (w.label || w.generatedLabel || "").trim();
    if (!/^[a-zA-Z0-9_-]+$/.test(label)) {
      return {
        ok: false,
        message: tt(
          "pg_wiz_invalid_label",
          "Use letters, numbers, underscores, or hyphens for VM label.",
        ),
      };
    }
  }
  if (w.step === 2) {
    const activeNetworks = new Set(
      (st.session?.networks || []).filter((n) => n.state === "active").map((n) => n.id),
    );
    const draftNetworks = new Set((w.newNetworks || []).map((n) => n.id));
    const validNetworks = (w.networkIds || []).every(
      (id) => activeNetworks.has(id) || draftNetworks.has(id),
    );
    if (!validNetworks)
      return {
        ok: false,
        message: tt(
          "pg_wiz_select_networks",
          "Select active networks or create a new Playground network.",
        ),
      };
    if ((w.vcpus || 0) < 1 || (w.vcpus || 0) > 8)
      return { ok: false, message: tt("pg_wiz_vcpu_range", "Choose 1–8 vCPUs.") };
    if ((w.memoryMb || 0) < 2048 || (w.memoryMb || 0) > 16384)
      return { ok: false, message: tt("pg_wiz_memory_range", "Choose 2–16 GB RAM.") };
    if ((w.diskGb || 0) < 20 || (w.diskGb || 0) > 160)
      return { ok: false, message: tt("pg_wiz_disk_range", "Choose 20–160 GB disk.") };
  }
  return { ok: true, message: "" };
}
function renderFooter(role, w) {
  const submit = $("create-vm-submit"),
    cancel = $("create-vm-cancel"),
    back = $("create-vm-back"),
    next = $("create-vm-next");
  if (!submit) return;

  const st = getState();
  const isTarget = role === "target";
  const isLast = isTarget && w.step === STEPS.length - 1;
  const validation = validateStep(role, w, st);

  if (cancel) cancel.hidden = false;
  if (back) back.hidden = !isTarget || w.step === 0;
  if (next) {
    next.hidden = !isTarget || isLast;
    next.disabled = !isTarget || !validation.ok;
    next.title = validation.ok ? "" : validation.message;
  }

  setWarning(isTarget && !isLast && !validation.ok ? validation.message : "");
  submit.hidden = isTarget && !isLast;
  submit.disabled = !validateStep(role, { ...w, step: Math.max(0, STEPS.length - 2) }, st).ok;
  submit.textContent = createText(role);
}

// ── Modal orchestration ────────────────────────────────────────────────────
function renderModal() {
  const st = getState();
  const role = st.ui.createRole;
  const modal = $("create-vm-modal");
  if (!modal) return;
  if (!role) {
    if (window.AlDialog?.current === modal) window.AlDialog.close();
    else modal.hidden = true;
    return;
  }
  if (!st.ui.createWizard || st.ui.createWizard.role !== role) {
    initWizard(role, st);
    return;
  }

  const w = st.ui.createWizard;
  const images = st.catalog?.[role] || [];
  if (!w.imageId && images[0]?.id) {
    setW({ imageId: images[0].id });
    if (role === "target") loadWorkloads(images[0].id);
    return;
  }
  setModalChrome(role, w);
  $("create-vm-form").querySelector('input[name="role"]').value = role;

  const isTarget = role === "target";
  const rail = buildVerticalRail({
    steps: isTarget ? STEPS : [singleTickLabel(role)],
    active: isTarget ? w.step : 0,
    onStep: (i) => {
      if (!isTarget) return;
      const validation = validateStep(role, w, st);
      if (i <= w.step || validation.ok) setW({ step: i });
      else setWarning(validation.message);
    },
  });
  const stepBody = isTarget
    ? w.step === 0
      ? renderBasics(role, w, st)
      : w.step === 1
        ? renderWorkload(w, st)
        : w.step === 2
          ? renderResources(st, w)
          : renderReview(role, w, st)
    : renderFlatRole(st, w, role);
  const content = el("div", { className: "pg-hud-content", children: [stepBody] });
  const manifest = renderManifest(role, w, st);

  // One-time boot reveal: only the first paint after open carries the boot
  // classes, so step changes / re-renders don't replay the staggered rail.
  const firstPaint = window.AlDialog?.current !== modal;
  // Gate the panel entry animation: play it on first paint and genuine step/role
  // changes, but not on same-step re-renders (regenerate name, draft-network add).
  const paintKey = `${role}:${isTarget ? w.step : 0}`;
  content.dataset.enter = String(firstPaint || paintKey !== lastPaintKey);
  lastPaintKey = paintKey;
  const section = modal.querySelector(".pg-v2-creator");
  if (firstPaint) {
    section?.classList.add("pg-hud-boot");
    rail.classList.add("pg-hud-rail--boot");
  }

  const fields = $("create-fields");
  fields.className = "pg-hud-grid";
  fields.replaceChildren(rail, content, manifest);

  renderFooter(role, w);
  if (firstPaint) {
    window.AlDialog.open(modal, {
      initial: "input, select, textarea, button",
      onClose: clearModal,
    });
    setTimeout(() => section?.classList.remove("pg-hud-boot"), 900);
  }
}
// An overlapping subnet is not refused: the backend picks a free one instead.
// The create-network dialog says so when it creates a network directly, but the
// wizard's draft networks are created later, with the VM, by which time that
// dialog is gone. An operator who typed one subnet and got another will point an
// attacker VM, a capture filter or a firewall rule at the one they asked for.
function substitutionNotice(requested, session) {
  const networks = session?.networks || [];
  const swapped = (requested || [])
    .map((want) => {
      const actual = networks.find((n) => n.name === want.name);
      return actual && want.subnet_cidr && actual.subnet_cidr !== want.subnet_cidr
        ? `${want.name}: ${tt("pg_wiz_cidr_substitution", "{from} overlapped an existing network; created as {to} instead.").replace("{from}", want.subnet_cidr).replace("{to}", actual.subnet_cidr)}`
        : null;
    })
    .filter(Boolean);
  return swapped.length ? swapped.join(" ") : undefined;
}
function selectedNewNetworks(w) {
  const ids = new Set(w.networkIds || []);
  return (w.newNetworks || [])
    .filter((n) => ids.has(n.id))
    .map(({ name, subnet_cidr, isolated }) => ({ name, subnet_cidr, isolated }));
}
function selectedExistingNetworkIds(w) {
  const draftIds = new Set((w.newNetworks || []).map((n) => n.id));
  return (w.networkIds || []).filter((id) => !draftIds.has(id));
}
function wireFooter() {
  $("create-vm-close")?.addEventListener("click", close);
  $("create-vm-cancel")?.addEventListener("click", close);
  $("create-vm-back")?.addEventListener("click", () => {
    const w = getState().ui.createWizard;
    if (w) setW({ step: Math.max(0, w.step - 1) });
  });
  $("create-vm-next")?.addEventListener("click", () => {
    const st = getState();
    const w = st.ui.createWizard;
    const role = st.ui.createRole;
    if (!w) return;
    const validation = validateStep(role, w, st);
    if (validation.ok) setW({ step: Math.min(STEPS.length - 1, w.step + 1) });
    else setWarning(validation.message);
  });
  $("create-vm-submit")?.addEventListener("click", async () => {
    const st = getState(),
      role = st.ui.createRole,
      w = st.ui.createWizard;
    if (!role || !w) return;
    const validation = validateStep(role, { ...w, step: Math.max(0, STEPS.length - 2) }, st);
    if (!validation.ok) {
      setWarning(validation.message);
      return;
    }
    try {
      const requestedNetworks = selectedNewNetworks(w);
      const createdVm = await createVm({
        label: w.label || w.generatedLabel,
        role,
        image_id: w.imageId,
        disk_gb: w.diskGb,
        vcpus: w.vcpus || undefined,
        memory_mb: w.memoryMb || undefined,
        network_ids: selectedExistingNetworkIds(w),
        new_networks: requestedNetworks,
        workload_id: role === "target" ? w.workloadId || null : null,
      });
      const session = await refreshSession();
      setState((prev) => ({
        ...prev,
        session,
        selectedId: createdVm?.id || session?.vms?.[0]?.id || null,
        notices: {
          ...prev.notices,
          cidrSubstituted: substitutionNotice(requestedNetworks, session),
        },
      }));
      close();
    } catch (err) {
      setErr(err.message);
    }
  });
}
export function mountCreateVm(onRetryCatalog = () => {}) {
  retryCatalog = onRetryCatalog;
  wireFooter();
  subscribe(
    (s) => ({
      createRole: s.ui.createRole,
      w: s.ui.createWizard,
      networkSig: networkSignature(s.session),
      catalog: s.catalog,
      catalogStatus: s.catalogStatus,
    }),
    renderModal,
  );
}

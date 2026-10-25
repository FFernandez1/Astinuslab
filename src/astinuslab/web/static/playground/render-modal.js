// Modal primitives + create-network controller. Create-vm lives in wizard.js and reuses these primitives.
import { el } from "./ui.js";
import { getState, setState, subscribe } from "./state.js";
import { createNetwork, refreshSession, suggestCidr } from "./api.js";
import { generatedPlaygroundName } from "./name-generator.js";
const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback || key;
// Vertical tick-rail stepper for the HUD creator. Future steps render as disabled
// <button>s (keeps the jobs-style "can't skip ahead" contract); completed/current
// steps stay keyboard-operable. data-active/data-done are boolean attributes so
// CSS [data-active] selectors and existing tests both match.
export function buildVerticalRail({ steps, active, onStep }) {
  return el("nav", {
    className: "pg-hud-rail",
    attrs: { "aria-label": tt("pg_wiz_steps", "Steps") },
    children: steps.map((label, i) => {
      const isActive = i === active;
      const isDone = i < active;
      return el("button", {
        className: "pg-hud-tick",
        attrs: {
          type: "button",
          "data-active": isActive,
          "data-done": isDone,
          "aria-current": isActive ? "step" : false,
          disabled: i > active,
        },
        children: [
          el("span", { className: "pg-hud-tick__box", text: isDone ? "✓" : String(i + 1) }),
          el("span", { className: "pg-hud-tick__lab", text: label }),
        ],
        onClick: () => {
          if (i <= active && onStep) onStep(i);
        },
      });
    }),
  });
}
// Corner-bracket OS-image select grid. Single-select; selection shown via the
// bracket overlay (data-selected) rather than a separate value line.
export function buildImageGrid({ images = [], value, onChange, labelledBy }) {
  const focusId = images.some((i) => i.id === value) ? value : images[0]?.id;
  const select = (id, focus = false) => {
    grid.querySelectorAll('[role="radio"]').forEach((tile) => {
      const on = tile.dataset.value === id;
      tile.dataset.selected = String(on);
      tile.setAttribute("aria-checked", String(on));
      tile.setAttribute("tabindex", on ? "0" : "-1");
      if (on && focus) tile.focus();
    });
    onChange?.(id);
  };
  const grid = el("div", {
    className: "pg-hud-images",
    attrs: { role: "radiogroup", "aria-labelledby": labelledBy },
    children: (images || []).map((i) => {
      const sub =
        [i.distro, i.version].filter(Boolean).join(" ") || (i.function_tags || []).join(", ") || "";
      const selected = i.id === value;
      // Move the selection in place (like buildHudSlider self-updates its fill),
      // so callers can refreshManifest() without a full re-render / animation replay.
      return el("div", {
        className: "pg-hud-image",
        dataset: { value: i.id, selected: String(selected) },
        attrs: {
          role: "radio",
          tabindex: i.id === focusId ? "0" : "-1",
          "aria-checked": String(selected),
        },
        children: [
          el("span", {
            className: "pg-hud-image__bkt",
            children: [el("b"), el("b"), el("b"), el("b")],
          }),
          el("span", { className: "pg-hud-image__n", text: i.label || i.id }),
          el("span", { className: "pg-hud-image__s", text: sub }),
        ],
        onClick: () => select(i.id),
        onKeyDown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            select(i.id);
          } else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
            e.preventDefault();
            const direction = ["ArrowLeft", "ArrowUp"].includes(e.key) ? -1 : 1;
            const next =
              (images.findIndex((image) => image.id === i.id) + direction + images.length) %
              images.length;
            select(images[next].id, true);
          }
        },
      });
    }),
  });
  return grid;
}
// Compact aligned network rows: checkbox | name | cidr | mode-badge | state.
// Disabled rows (non-active networks) are not toggleable.
export function buildNetworkRows({ options, selected, onToggle }) {
  const sel = new Set(selected || []);
  return el("div", {
    className: "pg-hud-netbox",
    children: (options || []).map((o) => {
      const on = sel.has(o.value);
      // Flip the row in place, then let the caller refreshManifest() — no full
      // re-render, so toggling a network never replays the panel entry animation.
      const toggle = (rowEl) => {
        if (o.disabled) return;
        const nowOn = rowEl.dataset.on !== "true";
        rowEl.dataset.on = String(nowOn);
        rowEl.setAttribute("aria-pressed", String(nowOn));
        const ck = rowEl.querySelector(".pg-hud-nrow__ck");
        if (ck) ck.textContent = nowOn ? "✓" : "";
        if (onToggle) onToggle(o.value);
      };
      return el("div", {
        className: "pg-hud-nrow",
        dataset: { on: String(on), disabled: String(!!o.disabled) },
        attrs: {
          role: "button",
          tabindex: o.disabled ? "-1" : "0",
          "aria-pressed": String(on),
          "aria-disabled": String(!!o.disabled),
          title: o.state
            ? tt("pg_wiz_network_state", "network state: {state}").replace("{state}", o.state)
            : false,
        },
        children: [
          el("span", { className: "pg-hud-nrow__ck", text: on ? "✓" : "" }),
          el("span", { className: "pg-hud-nrow__n", text: o.name }),
          el("span", { className: "pg-hud-nrow__cidr", text: o.cidr || "" }),
          el("span", {
            className: "pg-hud-nrow__m",
            children: [
              el("span", {
                className: `pg-hud-badge pg-hud-badge--${o.mode}`,
                text: tt(`pg_mode_${o.mode}`, o.mode),
              }),
              el("span", { className: "pg-hud-nrow__state", text: o.state }),
            ],
          }),
        ],
        onClick: (e) => toggle(e.currentTarget),
        onKeyDown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle(e.currentTarget);
          }
        },
      });
    }),
  });
}
// Preset-snapping slider (vCPU/memory/disk). The <input type=range> walks the
// preset index; the numeric box mirrors the chosen preset value. onChange gets
// the resolved preset value. Updates fill + value in place (no full re-render) so
// dragging stays smooth — callers refresh the manifest themselves.
export function buildHudSlider({ label, presets, value, unit, onChange }) {
  const idx = Math.max(0, presets.indexOf(value));
  const span = Math.max(1, presets.length - 1);
  const input = el("input", {
    className: "pg-hud-sl__i",
    attrs: {
      type: "text",
      inputmode: "numeric",
      value: String(value),
      "aria-label": tt("pg_wiz_value", "{label} value").replace("{label}", label),
    },
  });
  const range = el("input", {
    className: "pg-hud-sl__r",
    attrs: {
      type: "range",
      min: "0",
      max: String(span),
      step: "1",
      value: String(idx),
      "aria-label": label,
    },
  });
  const wrap = el("div", {
    className: "pg-hud-sl",
    children: [
      el("div", {
        className: "pg-hud-sl__h",
        children: [
          el("span", { className: "pg-hud-sl__n", text: label }),
          el("span", {
            className: "pg-hud-sl__e",
            children: [input, el("span", { text: unit })],
          }),
        ],
      }),
      range,
    ],
  });
  const fill = (i) => wrap.style.setProperty("--fill", `${Math.round((i / span) * 100)}%`);
  fill(idx);
  range.addEventListener("input", () => {
    const v = presets[Number(range.value)];
    input.value = String(v);
    fill(Number(range.value));
    if (onChange) onChange(v);
  });
  input.addEventListener("change", () => {
    const typed = parseFloat(input.value);
    const pick = presets.indexOf(typed) >= 0 ? typed : presets[0];
    const i = presets.indexOf(pick);
    range.value = String(i);
    input.value = String(pick);
    fill(i);
    if (onChange) onChange(pick);
  });
  return wrap;
}
function $(id) {
  return document.getElementById(id);
}
function setErr(id, msg) {
  const e = $(id);
  if (e) {
    e.hidden = !msg;
    e.textContent = msg || "";
  }
}
function setNotice(msg) {
  const e = $("network-notice");
  if (e) {
    e.hidden = !msg;
    e.textContent = msg || "";
  }
}
let generatedNetworkName = "";
let suggestedCidr = "";
function resetGeneratedNetworkName() {
  generatedNetworkName = generatedPlaygroundName("network");
}
function setNetworkModalChrome() {
  const modal = $("create-network-modal");
  const title = $("create-network-title");
  const eyebrow = $("create-network-eyebrow");
  const inVmWizard = !!getState().ui.createWizard;
  if (modal) modal.dataset.role = "network";
  if (eyebrow) eyebrow.textContent = tt("pg_eyebrow_new_network", "PLAYGROUND · NEW NETWORK");
  if (title)
    title.textContent = inVmWizard
      ? tt("pg_wiz_add_network", "Add network to VM")
      : tt("pg_create_network_title", "Create network");
}
function draftId() {
  return `draft-net-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
function addDraftNetwork({ name, subnet_cidr, isolated, cidr_auto }) {
  setState((prev) => {
    const wizard = prev.ui.createWizard;
    if (!wizard) return prev;
    const id = draftId();
    const newNetworks = [
      ...(wizard.newNetworks || []),
      { id, name, subnet_cidr, isolated, cidr_auto },
    ];
    const networkIds = [...(wizard.networkIds || []), id];
    return {
      ...prev,
      ui: {
        ...prev.ui,
        createNetworkOpen: false,
        createWizard: { ...wizard, newNetworks, networkIds },
      },
    };
  });
}
export function mountCreateNetwork() {
  const modal = $("create-network-modal");
  if (!modal) return;
  const clearModal = () => {
    setState((prev) => ({ ...prev, ui: { ...prev.ui, createNetworkOpen: false } }));
    setErr("network-error", "");
    setNotice("");
    const nameInput = $("create-network-name");
    if (nameInput) nameInput.value = "";
  };
  const close = () => {
    if (window.AlDialog?.current === modal) window.AlDialog.close();
    else clearModal();
  };
  $("create-network-close")?.addEventListener("click", close);
  $("create-network-cancel")?.addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) close();
  });
  $("create-network-submit")?.addEventListener("click", async () => {
    const networkName = $("create-network-name")?.value?.trim() || "";
    const name = networkName || generatedNetworkName;
    const typedCidr = $("create-network-cidr")?.value?.trim() || "";
    const cidr = typedCidr || suggestedCidr;
    const iso = !!modal.querySelector('input[name="isolated"]')?.checked;
    if (!cidr) {
      setErr("network-error", tt("pg_wiz_cidr_required", "Subnet CIDR required."));
      return;
    }
    if (getState().ui.createWizard) {
      const cidr_auto = !typedCidr || (typedCidr === suggestedCidr && suggestedCidr !== "");
      addDraftNetwork({ name, subnet_cidr: cidr, isolated: iso, cidr_auto });
      setErr("network-error", "");
      return;
    }
    try {
      const result = await createNetwork({ name, subnet_cidr: cidr, isolated: iso });
      const session = await refreshSession();
      setErr("network-error", "");
      const substituted = (result?.networks || []).find((n) => n.cidr_substituted_from);
      if (substituted) {
        setState((prev) => ({ ...prev, session }));
        setNotice(
          tt(
            "pg_wiz_cidr_substitution",
            "{from} overlapped an existing network; created as {to} instead.",
          )
            .replace("{from}", substituted.cidr_substituted_from)
            .replace("{to}", substituted.subnet_cidr),
        );
        const submitBtn = $("create-network-submit");
        if (submitBtn) submitBtn.disabled = true;
      } else {
        setState((prev) => ({ ...prev, session, ui: { ...prev.ui, createNetworkOpen: false } }));
      }
    } catch (err) {
      setErr("network-error", err.message);
    }
  });
  subscribe(
    (s) => s.ui.createNetworkOpen,
    (open) => {
      if (!open) {
        if (window.AlDialog?.current === modal) window.AlDialog.close();
        else modal.hidden = true;
        return;
      }
      resetGeneratedNetworkName();
      setNetworkModalChrome();
      setErr("network-error", "");
      setNotice("");
      const submitBtn = $("create-network-submit");
      if (submitBtn) submitBtn.disabled = false;
      const nameInput = $("create-network-name");
      if (nameInput) nameInput.setAttribute("placeholder", generatedNetworkName);
      const cidrInput = $("create-network-cidr");
      if (cidrInput) {
        suggestedCidr = "";
        cidrInput.value = "";
        const st = getState();
        const jobId = st.session?.session_id || st.session?.job_id;
        const extra = (st.ui.createWizard?.newNetworks || [])
          .map((n) => n.subnet_cidr)
          .filter(Boolean);
        suggestCidr({ jobId, extra })
          .then((res) => {
            if (res?.subnet_cidr) {
              suggestedCidr = res.subnet_cidr;
              if (!cidrInput.value) cidrInput.value = suggestedCidr;
            }
          })
          .catch(() => {});
      }
      if (window.AlDialog?.current !== modal) {
        window.AlDialog.open(modal, {
          initial: "#create-network-name",
          onClose: clearModal,
        });
      }
    },
  );
}

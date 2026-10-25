// Shared DOM + style helpers. Pure functions; no state, no I/O.
// This module deliberately exposes no raw markup escape hatch.
// All text goes through textContent so untrusted strings stay inert.

export function safeHref(raw) {
  if (!raw) return null;
  const trimmed = String(raw).trim();
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed, window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch (_) {}
  return null;
}

import { el } from "../lib/dom.js";
export { el };

export function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

export function kvRow(label, value, valueClass) {
  return el("div", {
    className: "pg-v2-kv-row",
    children: [
      el("span", { className: "k", text: label }),
      el("span", { className: valueClass ? `v ${valueClass}` : "v", text: value }),
    ],
  });
}

export function barLine(label, pct, color) {
  const fill = el("i");
  fill.style.setProperty("width", `${Math.max(0, Math.min(100, pct))}%`);
  if (color) fill.style.setProperty("background", color);
  return el("div", {
    className: "pg-bar-row",
    children: [
      el("span", { className: "k", text: label }),
      el("span", { className: "pg-bar", children: [fill] }),
      el("span", { text: `${Math.round(pct)}%` }),
    ],
  });
}

export function cpuColor(pct) {
  if (pct >= 90) return "var(--c-danger)";
  if (pct >= 70) return "var(--c-warning)";
  return "var(--c-success)";
}
export function roleAccent(role) {
  if (role === "attacker") return "var(--c-danger)";
  if (role === "network") return "var(--c-purple)";
  return "var(--c-info)";
}

// Maps a coarse backend vm.state plus IP presence to a user-readable phase
// label. Does not change backend states; the enum names remain canonical and
// are still exposed via vm.state / data-state for styling.
export function phaseLabel(vm) {
  const tt = window.t || ((k) => k);
  const state = (vm && vm.state) || "unknown";
  const hasIp = vm?.reachability_status
    ? vm.reachability_status === "ready"
    : Boolean(vm?.ip && vm.ip !== "—" && vm.ip !== "partial");
  if (state === "queued" || state === "pending") return tt("pg_phase_queued");
  if (state === "starting") return tt("pg_phase_starting");
  if (state === "booting") return tt("pg_phase_booting");
  if (state === "stopping") return tt("pg_phase_stopping");
  if (state === "running") return hasIp ? tt("pg_phase_ready") : tt("pg_phase_waiting_dhcp");
  if (state === "stopped" || state === "shutoff" || state === "shutdown")
    return tt("pg_phase_powered_off");
  if (state === "destroying") return tt("pg_phase_tearing_down");
  if (state === "destroyed") return tt("pg_phase_destroyed");
  if (state === "teardown_failed") return tt("pg_phase_teardown_failed");
  if (state === "failed") return tt("pg_phase_failed");
  return state;
}

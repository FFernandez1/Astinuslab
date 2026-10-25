// Small DOM + i18n helpers shared across the volatility workbench modules.
const t =
  window.t ||
  function (k, f) {
    return f != null ? f : k;
  };

const fmt = function (s, vars) {
  return s.replace(/\{(\w+)\}/g, function (_, k) {
    return vars[k];
  });
};

const CAT_COLORS = {
  info: "var(--c-info)",
  proc: "var(--c-info)",
  mal: "var(--c-danger)",
  net: "var(--primary)",
  files: "var(--c-warning)",
  persist: "var(--c-purple)",
  other: "var(--text-muted)",
};

export { t, fmt, CAT_COLORS };

export function $(id) {
  return document.getElementById(id);
}

export function el(tag, cls, txt) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}

// Quotes are escaped too: several call sites interpolate this into a
// double-quoted attribute (href=, data-job=, title=), where a bare " would
// close the attribute and let the rest of the value become new markup.
export function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Only same-origin, absolute-path URLs may reach an href. Everything the
// backend puts there is a server-generated "/jobs/<id>"; anything else -- a
// javascript: or data: scheme, or a protocol-relative "//host" -- is refused
// rather than rendered.
export function safePath(url) {
  var value = String(url == null ? "" : url);
  return /^\/(?!\/)/.test(value) ? value : "";
}

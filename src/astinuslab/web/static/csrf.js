const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function csrfToken() {
  if (typeof document === "undefined" || typeof document.querySelector !== "function") return "";
  return document.querySelector('meta[name="astinuslab-csrf-token"]')?.content || "";
}

export function csrfHeaders(method, headers = {}) {
  const result = { ...headers };
  if (UNSAFE_METHODS.has(String(method).toUpperCase())) {
    const token = csrfToken();
    if (token) result["X-CSRF-Token"] = token;
  }
  return result;
}

// Classic <script> files and nonce'd inline page scripts cannot `import`, so the
// same helper is published on window for them. This module is deferred like any
// other, and a classic `defer` script earlier in <head> runs before it -- so a
// caller must read window.csrfHeaders at request time, never at load time.
if (typeof window !== "undefined") {
  window.csrfHeaders = csrfHeaders;
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("htmx:configRequest", (event) => {
    const method = event.detail.verb || event.detail.xhr?.method || "GET";
    if (!UNSAFE_METHODS.has(String(method).toUpperCase())) return;
    const token = csrfToken();
    if (token) event.detail.headers["X-CSRF-Token"] = token;
  });
}

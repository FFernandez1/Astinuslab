(function () {
  function showToast(message, kind) {
    const root = document.getElementById("al-toast-root");
    if (!root) return;
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " toast--" + kind : "");
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, 5000);
  }

  window.showToast = showToast;

  const t = window.t || ((key, fallback) => (fallback != null ? fallback : key));
  document.body.addEventListener("htmx:responseError", (event) => {
    const status = event.detail && event.detail.xhr ? event.detail.xhr.status : "?";
    showToast(t("htmx_error", "Request failed") + " (" + status + ")", "error");
  });
  document.body.addEventListener("htmx:sendError", () => {
    showToast(t("htmx_network_error", "Network error — check the connection"), "error");
  });
})();

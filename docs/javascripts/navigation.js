(function () {
  let searchOrigin;

  function toggle(id, checked) {
    const control = document.getElementById(id);
    if (!control) return;
    control.checked = checked;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function openSearch() {
    searchOrigin = document.activeElement;
    toggle("__search", true);
    document.querySelector('[data-md-component="search-query"]')?.focus();
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest(".astinuslab-search-trigger")) {
      event.preventDefault();
      openSearch();
    } else if (event.target.closest(".astinuslab-drawer")) {
      toggle("__drawer", !document.getElementById("__drawer").checked);
    }
  });

  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      if (target.closest("input, textarea, select, [contenteditable]:not([contenteditable=false])")) return;
      event.preventDefault();
      openSearch();
    } else if (event.key === "Enter" || event.key === " ") {
      if (target.matches(".astinuslab-search-trigger")) {
        event.preventDefault();
        openSearch();
      }
    } else if (event.key === "Escape") {
      if (document.getElementById("__search")?.checked) {
        event.preventDefault();
        toggle("__search", false);
        (searchOrigin?.isConnected ? searchOrigin : document.querySelector(".astinuslab-search-trigger"))?.focus();
      } else if (document.getElementById("__drawer")?.checked) {
        event.preventDefault();
        toggle("__drawer", false);
        document.querySelector(".astinuslab-drawer")?.focus();
      }
    }
  }, true);

  document.addEventListener("change", (event) => {
    if (event.target.id === "__drawer") {
      document.querySelector(".astinuslab-drawer")?.setAttribute("aria-expanded", String(event.target.checked));
    }
  });
})();

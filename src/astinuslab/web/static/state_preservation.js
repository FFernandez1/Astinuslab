(function () {
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.target?.id !== "jobs-table") return;
    if (
      document.querySelector(".job-select:checked") ||
      document.querySelector(".modal-overlay--open")
    ) {
      event.preventDefault();
    }
  });

  // Preserve checkbox selection across jobs-table HTMX polling swaps.
  let savedJobIds = new Set();
  let removedControl = null;
  let tableScrollLeft = 0;
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.target?.id !== "jobs-table") return;
    tableScrollLeft = event.target.querySelector(".table-scroll")?.scrollLeft || 0;
    const focused = document.activeElement;
    const row = focused?.closest('tr[id^="job-row-"]');
    removedControl =
      row && focused.id ? { id: focused.id, jobId: row.id.slice("job-row-".length) } : null;
    savedJobIds = new Set(
      [...document.querySelectorAll(".job-select:checked")].map((cb) => cb.value),
    );
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target?.id !== "jobs-table") return;
    const scroll = event.target.querySelector(".table-scroll");
    if (scroll) scroll.scrollLeft = tableScrollLeft;
    tableScrollLeft = 0;
    const previous = removedControl;
    removedControl = null;
    // HTMX restores surviving controls by their stable IDs. Only a removed
    // action needs a fallback, and a user's newer focus always takes priority.
    if (
      !previous ||
      document.activeElement !== document.body ||
      document.getElementById(previous.id)
    )
      return;
    (
      document.getElementById(`job-open-${previous.jobId}`) || document.getElementById("jobs-new")
    )?.focus();
  });
  document.addEventListener("htmx:afterSettle", () => {
    if (savedJobIds.size === 0) return;
    document.querySelectorAll(".job-select").forEach((cb) => {
      if (savedJobIds.has(cb.value) && !cb.disabled) cb.checked = true;
    });
    savedJobIds = new Set();
  });
})();

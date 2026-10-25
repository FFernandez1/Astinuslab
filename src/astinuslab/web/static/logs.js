// Log presentation stays client-side; copying and downloading retain raw entries.
(function () {
  const content = document.getElementById("log-content");
  if (!content) return;
  const t = window.t;
  const search = document.getElementById("log-search");
  const clear = document.querySelector("[data-clear-log-search]");
  const wrap = document.querySelector("[data-toggle-log-wrap]");
  const routine = document.querySelector("[data-hide-routine]");
  const liveButton = document.querySelector("[data-toggle-auto-refresh]");
  const state = document.getElementById("log-tail-state");
  const status = document.getElementById("log-refresh-status");
  let live = false;
  let previousScrollTop = 0;
  let lastUpdated = "";
  let searchTimer;
  const viewOptions = document.querySelector(".logs-view-options");
  const smallScreen = window.matchMedia("(max-width: 640px)");
  const updateOptions = () => {
    viewOptions.open = !smallScreen.matches;
  };
  updateOptions();
  smallScreen.addEventListener("change", updateOptions);
  const level = document.getElementById("level-select");
  const lines = document.getElementById("lines-input");
  level.addEventListener("change", () => {
    lines.max = level.value === "all" ? lines.dataset.limitAll : lines.dataset.limitFiltered;
    if (lines.valueAsNumber > Number(lines.max)) lines.value = lines.max;
  });

  function format(key, values) {
    return Object.entries(values).reduce(
      (text, [name, value]) => text.replace(`{${name}}`, value),
      t(key),
    );
  }

  function rememberView() {
    try {
      localStorage.setItem(
        "al_logs_view",
        JSON.stringify({ wrap: wrap.checked, routine: routine.checked }),
      );
    } catch (error) {
      /* Storage can be unavailable in private browsing. */
    }
  }

  try {
    const saved = JSON.parse(localStorage.getItem("al_logs_view") || "{}");
    wrap.checked = saved.wrap !== false;
    routine.checked = saved.routine === true;
  } catch (error) {
    /* Use readable defaults. */
  }

  function rawText(row) {
    return row.querySelector(".log-line__raw").textContent;
  }

  function isRoutine(row) {
    if (!["info", "debug", "other"].includes(row.dataset.level)) return false;
    const text = rawText(row);
    // Only successful read requests to assets, health, or this viewer's poll.
    // A failed request is useful even when its logger labels it INFO.
    return (
      /(?:method=(?:GET|HEAD) path=|"(?:GET|HEAD) )(?:\/static\/[^\s"]*|\/health(?:z)?(?:\?[^\s"]*)?|\/logs\/fragment(?:\?[^\s"]*)?)(?:\s|$)/.test(
        text,
      ) && /(?:status=|HTTP\/\d(?:\.\d)?" )(?:2\d\d|304)(?:\s|$)/.test(text)
    );
  }

  // Renders one JSON record, or reports false so the text parsers get a turn.
  // Text nodes only, as above: a log payload must never become HTML.
  function renderJsonLine(row, raw) {
    let record;
    try {
      record = JSON.parse(raw);
    } catch {
      return false;
    }
    if (!record || typeof record !== "object" || typeof record.message !== "string") return false;
    const context = ["job_id", "action_id"]
      .filter((field) => typeof record[field] === "string" && record[field])
      .map((field) => `${field}=${record[field]}`)
      .join(" ");
    row.classList.add("log-line--structured");
    const text = row.querySelector(".log-line__text");
    const time = document.createElement("span");
    time.className = "log-line__time";
    time.textContent =
      typeof record.timestamp === "string" ? record.timestamp.replace("T", " ").slice(0, 19) : "";
    text.textContent = context ? `${record.message} ${context}` : record.message;
    text.before(time);
    row.querySelector(".log-line__details").hidden = false;
    return true;
  }

  function enhanceRows() {
    content.querySelectorAll(".log-line").forEach((row) => {
      const raw = rawText(row);
      // JSON records (log_format = "json") carry the same fields by name.
      // Handled first: a structured line needs no pattern matching at all.
      if (raw.startsWith("{")) {
        if (renderJsonLine(row, raw)) return;
      }
      // Unknown formats stay intact. Parse only recognized timestamp prefixes;
      // use text nodes so log payloads can never become HTML.
      const celery = raw.match(
        /^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?): (?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\/[^\]]+\] (.*)$/,
      );
      const match = (celery ? celery[2] : raw).match(
        /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] (?:request_id=\S+ )?([^:]+): (.*)$/,
      );
      if (!match && !celery) return;
      row.classList.add("log-line--structured");
      const text = row.querySelector(".log-line__text");
      const time = document.createElement("span");
      time.className = "log-line__time";
      time.textContent = match ? match[1] : celery[1];
      text.textContent = match ? match[4] : celery[2];
      text.before(time);
      row.querySelector(".log-line__details").hidden = false;
    });
  }

  function applyView() {
    const viewport = document.getElementById("log-viewport");
    viewport.classList.toggle("log-viewport--nowrap", !wrap.checked);
    const needle = search.value.trim().toLowerCase();
    const rows = [...viewport.querySelectorAll(".log-line")];
    const counts = {};
    let visible = 0;
    rows.forEach((row) => {
      const match =
        (!needle || rawText(row).toLowerCase().includes(needle)) &&
        (!routine.checked || !isRoutine(row));
      row.classList.toggle("log-line--hidden", !match);
      row.classList.toggle("log-line--match", Boolean(needle) && match);
      if (match) {
        visible++;
        counts[row.dataset.level] = (counts[row.dataset.level] || 0) + 1;
      }
    });
    content.querySelector(".logs-showing").textContent = format("logs_visible_count", {
      visible,
      loaded: rows.length,
    });
    const stats = content.querySelector(".log-stats-bar");
    stats.replaceChildren();
    ["debug", "info", "warning", "error", "critical", "other"].forEach((level) => {
      if (!counts[level]) return;
      const chip = document.createElement("span");
      chip.className = `log-stats-chip log-stats-chip--${level}`;
      chip.textContent = `${counts[level]} ${level === "other" ? t("logs_level_other") : level.toUpperCase()}`;
      stats.append(chip);
    });
    content.querySelector("[data-log-no-matches]").hidden = rows.length === 0 || visible > 0;
    clear.hidden = !search.value;
    syncSearch();
    previousScrollTop = viewport.scrollTop;
  }

  function syncSearch() {
    const current = new URL(window.location.href);
    if (search.value) current.searchParams.set("search", search.value);
    else current.searchParams.delete("search");
    window.history.replaceState(window.history.state, "", current);
    document.querySelectorAll(".tab-strip a").forEach((link) => {
      const url = new URL(link.href);
      url.searchParams.set("search", search.value);
      link.href = url.pathname + url.search;
    });
  }

  function scrollToLatest() {
    const viewport = document.getElementById("log-viewport");
    viewport.scrollTop = viewport.scrollHeight;
    previousScrollTop = viewport.scrollTop;
  }

  function setLive(enabled, forReading = false) {
    live = enabled;
    liveButton.setAttribute("aria-pressed", String(live));
    liveButton.classList.toggle("logs-tail-btn--active", live);
    state.textContent = t(live ? "logs_live" : "logs_paused");
    content.setAttribute("hx-trigger", live ? "refreshLogs, every 5s" : "refreshLogs");
    window.htmx.process(content);
    if (!live) window.htmx.trigger(content, "htmx:abort");
    if (forReading) {
      status.textContent = t("logs_reading_older");
    }
  }

  function refreshed() {
    lastUpdated = new Intl.DateTimeFormat(document.documentElement.lang, {
      timeStyle: "medium",
    }).format(new Date());
    status.textContent = format("logs_updated", { time: lastUpdated });
    status.classList.remove("logs-refresh-error");
  }

  const boundViewports = new WeakSet();
  function bindViewport() {
    const viewport = document.getElementById("log-viewport");
    if (boundViewports.has(viewport)) return;
    boundViewports.add(viewport);
    previousScrollTop = viewport.scrollTop;
    viewport.addEventListener("scroll", () => {
      if (
        live &&
        viewport.scrollTop < previousScrollTop - 1 &&
        viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight > 40
      ) {
        setLive(false, true);
      }
      previousScrollTop = viewport.scrollTop;
    });
  }

  liveButton.addEventListener("click", () => {
    setLive(!live);
    if (live) {
      scrollToLatest();
      window.htmx.trigger(content, "refreshLogs");
    } else {
      status.textContent = format("logs_updated", { time: lastUpdated });
    }
  });
  document.querySelector("[data-scroll-log-bottom]").addEventListener("click", scrollToLatest);
  search.addEventListener("input", () => {
    syncSearch();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyView, 150);
  });
  // Native details toggle events do not bubble. Capture them before a poll can
  // replace the entry being inspected, including entries opened by keyboard.
  content.addEventListener(
    "toggle",
    (event) => {
      if (live && event.target.matches(".log-line__details[open]")) setLive(false, true);
    },
    true,
  );
  clear.addEventListener("click", () => {
    search.value = "";
    clearTimeout(searchTimer);
    applyView();
    search.focus();
  });
  [wrap, routine].forEach((control) =>
    control.addEventListener("change", () => {
      applyView();
      rememberView();
    }),
  );

  const copy = document.querySelector("[data-copy-log-view]");
  let copyTimer;
  copy.addEventListener("click", async () => {
    // Flush a pending search so copying always reflects the current query.
    clearTimeout(searchTimer);
    applyView();
    const text = [...content.querySelectorAll(".log-line:not(.log-line--hidden)")]
      .map(rawText)
      .join("\n");
    let label = t("logs_nothing_to_copy");
    if (text) {
      try {
        await navigator.clipboard.writeText(text);
        label = copy.dataset.copied;
      } catch (error) {
        label = t("logs_copy_failed");
      }
    }
    copy.textContent = label;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copy.textContent = t("logs_copy_visible");
    }, 1800);
  });

  function isLogEvent(event) {
    return event.detail.target === content || event.detail.elt === content;
  }
  document.addEventListener("htmx:beforeSwap", (event) => {
    // An in-flight request must not replace the lines the user paused to read.
    if (isLogEvent(event) && !live) event.detail.shouldSwap = false;
  });
  document.addEventListener("htmx:afterSettle", (event) => {
    // HTMX restores response classes during settling. Apply view preferences
    // afterwards, and ignore the separate out-of-band file metadata update.
    if (event.target !== content) return;
    enhanceRows();
    applyView();
    bindViewport();
    if (live) scrollToLatest();
    if (live) refreshed();
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target !== content) return;
    // The new viewport is interactive before HTMX finishes settling classes.
    // Bind now so a reader's upward scroll in that interval pauses the tail.
    bindViewport();
    if (live) scrollToLatest();
  });
  function refreshFailed(event) {
    if (!isLogEvent(event) || !live) return;
    setLive(false);
    status.textContent = format("logs_refresh_failed", { time: lastUpdated });
    status.classList.add("logs-refresh-error");
  }
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach((name) =>
    document.addEventListener(name, refreshFailed),
  );
  enhanceRows();
  applyView();
  bindViewport();
  refreshed();
})();

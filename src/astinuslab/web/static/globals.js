(function () {
  const COPY_LABEL = window.t ? window.t("btn_copy", "Copy") : "Copy";

  // Progressive enhancement: navigation remains expanded without JavaScript.
  const sidebar = document.querySelector(".sidebar");
  const menu = document.querySelector(".sidebar-menu-toggle");
  if (sidebar && menu) {
    sidebar.classList.add("nav-ready");
    const closeMenu = () => {
      sidebar.classList.remove("sidebar--open");
      menu.setAttribute("aria-expanded", "false");
    };
    menu.addEventListener("click", () => {
      const open = menu.getAttribute("aria-expanded") !== "true";
      sidebar.classList.toggle("sidebar--open", open);
      menu.setAttribute("aria-expanded", String(open));
    });
    sidebar.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.getAttribute("aria-expanded") === "true") {
        closeMenu();
        menu.focus();
      }
    });
    window.matchMedia("(max-width: 600px)").addEventListener("change", closeMenu);
  }

  // "Select all" toggle
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.matches("[data-toggle-all]")) return;
    const selector = target.getAttribute("data-toggle-all");
    if (!selector) return;
    document.querySelectorAll(selector).forEach((el) => {
      if (el instanceof HTMLInputElement && !el.disabled) {
        el.checked = target.checked;
      }
    });
  });

  // Settings toast dismissal. The toast arrives as an htmx out-of-band swap and
  // is replaced on every poll, so this is delegated rather than bound per node.
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const btn = target.closest("[data-dismiss-settings-toast]");
    if (!btn) return;
    const region = btn.closest(".settings-toasts");
    if (region) region.replaceChildren();
  });

  // System clock (UTC + Local)
  function tick() {
    const d = new Date();
    const h = String(d.getUTCHours()).padStart(2, "0");
    const m = String(d.getUTCMinutes()).padStart(2, "0");
    const s = String(d.getUTCSeconds()).padStart(2, "0");
    const utcStr = "utc " + h + ":" + m + ":" + s;

    const lh = String(d.getHours()).padStart(2, "0");
    const lm = String(d.getMinutes()).padStart(2, "0");
    const ls = String(d.getSeconds()).padStart(2, "0");

    let tz = "";
    try {
      const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d);
      for (let i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") {
          tz = parts[i].value;
          break;
        }
      }
    } catch (error) {
      /* ignore */
    }

    const localStr = (tz ? tz + " " : "") + lh + ":" + lm + ":" + ls;

    const el = document.getElementById("utc-clock");
    if (el) el.textContent = utcStr + " | " + localStr;
  }
  tick();
  setInterval(tick, 1000);

  // Copy SSH command to clipboard.
  function copySSH(btn) {
    const cmdEl = btn.closest(".ssh-block")?.querySelector(".ssh-block__cmd");
    const cmd = cmdEl ? cmdEl.textContent.trim() : "";
    if (!cmd) return;
    navigator.clipboard
      .writeText(cmd)
      .then(() => {
        btn.textContent = "✓";
        btn.classList.add("copied");
        setTimeout(() => {
          btn.textContent = COPY_LABEL;
          btn.classList.remove("copied");
        }, 1500);
      })
      .catch(() => {
        btn.textContent = "✗";
        setTimeout(() => {
          btn.textContent = COPY_LABEL;
        }, 1500);
      });
  }

  document.addEventListener("click", (event) => {
    const target =
      event.target && event.target.nodeType === 1 ? event.target : event.target.parentElement;
    const btn = target ? target.closest("[data-copy-ssh]") : null;
    if (!btn) return;
    copySSH(btn);
  });
})();

(function () {
  function t(key, fallback) {
    return window.t ? window.t(key, fallback) : fallback;
  }

  function confirmDialog(message) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay modal-overlay--open modal-overlay--confirm";
      overlay.innerHTML =
        '<div class="modal-dialog confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-message">' +
        '<div class="modal-header"><div class="modal-header__title" id="confirm-dialog-title"></div></div>' +
        '<div class="modal-body confirm-dialog__message" id="confirm-dialog-message"></div>' +
        '<div class="modal-footer modal-footer--end">' +
        '<button type="button" class="wiz-btn wiz-btn--secondary" data-confirm-no></button>' +
        '<button type="button" class="wiz-btn wiz-btn--danger" data-confirm-yes></button>' +
        "</div></div>";

      overlay.querySelector("#confirm-dialog-title").textContent = t("btn_confirm", "Confirm");
      overlay.querySelector(".confirm-dialog__message").textContent = message;
      overlay.querySelector("[data-confirm-no]").textContent = t("btn_cancel", "Cancel");
      overlay.querySelector("[data-confirm-yes]").textContent = t("btn_confirm", "Confirm");

      let confirmed = false;
      function cleanup(ok) {
        confirmed = ok;
        window.AlDialog.close();
      }

      overlay.querySelector("[data-confirm-no]").addEventListener("click", () => cleanup(false));
      overlay.querySelector("[data-confirm-yes]").addEventListener("click", () => cleanup(true));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
      document.body.appendChild(overlay);
      window.AlDialog.open(overlay, {
        openClass: "modal-overlay--open",
        initial: "[data-confirm-no]",
        onClose: () => {
          overlay.remove();
          resolve(confirmed);
        },
      });
    });
  }

  function confirmAndSubmit(form, submitter, question) {
    confirmDialog(question).then((ok) => {
      if (!ok) return;
      form.dataset.confirmBypass = "1";
      if (form.requestSubmit) {
        form.requestSubmit(submitter || undefined);
      } else {
        form.submit();
      }
    });
  }

  function confirmAndClick(el, question) {
    confirmDialog(question).then((ok) => {
      if (!ok) return;
      el.dataset.confirmBypass = "1";
      el.click();
    });
  }

  window.confirmDialog = confirmDialog;

  document.body.addEventListener("htmx:confirm", (event) => {
    const question = event.detail && event.detail.question;
    if (!question) return;
    event.preventDefault();
    confirmDialog(question).then((ok) => {
      if (ok) event.detail.issueRequest(true);
    });
  });

  document.addEventListener("click", (event) => {
    const target =
      event.target && event.target.nodeType === 1 ? event.target : event.target.parentElement;
    const el = target ? target.closest("[data-confirm]") : null;
    if (!el || el.tagName === "FORM") return;
    if (el.dataset.confirmBypass === "1") {
      delete el.dataset.confirmBypass;
      return;
    }
    const question = el.dataset.confirm;
    if (!question) return;
    const form =
      el.form || (el.getAttribute("form") && document.getElementById(el.getAttribute("form")));
    event.preventDefault();
    if (form && (el.type || "").toLowerCase() === "submit") {
      confirmAndSubmit(form, el, question);
    } else {
      confirmAndClick(el, question);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || form.tagName !== "FORM") return;
    const question = form.dataset.confirm;
    if (!question) return;
    if (form.dataset.confirmBypass === "1") {
      delete form.dataset.confirmBypass;
      return;
    }
    event.preventDefault();
    confirmAndSubmit(form, event.submitter, question);
  });
})();

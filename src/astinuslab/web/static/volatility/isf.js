// ISF (symbol table) build + upload row wiring on the Activity panel.
import { t, escapeHtml, safePath } from "./utils.js";
import { refreshQueue } from "./queue.js";
import { vol3Enabled } from "./readiness.js";
import { csrfHeaders } from "../csrf.js";

function setIsfRowState(form, cls, text, url) {
  var row = form.closest(".vol-isfrow");
  var stateCell = row ? row.querySelector(".vol-isf-state") : null;
  if (!stateCell) return;
  var status = stateCell.querySelector("[data-isf-status]");
  if (!status) {
    status = document.createElement("div");
    status.setAttribute("data-isf-status", "");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    stateCell.appendChild(status);
  }
  var href = safePath(url);
  if (href) {
    status.innerHTML =
      '<a class="wiz-btn wiz-btn--ghost wiz-btn--xs" href="' +
      escapeHtml(href) +
      '">' +
      escapeHtml(text) +
      "</a>";
  } else {
    status.innerHTML = '<span class="' + cls + '">' + escapeHtml(text) + "</span>";
  }
}

export function wireIsfBuildForms() {
  document.querySelectorAll(".vol-isf-form").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      // Only a disabled integration stops the submission.  An unready dwarf2json
      // leaves prebuilt and debuginfod symbol sources usable, and the server
      // enforces the tool on the paths that actually execute it.
      if (!vol3Enabled()) {
        if (btn) btn.disabled = true;
        return;
      }
      var originalText = btn ? btn.textContent : "Build ISF";
      var restoreFocus = btn && document.activeElement === btn;
      if (btn) {
        btn.disabled = true;
        btn.textContent = t("vol_isf_button_queued", "queued…");
      }
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: csrfHeaders("POST", { Accept: "application/json", "X-Requested-With": "fetch" }),
      })
        .then(function (r) {
          return r
            .json()
            .catch(function () {
              return {};
            })
            .then(function (body) {
              if (!r.ok) {
                var err = new Error(
                  body.detail || t("vol_isf_build_failed", "ISF build request failed"),
                );
                err.detail = body.detail;
                throw err;
              }
              return body;
            });
        })
        .then(function (body) {
          setIsfRowState(
            form,
            "vol-dot-pend",
            t("vol_isf_queued"),
            body.url || (body.job_id ? "/jobs/" + body.job_id : ""),
          );
          refreshQueue();
        })
        .catch(function (err) {
          if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
          }
          setIsfRowState(
            form,
            "vol-dot-err",
            err.detail || err.message || t("vol_isf_build_failed", "ISF build request failed"),
          );
          if (restoreFocus && document.activeElement === document.body) btn.focus();
        });
    });
  });
}

export function wireIsfUploadButtons(onUploaded = () => Promise.resolve()) {
  document.querySelectorAll("[data-upload-isf-target]").forEach(function (btn) {
    var row = btn.closest(".vol-isfrow");
    var input = row ? row.querySelector("[data-upload-isf-input]") : null;
    if (!input) return;
    btn.addEventListener("click", function () {
      input.click();
    });
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      var targetId = btn.getAttribute("data-target-id") || "";
      var scenarioId = btn.getAttribute("data-scenario-id") || "";
      var originalText = btn.textContent;
      var restoreFocus = document.activeElement === btn;
      var fd = new FormData();
      fd.append("file", file);
      if (scenarioId) fd.append("scenario_id", scenarioId);
      btn.disabled = true;
      btn.textContent = t("vol_uploading");
      fetch("/api/v1/volatility/targets/" + encodeURIComponent(targetId) + "/isf/upload", {
        method: "POST",
        body: fd,
        headers: csrfHeaders("POST", { Accept: "application/json", "X-Requested-With": "fetch" }),
      })
        .then(function (r) {
          return r
            .json()
            .catch(function () {
              return {};
            })
            .then(function (body) {
              if (!r.ok) {
                var err = new Error(body.detail || "ISF upload failed");
                err.detail = body.detail;
                throw err;
              }
              return body;
            });
        })
        .then(async function (body) {
          btn.disabled = false;
          btn.textContent = originalText;
          setIsfRowState(
            btn,
            "vol-dot-ok",
            "✓ " + t("vol_built"),
            body.url || (body.job_id ? "/jobs/" + body.job_id : ""),
          );
          refreshQueue();
          await onUploaded();
          if (restoreFocus && document.activeElement === document.body) btn.focus();
        })
        .catch(function (err) {
          btn.disabled = false;
          btn.textContent = originalText;
          setIsfRowState(btn, "vol-dot-err", err.detail || err.message || "ISF upload failed");
          if (restoreFocus && document.activeElement === document.body) btn.focus();
        });
    });
  });
}

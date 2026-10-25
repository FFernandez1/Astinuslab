(function () {
  function wireControls() {
    var section = document.getElementById("live-section");
    if (!section) return;
    var headingStatus = document.querySelector("[data-live-job-status]");
    var liveStatus = section.querySelector("[data-live-status-label]");
    if (headingStatus && liveStatus) headingStatus.textContent = liveStatus.textContent;
    var RESET_CONFIRM = section.dataset.resetConfirm;
    var CANCEL_CONFIRM = section.dataset.cancelConfirm;
    var JOB_ID = section.dataset.jobId;
    var askConfirm =
      window.confirmDialog ||
      function () {
        return Promise.resolve(false);
      };
    var showToast = window.showToast || function () {};
    // csrf.js is a module and runs after this deferred script, so the helper is
    // read per request rather than captured here. Without the header every
    // unsafe method is refused by the app-wide CSRF dependency.
    var csrf = function (method, headers) {
      return window.csrfHeaders ? window.csrfHeaders(method, headers) : headers || {};
    };
    var t =
      window.t ||
      function (key, fallback) {
        return fallback != null ? fallback : key;
      };
    var say = function (key, vars, fallback) {
      return String(t(key, fallback)).replace(/\{(\w+)\}/g, function (_, name) {
        return vars && vars[name] != null ? vars[name] : "{" + name + "}";
      });
    };

    var btnReset = document.getElementById("btn-reset-lab-" + JOB_ID);
    if (btnReset && !btnReset.dataset.wired) {
      btnReset.dataset.wired = "1";
      btnReset.addEventListener("click", function () {
        askConfirm(RESET_CONFIRM).then(function (ok) {
          if (!ok) return;
          fetch("/jobs/" + JOB_ID + "/reset-lab", {
            method: "POST",
            headers: csrf("POST", { Accept: "application/json" }),
          })
            .then(function (r) {
              if (r.ok) {
                window.location.reload();
                return;
              }
              // The body carries the server's explanation, and the old code
              // threw the status away before reading it -- so a refusal an
              // operator could have acted on arrived as "Error: 409".
              return r
                .json()
                .catch(function () {
                  return {};
                })
                .then(function (body) {
                  throw new Error(
                    body.detail || say("job_reset_failed", null, "Could not reset the lab."),
                  );
                });
            })
            .catch(function (err) {
              showToast(
                err.message || say("job_reset_failed", null, "Could not reset the lab."),
                "error",
              );
            });
        });
      });
    }

    var btnCancelLab = document.getElementById("btn-cancel-lab-" + JOB_ID);
    if (btnCancelLab && !btnCancelLab.dataset.wired) {
      btnCancelLab.dataset.wired = "1";
      btnCancelLab.addEventListener("click", function () {
        askConfirm(CANCEL_CONFIRM).then(function (ok) {
          if (!ok) return;
          document.getElementById("cancel-" + JOB_ID).submit();
        });
      });
    }

    // ── Manual memory-dump override ──
    var overrideCard = document.getElementById("manual-override-" + JOB_ID);
    if (overrideCard && !overrideCard.dataset.wired) {
      overrideCard.dataset.wired = "1";
      var statusEl = overrideCard.querySelector("[data-upload-status]");
      var showStatus = function (msg, isError) {
        if (!statusEl) return;
        statusEl.hidden = false;
        statusEl.textContent = msg;
        statusEl.classList.toggle("text-danger", Boolean(isError));
        statusEl.classList.toggle("text-success", !isError);
      };
      var uploadFile = function (url, file, label) {
        var fd = new FormData();
        fd.append("file", file);
        showStatus(say("job_upload_uploading", { item: label }, "Uploading {item}…"), false);
        fetch(url, { method: "POST", body: fd, headers: csrf("POST") })
          .then(function (r) {
            return r
              .json()
              .catch(function () {
                return {};
              })
              .then(function (body) {
                if (!r.ok)
                  throw new Error(
                    body.detail ||
                      say("job_upload_failed", { item: label }, "{item} upload failed"),
                  );
                return body;
              });
          })
          .then(function () {
            showStatus(
              say("job_upload_stored", { item: label }, "{item} stored — reloading…"),
              false,
            );
            window.location.reload();
          })
          .catch(function (err) {
            showStatus(
              err.message || say("job_upload_failed", { item: label }, "{item} upload failed"),
              true,
            );
          });
      };
      var wireUpload = function (btnSel, inputSel, url, label) {
        var btn = overrideCard.querySelector(btnSel);
        var input = overrideCard.querySelector(inputSel);
        if (!btn || !input) return;
        btn.addEventListener("click", function () {
          input.click();
        });
        input.addEventListener("change", function () {
          if (input.files && input.files[0]) uploadFile(url, input.files[0], label);
          input.value = "";
        });
      };
      wireUpload(
        "[data-upload-dump]",
        "[data-upload-dump-input]",
        "/api/v1/jobs/" + JOB_ID + "/dump/upload",
        say("job_upload_item_dump", null, "Memory dump"),
      );
    }
  }
  // Script-runner file picker. The runner fragment re-swaps on a poll, so this
  // is delegated on document rather than bound to the input: a swapped-in
  // fragment cannot carry an inline script the host page's CSP would accept.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || input.id !== "scripts-upload") return;
    var label = document.getElementById("file-label");
    if (!label) return;
    var files = input.files || [];
    if (files.length === 0) {
      label.textContent = label.dataset.labelNone || "";
    } else if (files.length === 1) {
      label.textContent = files[0].name;
    } else {
      label.textContent = (label.dataset.labelMany || "{n}").replace("{n}", files.length);
    }
  });

  // Other deferred scripts provide confirmation, toast and CSRF support.
  if (document.readyState !== "complete") {
    document.addEventListener("DOMContentLoaded", wireControls, { once: true });
  } else {
    wireControls();
  }
  document.addEventListener("htmx:afterSwap", wireControls);
})();

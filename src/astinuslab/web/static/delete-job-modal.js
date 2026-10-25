// Delete-job confirmation modal.
//
// The modal markup ships inside partials/delete_job_modal.html, which is
// included by the #jobs-table fragment -- a region htmx re-swaps every 5s. An
// inline <script> there would carry the fragment response's CSP nonce, which
// never matches the host page's, so the browser blocks it and the freshly
// swapped buttons stay unbound. Everything here is therefore a document-level
// delegated listener: it is bound once per page load and keeps working against
// whatever overlay nodes the latest swap produced.
(function () {
  var jobToDelete = null;
  var rowToTarget = null;
  var isBulkDelete = false;
  var redirectAfterDelete = false;

  function overlay() {
    return document.getElementById("delete-job-modal-overlay");
  }

  function openModal() {
    var el = overlay();
    if (!el) return;
    var checkbox = document.getElementById("delete-files-checkbox");
    if (checkbox) checkbox.checked = false;
    window.AlDialog.open(el, {
      openClass: "modal-overlay--open",
      initial: "#delete-files-checkbox",
    });
  }

  function openDeleteJobModal(jobId, targetId, options) {
    jobToDelete = jobId;
    rowToTarget = targetId;
    isBulkDelete = false;
    redirectAfterDelete = (options && options.redirect) || false;
    openModal();
  }

  function openBulkDeleteModal() {
    if (!document.querySelectorAll(".job-select:checked").length) return;
    isBulkDelete = true;
    redirectAfterDelete = false;
    openModal();
  }

  window.closeDeleteJobModal = function () {
    var el = overlay();
    if (!el) return;
    if (window.AlDialog && window.AlDialog.current === el) {
      window.AlDialog.close();
    } else {
      el.classList.remove("modal-overlay--open");
      el.setAttribute("aria-hidden", "true");
    }
  };

  function confirmDelete() {
    var checkbox = document.getElementById("delete-files-checkbox");
    var deleteFiles = Boolean(checkbox && checkbox.checked);
    window.closeDeleteJobModal();

    if (isBulkDelete) {
      var form = document.getElementById("bulk-delete-form");
      if (!form) return;
      var existing = form.querySelector('input[name="delete_files"]');
      if (existing) existing.remove();
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "delete_files";
      input.value = deleteFiles ? "true" : "false";
      form.appendChild(input);
      form.submit();
      return;
    }

    if (!jobToDelete) return;
    if (redirectAfterDelete) {
      var redirectForm = document.createElement("form");
      redirectForm.method = "POST";
      redirectForm.action =
        "/jobs/" +
        encodeURIComponent(jobToDelete) +
        "/delete?delete_files=" +
        deleteFiles +
        "&redirect=/";
      var token = document.querySelector('meta[name="astinuslab-csrf-token"]');
      if (token && token.content) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "csrf_token";
        hidden.value = token.content;
        redirectForm.appendChild(hidden);
      }
      document.body.appendChild(redirectForm);
      redirectForm.submit();
      return;
    }
    window.htmx.ajax(
      "POST",
      "/jobs/" + encodeURIComponent(jobToDelete) + "/delete?delete_files=" + deleteFiles,
      {
        target: rowToTarget,
        swap: "outerHTML",
      },
    );
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") return;

    if (target.closest('[data-action="close-delete-job-modal"]')) {
      window.closeDeleteJobModal();
      return;
    }
    if (target.closest("#confirm-delete-btn")) {
      confirmDelete();
      return;
    }
    if (target.closest("[data-open-bulk-delete-modal]")) {
      openBulkDeleteModal();
      return;
    }
    var deleteButton = target.closest("[data-delete-job-id]");
    if (deleteButton) {
      openDeleteJobModal(
        deleteButton.dataset.deleteJobId,
        deleteButton.dataset.deleteJobRow || null,
        { redirect: deleteButton.dataset.deleteRedirect === "true" },
      );
    }
  });
})();

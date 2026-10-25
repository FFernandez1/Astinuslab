// Analysis queue table: status dot rendering and periodic refresh.
import { t, $, el, escapeHtml } from "./utils.js";
import { getJSON } from "./api.js";

function statusDot(s) {
  var cls = s === "completed" ? "ok" : s === "running" || s === "queued" ? "run" : "fail";
  return (
    '<span style="display:inline-flex;align-items:center;gap:6px;"><span class="vol-status-dot ' +
    cls +
    '"></span>' +
    escapeHtml(s || "—") +
    "</span>"
  );
}

export function refreshQueue() {
  return getJSON("/api/v1/volatility/queue")
    .then(function (data) {
      var tb = $("queue-body");
      tb.innerHTML = "";
      var runs = data.runs || [];
      var countEl = $("vol-activity-count");
      if (countEl) countEl.textContent = runs.length;
      if (!runs.length) {
        var tr = el("tr");
        var td = el("td", "vol-mini-empty", t("vol_queue_empty"));
        td.colSpan = 7;
        tr.appendChild(td);
        tb.appendChild(tr);
        return;
      }
      runs.forEach(function (q) {
        var tr = el("tr");
        var fin = q.finished_at ? q.finished_at.replace("T", " ").slice(11, 19) : "—";
        tr.innerHTML =
          '<td class="mono" style="color:var(--text-muted)">' +
          escapeHtml(q.run_id || "—") +
          "</td>" +
          '<td class="mono" style="color:var(--c-info)">' +
          escapeHtml(q.plugin || "—") +
          "</td>" +
          "<td>" +
          escapeHtml(q.dump_file || "—") +
          "</td>" +
          "<td>" +
          statusDot(q.status) +
          "</td>" +
          '<td class="mono">' +
          escapeHtml(q.elapsed || "—") +
          "</td>" +
          '<td class="mono" style="color:var(--text-muted)">' +
          fin +
          "</td>" +
          '<td class="vol-col-actions"><button type="button" class="vol-queue-del" ' +
          'data-job="' +
          escapeHtml(q.job_id || "") +
          '" ' +
          'data-run="' +
          escapeHtml(q.run_id || "") +
          '" ' +
          'title="' +
          escapeHtml(t("vol_queue_delete")) +
          '">✕</button></td>';
        tb.appendChild(tr);
      });
    })
    .catch(function () {
      /* queue is best-effort */
    });
}

// Capture console — revealed when ui.stream becomes non-null.
import { subscribe, setState } from "./state.js";
function $(id) {
  return document.getElementById(id);
}
function render(stream) {
  const section = $("capture-section"),
    log = $("stream-log"),
    vmEl = $("stream-vm"),
    kindEl = $("stream-kind");
  if (!section || !log || !vmEl || !kindEl) return;
  if (!stream) {
    section.hidden = true;
    log.textContent =
      "idle · select a running host and choose Capture RAM or Capture Disk to start an Acquisition. Phases: preparing → dumping → hashing → publishing → ready.";
    vmEl.textContent = "-";
    kindEl.textContent = "-";
    return;
  }
  section.hidden = false;
  kindEl.textContent = stream.kind;
  vmEl.textContent = stream.vmLabel || stream.vmId;
  log.textContent = (stream.lines || []).join("\n");
  log.scrollTop = log.scrollHeight;
}
export function mountCapture() {
  const head = document.querySelector("#capture-section .pg-panel__head");
  if (head && !head.querySelector(".pg-capture-close")) {
    const close = document.createElement("button");
    close.className = "pg-capture-close";
    close.type = "button";
    close.setAttribute("aria-label", "Close capture console");
    close.textContent = "×";
    close.addEventListener("click", () =>
      setState((prev) => ({ ...prev, ui: { ...prev.ui, stream: null } })),
    );
    head.appendChild(close);
  }
  subscribe(
    (s) => s.ui.stream,
    (stream) => render(stream),
  );
}

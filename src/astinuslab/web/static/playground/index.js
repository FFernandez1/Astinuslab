// Playground page controller. Mounts components into pre-existing DOM anchors in playground.html, owns global event wiring, kicks off the session poll loop.
import { getCatalogImages } from "./api.js";
import { getState, setState, subscribe, restoreCleanup } from "./state.js";
import { startSessionPoll, stopSessionPoll } from "./pollers.js";
// ESM imports stay unversioned: the /static mount sends no-cache for them, so a
// deploy is picked up on the next load. See web/static_cache.py.
import { mountTop } from "./render-top.js";
import { mountMap } from "./render-map.js";
import { mountDetails } from "./render-details.js";
import { mountCapture } from "./render-capture.js";
import { mountCreateNetwork } from "./render-modal.js";
import { mountCreateVm } from "./wizard.js";
import { mountAlerts } from "./render-alerts.js";
async function loadCatalog(role) {
  setState((prev) => ({ ...prev, catalogStatus: { ...prev.catalogStatus, [role]: "loading" } }));
  try {
    const images = await getCatalogImages(role);
    setState((prev) => ({
      ...prev,
      catalog: { ...prev.catalog, [role]: images },
      catalogStatus: { ...prev.catalogStatus, [role]: images.length ? "loaded" : "loaded-empty" },
    }));
  } catch (_) {
    setState((prev) => ({ ...prev, catalogStatus: { ...prev.catalogStatus, [role]: "failed" } }));
  }
}
function wireRootState() {
  subscribe(
    (s) => ({
      sessionId: s.session?.job_id || s.session?.id || null,
      cleanupId: s.cleanup?.session_id,
      vmCount: s.session?.vms?.length || 0,
    }),
    ({ sessionId, cleanupId }) => {
      const root = document.getElementById("playground-root");
      if (root) root.dataset.state = cleanupId ? "cleanup" : sessionId ? "active" : "idle";
      const label = document.querySelector(".pg-header-label");
      if (label)
        label.textContent =
          cleanupId || sessionId ? `Playground · ${cleanupId || sessionId}` : "Playground";
    },
  );
}
function boot() {
  restoreCleanup();
  window.addEventListener("pagehide", stopSessionPoll);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) startSessionPoll();
  });
  wireRootState();
  mountAlerts();
  for (const role of ["target", "attacker", "network"]) loadCatalog(role);
  startSessionPoll();
  mountTop();
  mountMap();
  mountDetails();
  mountCapture();
  mountCreateNetwork();
  mountCreateVm((role) => {
    if (getState().catalogStatus[role] !== "loading") loadCatalog(role);
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

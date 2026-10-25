import { definitionFromSession } from "./api.js";

const tt = (key, fallback) => globalThis.window?.t?.(key, fallback) || fallback;

export function wireCreatorHandoff() {
  const byId = (id) => document.getElementById(id);
  const form = byId("creator-load-form");
  if (!form) return;
  const button = byId("creator-load-button");
  const feedback = byId("creator-load-status");
  const definition = byId("creator-definition");
  const sourceSession = byId("creator-authored-session");
  let generation = 0;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = ++generation;
    const before = [definition.value, sourceSession.value];
    button.disabled = true;
    feedback.textContent = tt("pkg_definition_loading", "Loading current Playground…");
    feedback.className = "text-muted small";
    try {
      const payload = Object.fromEntries(
        ["package_id", "version", "title", "description", "creator", "license"].map((name) => [
          name,
          form.elements.namedItem(name)?.value || "",
        ]),
      );
      const result = await definitionFromSession(payload);
      if (token !== generation) return;
      if (before[0] !== definition.value || before[1] !== sourceSession.value) {
        feedback.textContent = tt(
          "pkg_definition_changed",
          "Your draft changed while loading. Load again to replace it.",
        );
        return;
      }
      definition.value = JSON.stringify(result.definition, null, 2);
      sourceSession.value = result.source_session_id;
      feedback.textContent = tt(
        "pkg_definition_loaded",
        "Definition loaded. Review the editable JSON and resource estimates below before verification.",
      );
    } catch (error) {
      if (token !== generation) return;
      feedback.className = "form-error";
      feedback.textContent = error?.message || tt("pkg_request_failed", "Package request failed");
    } finally {
      if (token === generation) button.disabled = false;
    }
  });
}

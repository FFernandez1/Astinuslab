import test from "node:test";
import assert from "node:assert/strict";
import { install, deferred, flush, Node } from "./page-harness.mjs";
import {
  renderVerification,
  renderVerificationHistory,
} from "../../../../src/astinuslab/web/static/packages/render-verification.js";
import { badgeVariant } from "../../../../src/astinuslab/web/static/packages/render-library.js";

for (const state of ["verified", "failed", "cancelled", "reserved", "running"]) {
  test(`verification ${state} has faithful DOM children and appropriate cancellation`, (t) => {
    const { ids } = install(t);
    const panel = ids["verification-status"];
    renderVerification(panel, { id: "v1", state }, () => {});
    assert.equal(
      panel.children.some((child) => child.tagName === "#TEXT" && child.textContent === "null"),
      false,
    );
    assert.equal(
      Boolean(panel.querySelector("[data-verification-cancel]")),
      ["reserved", "running"].includes(state),
    );
    const id = panel.querySelector("code");
    assert.equal(id.textContent, "v1");
    assert.equal(id.tabIndex, 0);
    assert.ok(id.getAttribute("aria-label"));
    assert.ok(panel.querySelector("[data-copy-verification-id]").getAttribute("aria-label"));
  });
}

for (const outcome of ["copied", "failed"]) {
  test(`clipboard ${outcome} survives same-ID replacement and updates accessible translated feedback`, async (t) => {
    const { ids, click } = install(t);
    const pending = deferred();
    globalThis.navigator.clipboard.writeText = () => pending.promise;
    globalThis.window.t = (key, fallback) =>
      ({ pkg_copy_succeeded: "Identifiant copié", pkg_copy_failed: "Échec de copie" })[key] ||
      fallback;
    const panel = ids["verification-status"];
    renderVerification(panel, { id: "v1", state: "running" });
    await click("verification-status", "[data-copy-verification-id]");
    const oldButton = panel.querySelector("button");
    renderVerification(panel, { id: "v1", state: "verified" });
    if (outcome === "copied") pending.resolve();
    else pending.reject(new Error("clipboard refused"));
    await flush();
    assert.notEqual(panel.querySelector("button"), oldButton);
    assert.equal(panel.querySelector("[data-copy-verification-id]").dataset.copyState, outcome);
    const feedback = panel.querySelector("[role=status]");
    assert.equal(
      feedback.textContent,
      outcome === "copied" ? "Identifiant copié" : "Échec de copie",
    );
    renderVerification(panel, { id: "v1", state: "verified" });
    assert.equal(panel.querySelector("[role=status]").textContent, feedback.textContent);
    renderVerification(panel, { id: "v2", state: "running" });
    assert.equal(panel.querySelector("[role=status]").textContent, "");
    assert.equal(panel.querySelector("[data-copy-verification-id]").dataset.copyState || "", "");
  });
}

test("late clipboard completion for another ID cannot contaminate visible feedback", async (t) => {
  const { ids, click } = install(t);
  const pending = deferred();
  globalThis.navigator.clipboard.writeText = () => pending.promise;
  const panel = ids["verification-status"];
  renderVerification(panel, { id: "old", state: "running" });
  await click("verification-status", "[data-copy-verification-id]");
  renderVerification(panel, { id: "new", state: "running" });
  pending.reject(new Error("old failure"));
  await flush();
  assert.equal(panel.querySelector("[role=status]").textContent, "");
  assert.equal(panel.querySelector("code").textContent, "new");
});

test("cancel delegates the selected verification ID", async (t) => {
  const { ids, click } = install(t);
  const seen = [];
  renderVerification(ids["verification-status"], { id: "v1", state: "running" }, (id) =>
    seen.push(id),
  );
  await click("verification-status", "[data-verification-cancel]");
  assert.deepEqual(seen, ["v1"]);
});

test("DOM harness models null string conversion", () => {
  const node = new Node();
  node.replaceChildren(null);
  assert.equal(node.textContent, "null");
});

const verified = (id) => ({
  id,
  state: "verified",
  package_id: "demo",
  package_title: "Demo lab",
  package_version: "1.0",
  attestation_status: "unexpired",
  attestation_expires_at: "2099-01-01T00:00:00Z",
});

test("export metadata changes in place and expired or unavailable rows are read-only", (t) => {
  const { ids } = install(t);
  const panel = ids["verification-history"];
  renderVerificationHistory(panel, [
    verified("first"),
    { ...verified("second"), package_version: "2.0" },
    { ...verified("expired"), attestation_status: "expired" },
  ]);
  assert.match(panel.textContent, /Demo lab.*demo@1.0/);
  assert.match(panel.textContent, /demo@2.0/);
  assert.match(panel.textContent, /Export until 2099-01-01T00:00:00Z/);
  assert.match(panel.textContent, /Attestation expired/);
  assert.equal(panel.querySelector("[data-verification-export=expired]").disabled, true);
  const row = panel.querySelector("[data-verification-row=first]");
  panel.querySelector("[data-export-embed=first]").checked = true;
  renderVerificationHistory(panel, [{ ...verified("first"), attestation_status: "unavailable" }]);
  assert.equal(panel.querySelector("[data-verification-row=first]"), row);
  assert.equal(panel.querySelector("[data-export-embed=first]").checked, true);
  assert.equal(panel.querySelector("[data-verification-export=first]").disabled, true);
  assert.match(panel.textContent, /Attestation unavailable/);
});

test("an open row expires without rerender or losing consent, and never sends an export", async (t) => {
  const h = install(t);
  let now = Date.parse("2026-09-06T10:00:00Z");
  t.mock.method(Date, "now", () => now);
  const calls = [];
  const panel = h.ids["verification-history"];
  renderVerificationHistory(
    panel,
    [{ ...verified("soon"), attestation_expires_at: "2026-09-06T10:00:01Z" }],
    (id) => calls.push(id),
  );
  panel.querySelector("[data-export-embed=soon]").checked = true;
  now += 1001;
  await h.advance(1001);
  assert.equal(panel.querySelector("[data-verification-export=soon]").disabled, true);
  assert.equal(panel.querySelector("[data-export-embed=soon]").checked, true);
  await h.click("verification-history", "[data-verification-export=soon]");
  assert.deepEqual(calls, []);
});

test("authoritative refusal is shown beside the same export row", async (t) => {
  const h = install(t);
  renderVerificationHistory(h.ids["verification-history"], [verified("refused")], () => ({
    error: "embedding_required",
  }));
  await h.click("verification-history", "[data-verification-export=refused]");
  assert.match(h.ids["verification-history"].textContent, /embedding_required/);
  assert.equal(
    h.ids["verification-history"].querySelector("[data-export-embed=refused]").checked,
    false,
  );
});

test("failure reason is literal text and unchanged status preserves controls", (t) => {
  const { ids } = install(t);
  const panel = ids["verification-status"];
  const record = {
    id: "failure",
    state: "failed",
    error_summary: "<script>private-free reason</script>",
  };
  renderVerification(panel, record);
  assert.match(panel.textContent, /Verification failed/);
  assert.match(panel.textContent, /<script>private-free reason<\/script>/);
  assert.equal(panel.querySelector("script"), null);
  const copy = panel.querySelector("button");
  renderVerification(panel, { ...record });
  assert.equal(panel.querySelector("button"), copy);
  renderVerification(panel, { id: "failure", state: "failed" });
  assert.match(panel.textContent, /without a detailed reason/);
  renderVerification(panel, { id: "failure", state: "cancelled" });
  assert.match(panel.textContent, /Verification cancelled/);
  assert.doesNotMatch(panel.textContent, /without a detailed reason/);
});

test("an empty history explains itself in the page language", (t) => {
  const { ids } = install(t);
  globalThis.window.t = (key, fallback) =>
    key === "pkg_verification_history_empty" ? "Aucune vérification terminée." : fallback;
  const panel = ids["verification-history"];
  renderVerificationHistory(panel, [], () => {});
  assert.match(panel.textContent, /Aucune vérification terminée/);
  renderVerificationHistory(panel, undefined, () => {});
  assert.match(panel.textContent, /Aucune vérification terminée/);
});

test("row identity and ticked consent survive re-renders driven by unrelated attempts", (t) => {
  const { ids } = install(t);
  const panel = ids["verification-history"];
  const done = verified("v-done");
  renderVerificationHistory(panel, [done], () => {});
  const row = panel.querySelector("[data-verification-row=v-done]");
  panel.querySelector("[data-export-embed=v-done]").checked = true;
  // A poll on some other verification re-renders the history; consent is the
  // creator's, not the render pass's.
  renderVerificationHistory(panel, [done], () => {});
  assert.equal(panel.querySelector("[data-verification-row=v-done]"), row);
  assert.equal(panel.querySelector("[data-export-embed=v-done]").checked, true);
  renderVerificationHistory(panel, [done, verified("v-new")], () => {});
  assert.equal(panel.children.length, 2);
  assert.equal(panel.querySelector("[data-export-embed=v-done]").checked, true);
  // A record that leaves the feed takes its row with it.
  renderVerificationHistory(panel, [verified("v-new")], () => {});
  assert.equal(panel.querySelector("[data-verification-row=v-done]"), null);
  assert.equal(panel.children.length, 1);
});

test("export forwards this row's consent flags and defaults both to withheld", async (t) => {
  const { ids, click } = install(t);
  const panel = ids["verification-history"];
  const calls = [];
  renderVerificationHistory(panel, [verified("v1"), verified("v2")], (id, options) =>
    calls.push([id, options]),
  );
  panel.querySelector("[data-export-embed=v1]").checked = true;
  panel.querySelector("[data-export-confirm=v1]").checked = true;
  await click("verification-history", "[data-verification-export=v1]");
  await click("verification-history", "[data-verification-export=v2]");
  assert.deepEqual(calls, [
    ["v1", { embed_assets: true, redistribution_confirmed: true }],
    ["v2", { embed_assets: false, redistribution_confirmed: false }],
  ]);
  assert.ok(panel.querySelector("[data-verification-export=v1]").getAttribute("aria-label"));
});

test("an in-flight export disables only its own control and stays retryable", async (t) => {
  const { ids, click } = install(t);
  const panel = ids["verification-history"];
  const pending = [];
  const onExport = () => {
    const gate = deferred();
    pending.push(gate);
    return gate.promise;
  };
  renderVerificationHistory(panel, [verified("v1"), verified("v2")], onExport);
  await click("verification-history", "[data-verification-export=v1]");
  assert.equal(pending.length, 1);
  assert.equal(panel.querySelector("[data-verification-export=v1]").disabled, true);
  assert.equal(panel.querySelector("[data-verification-export=v1]").dataset.exportState, "running");
  // Exports are per attempt: another verified record stays exportable.
  assert.equal(panel.querySelector("[data-verification-export=v2]").disabled, false);
  await click("verification-history", "[data-verification-export=v1]");
  assert.equal(pending.length, 1, "a second click must not build the archive twice");
  // The page reports refusals itself, so a completed request always resolves.
  pending[0].resolve();
  await flush();
  assert.equal(panel.querySelector("[data-verification-export=v1]").disabled, false);
  await click("verification-history", "[data-verification-export=v1]");
  assert.equal(pending.length, 2);
});

test("an export in flight survives a re-render and still refuses a duplicate", async (t) => {
  const { ids, click } = install(t);
  const panel = ids["verification-history"];
  const pending = [];
  const record = verified("v1");
  renderVerificationHistory(panel, [record], () => {
    const gate = deferred();
    pending.push(gate);
    return gate.promise;
  });
  await click("verification-history", "[data-verification-export=v1]");
  renderVerificationHistory(panel, [{ ...record, attestation_status: "expired" }], () => {
    throw new Error("a re-render must not rebind an export already in flight");
  });
  assert.equal(panel.querySelector("[data-verification-export=v1]").disabled, true);
  await click("verification-history", "[data-verification-export=v1]");
  assert.equal(pending.length, 1);
  pending[0].resolve();
  await flush();
  assert.equal(panel.querySelector("[data-verification-export=v1]").disabled, true);
});

test("badgeVariant maps every package status onto a shared badge variant", () => {
  assert.equal(badgeVariant("trusted"), "wiz-badge--success");
  assert.equal(badgeVariant("valid"), "wiz-badge--success");
  assert.equal(badgeVariant("ready"), "wiz-badge--success");
  assert.equal(badgeVariant("unknown"), "wiz-badge--warning");
  assert.equal(badgeVariant("untrusted"), "wiz-badge--warning");
  assert.equal(badgeVariant("invalid"), "wiz-badge--danger");
  assert.equal(badgeVariant("blocked"), "wiz-badge--danger");
  assert.equal(badgeVariant("failed"), "wiz-badge--danger");
  assert.equal(
    badgeVariant("verified"),
    "wiz-badge--success",
    "the creator flow's own success state",
  );
  assert.equal(badgeVariant("cancelled"), "wiz-badge--neutral");
  assert.equal(badgeVariant("TRUSTED"), "wiz-badge--success", "status comparison ignores case");
  assert.equal(badgeVariant(null), "wiz-badge--neutral");
  assert.equal(badgeVariant("running"), "wiz-badge--neutral", "an in-flight state is neutral");
});

test("history rows are reused across renders and dropped when their record goes", (t) => {
  const { ids } = install(t);
  const container = ids["verification-history"];
  const record = {
    id: "ver-1",
    package_title: "Lab one",
    package_id: "org.example.lab",
    package_version: "1.0.0",
    attestation_status: "unexpired",
    attestation_expires_at: new Date(Date.now() + 600000).toISOString(),
  };

  renderVerificationHistory(container, [record], async () => ({}));
  const first = container.querySelector("[data-verification-row=ver-1]");
  assert.ok(first, "the row renders");
  assert.equal(first.className, "package-verification-row");

  renderVerificationHistory(container, [{ ...record }], async () => ({}));
  assert.equal(
    container.querySelector("[data-verification-row=ver-1]"),
    first,
    "an unchanged list reuses the same node rather than blurring the creator's focus",
  );

  renderVerificationHistory(container, [], async () => ({}));
  assert.equal(container.querySelector("[data-verification-row=ver-1]"), null);
  assert.equal(
    container.children[0].className,
    "text-muted small",
    "the empty state uses the shared treatment",
  );
});

// Review Focus 5: index.js moves focus to this heading after "Open package".
// Nothing asserted it, so a renderer that promoted the h3 to an h2, or dropped
// the tabindex, would leave every suite green and keyboard focus stuck.
test("the package detail heading stays a focusable h3 for the focus hop", async (t) => {
  const { ids } = install(t);
  const { renderPackageExperience } =
    await import("../../../../src/astinuslab/web/static/packages/render-inspection.js");
  const panel = ids["package-detail"];
  renderPackageExperience(panel, {
    title: "Lab one",
    package_id: "org.example.lab",
    version: "1.0.0",
    runbook: "# Steps",
    experience: {},
    resources: {},
  });

  const heading = panel.querySelector("h3");
  assert.ok(heading, "index.js focuses byId('package-detail').querySelector('h3')");
  assert.equal(
    heading.getAttribute("tabindex"),
    "-1",
    "a heading without tabindex cannot take focus",
  );
});

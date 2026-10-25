import test from "node:test";
import assert from "node:assert/strict";
import { install, flush, response, deferred } from "./page-harness.mjs";
import { getState, resetState } from "../../../../src/astinuslab/web/static/packages/state.js";

let generation = 0;
const empty = { prepares: [], instances: [], verifications: [] };
const pkg = (id) => ({ id, package_id: id, version: "1.0", title: id });
const preparation = (id, state = "running", packageId = "p1") => ({
  id,
  state,
  package_record_id: packageId,
  progress_percent: 12,
  assets: [],
});
const instance = (id, state = "provisioning", packageId = "p1") => ({
  id,
  state,
  package_record_id: packageId,
});
const verification = (id, state = "running") => ({ id, state, progress_percent: 12 });
const experience = (id) => ({
  ...pkg(id),
  package_record_id: id,
  runbook: `# Instructions for ${id}`,
  experience: {},
  resources: {},
});
async function page(t, activity = empty, delayActivity = false) {
  const harness = install(t);
  resetState();
  await import(`../../../../src/astinuslab/web/static/packages/index.js?task7=${++generation}`);
  await flush();
  harness.take("").resolve(response([pkg("p1"), pkg("p2")]));
  if (!delayActivity) harness.take("/activity").resolve(response(activity));
  await flush();
  return harness;
}
// Select real rendered controls with one supported selector, then dispatch through page listener.
async function action(h, kind, packageId = "p1") {
  const panel = kind === "open" ? "package-library" : "package-detail";
  if (kind !== "open") {
    await action(h, "open", packageId);
    h.take(`/${packageId}/experience`).resolve(response(experience(packageId)));
    await flush();
  }
  const target = h.ids[panel]
    .querySelectorAll("[data-package-action]")
    .find((node) => node.dataset.packageAction === kind && node.dataset.recordId === packageId);
  assert.ok(target);
  void h.ids[panel].fire("click", target);
  await flush();
}
async function startPrepare(h, packageId, id, state = "running") {
  await action(h, "prepare", packageId);
  h.take(`/${packageId}/prepare`, "POST").resolve(response(preparation(id, state, packageId)));
  await flush();
}
async function startVerify(h, id) {
  void h.ids["creator-verify-form"].fire("submit");
  await flush();
  h.take("/drafts", "POST").resolve(response({ id: `draft-${id}` }));
  await flush();
  h.take("/verify-clean-run", "POST").resolve(response(verification(id)));
  await flush();
}

test("reopening package instructions fences older detail and keeps operation ownership", async (t) => {
  const h = await page(t, { ...empty, instances: [instance("i", "ready", "p1")] });
  await action(h, "open", "p1");
  const old = h.take("/p1/experience");
  await action(h, "open", "p2");
  h.take("/p2/experience").resolve(response(experience("p2")));
  await flush();
  old.resolve(response(experience("p1")));
  await flush();
  assert.equal(getState().selectedPackageId, "p2");
  assert.match(h.ids["package-detail"].textContent, /p2@1.0/);
  assert.doesNotMatch(h.ids["package-detail"].textContent, /p1@1.0/);
  assert.match(h.ids["package-operation-status"].textContent, /p1@1.0/);
  assert.equal(
    h.ids["package-library"].querySelectorAll("[data-package-action=prepare]").length,
    0,
  );
});

for (const matches of [true, false]) {
  test(`ready instance handoff requires the matching current session (${matches})`, async (t) => {
    const h = await page(t, {
      ...empty,
      instances: [{ ...instance("i", "ready"), session_id: "owned-session" }],
    });
    const navigated = [];
    globalThis.window.location = { assign: (path) => navigated.push(path) };
    await h.click("package-operation-status", "[data-instance-action=open]");
    h.take("/api/v1/playground/sessions/current-or-null").resolve(
      response({ session_id: matches ? "owned-session" : "unrelated" }),
    );
    await flush();
    assert.deepEqual(navigated, matches ? ["/playground"] : []);
    if (!matches)
      assert.match(h.ids["package-error"].textContent, /no longer the active Playground session/);
  });
}

test("selecting a historical failure fences a pending active response", async (t) => {
  const h = await page(t, {
    ...empty,
    verifications: [
      verification("done", "verified"),
      verification("active"),
      {
        ...verification("failed", "failed"),
        error_code: "capture_failed",
        error_summary: "Safe capture reason",
      },
    ],
  });
  assert.equal(getState().verification.id, "active");
  await h.advance(2000);
  const pending = h.take("/verifications/active");
  h.ids["verification-attempt"].value = "failed";
  await h.ids["verification-attempt"].fire("change");
  assert.match(h.ids["verification-status"].textContent, /Safe capture reason/);
  pending.resolve(response({ ...verification("active", "failed"), error_summary: "Stale reason" }));
  await flush();
  assert.equal(getState().verification.id, "failed");
  assert.doesNotMatch(h.ids["verification-status"].textContent, /Stale reason/);
  assert.equal(
    h.ids["verification-history"].querySelectorAll("[data-verification-export]").length,
    1,
  );
});

for (const accepted of [false, true]) {
  test(`reset confirmation ${accepted} is required and duplicate-safe`, async (t) => {
    const h = await page(t, { ...empty, instances: [instance("i", "ready")] });
    const gate = deferred();
    let dialogs = 0;
    globalThis.window.confirmDialog = (message) => {
      dialogs++;
      assert.match(message, /Current VM changes will be discarded/);
      assert.match(message, /Captured evidence is kept/);
      assert.match(message, /p1/);
      return gate.promise;
    };
    await h.click("package-operation-status", "[data-instance-action=reset]");
    await h.click("package-operation-status", "[data-instance-action=reset]");
    assert.equal(dialogs, 1);
    assert.equal(h.requests.filter((r) => r.path.endsWith("/reset")).length, 0);
    gate.resolve(accepted);
    await flush();
    await h.click("package-operation-status", "[data-instance-action=reset]");
    assert.equal(h.requests.filter((r) => r.path.endsWith("/reset")).length, accepted ? 1 : 0);
  });
}

test("changing the operation while reset confirmation is open sends no old mutation", async (t) => {
  const h = await page(t, { ...empty, instances: [instance("old", "ready")] });
  const gate = deferred();
  globalThis.window.confirmDialog = () => gate.promise;
  await h.click("package-operation-status", "[data-instance-action=reset]");
  await action(h, "preflight", "p2");
  h.take("/p2/preflight", "POST").resolve(
    response({ ready: true, warnings: [], warning_token: "new" }),
  );
  await flush();
  await h.click("package-action-status", "[data-warning-token]");
  gate.resolve(true);
  await flush();
  assert.equal(
    h.requests.some((r) => r.path === "/operations/old/reset"),
    false,
  );
});

test("actual boot loads durable activity alongside library and hydrates server-first panels", async (t) => {
  const h = await page(t, {
    prepares: [preparation("p-first", "completed"), preparation("p-other")],
    instances: [instance("i-first", "ready"), instance("i-other")],
    verifications: [verification("v-first", "verified"), verification("v-other")],
  });
  assert.match(h.ids["package-library"].textContent, /p1@1.0/);
  assert.deepEqual(Object.keys(getState().prepares), ["p-first", "p-other"]);
  assert.deepEqual(Object.keys(getState().operations), ["i-first", "i-other"]);
  assert.equal(getState().verification.id, "v-other");
  assert.match(h.ids["package-prepare-status"].textContent, /completed/);
  assert.match(h.ids["package-operation-status"].textContent, /i-first/);
  assert.match(h.ids["verification-status"].textContent, /v-other/);
  assert.doesNotMatch(h.ids["verification-status"].textContent, /null/);
  await h.advance(60000);
  assert.equal(h.requests.length, 3);
});

test("existing creator event renders a terminal verification without a null text child", async (t) => {
  // Intentionally permits the baseline's absent activity request: this RED proves existing page behavior.
  const h = await page(t, empty, true);
  const activity = h.requests.find((r) => r.path === "/activity");
  activity?.resolve(response(empty));
  void h.ids["creator-verify-form"].fire("submit");
  await flush();
  h.take("/drafts", "POST").resolve(response({ id: "draft" }));
  await flush();
  h.take("/verify-clean-run", "POST").resolve(response(verification("v-terminal", "verified")));
  await flush();
  assert.doesNotMatch(h.ids["verification-status"].textContent, /null/);
  await h.advance(2000);
  assert.equal(
    h.requests.some((r) => r.path === "/verifications/v-terminal"),
    false,
  );
});

test("rehydrated pollers retain durable state on transient failure, retry and stop terminals", async (t) => {
  const h = await page(t, {
    prepares: [preparation("p")],
    instances: [instance("i")],
    verifications: [verification("v")],
  });
  h.take("/prepare/p").resolve(response({ detail: "temporary" }, 503));
  h.take("/operations/i").resolve(response(instance("i", "ready")));
  h.take("/verifications/v").resolve(response(verification("v", "verified")));
  await flush();
  assert.equal(getState().prepares.p.state, "running");
  assert.match(h.ids["package-prepare-status"].textContent, /running/);
  assert.match(h.ids["package-error"].textContent, /temporary/);
  await h.advance(2000);
  h.take("/prepare/p").resolve(response(preparation("p", "completed")));
  await flush();
  await h.advance(30000);
  assert.equal(h.requests.filter((r) => r.path === "/prepare/p").length, 2);
  assert.equal(h.requests.filter((r) => r.path === "/operations/i").length, 1);
  assert.equal(h.requests.filter((r) => r.path === "/verifications/v").length, 1);
});

test("late boot cannot replace a prepare started while activity was loading", async (t) => {
  const h = await page(t, empty, true);
  await startPrepare(h, "p2", "new", "completed");
  h.take("/activity").resolve(response({ ...empty, prepares: [preparation("old", "failed")] }));
  await flush();
  assert.match(h.ids["package-prepare-status"].textContent, /completed/);
  assert.equal(getState().prepares.new.state, "completed");
});

test("prepare replacement fences abort-ignoring status, preserves single flight and ignores stale cancel", async (t) => {
  const h = await page(t);
  await startPrepare(h, "p1", "old");
  const oldStatus = h.take("/prepare/old");
  await h.click("package-prepare-status", "[data-prepare-cancel]");
  const cancel = h.take("/prepare/old/cancel", "POST");
  await startPrepare(h, "p2", "new");
  assert.equal(oldStatus.signal?.aborted, true);
  await h.advance(20000);
  assert.equal(
    h.requests.some((r) => r.path === "/prepare/new"),
    false,
  );
  oldStatus.resolve(response(preparation("old", "failed")));
  await flush();
  h.take("/prepare/new").resolve(response(preparation("new", "completed", "p2")));
  cancel.resolve(response(preparation("old", "cancelled")));
  await flush();
  assert.match(h.ids["package-prepare-status"].textContent, /completed/);
  assert.equal(getState().prepares.old.state, "running");
  await h.advance(10000);
  assert.equal(h.requests.filter((r) => r.path === "/prepare/new").length, 1);
});

test("older prepare action response and error cannot seize a newer prepare selection", async (t) => {
  const h = await page(t);
  await action(h, "prepare", "p1");
  const old = h.take("/p1/prepare", "POST");
  await startPrepare(h, "p2", "new", "completed");
  old.resolve(response(preparation("old", "failed")));
  await flush();
  assert.match(h.ids["package-prepare-status"].textContent, /completed/);
  await action(h, "prepare", "p1");
  const failure = h.take("/p1/prepare", "POST");
  await startPrepare(h, "p2", "newer", "completed");
  failure.reject(new Error("stale action error"));
  await flush();
  assert.doesNotMatch(h.ids["package-error"].textContent, /stale action error/);
  assert.equal(
    h.requests.some((r) => r.path === "/prepare/old"),
    false,
  );
});

test("verification replacement fences status and cancellation responses", async (t) => {
  const h = await page(t, { ...empty, verifications: [verification("old")] });
  const oldStatus = h.take("/verifications/old");
  await h.click("verification-status", "[data-verification-cancel]");
  const cancel = h.take("/verifications/old/cancel", "POST");
  await startVerify(h, "new");
  oldStatus.reject(new Error("stale status error"));
  cancel.resolve(response(verification("old", "cancelled")));
  await flush();
  h.take("/verifications/new").resolve(response(verification("new", "verified")));
  await flush();
  assert.equal(getState().verification.id, "new");
  assert.match(h.ids["verification-status"].textContent, /new/);
  assert.doesNotMatch(h.ids["package-error"].textContent, /stale status error/);
});

test("late draft response cannot start or replace newer verification", async (t) => {
  const h = await page(t);
  void h.ids["creator-verify-form"].fire("submit");
  await flush();
  const oldDraft = h.take("/drafts", "POST");
  await startVerify(h, "new");
  h.take("/verifications/new").resolve(response(verification("new", "verified")));
  oldDraft.resolve(response({ id: "old-draft" }));
  await flush();
  assert.equal(h.requests.filter((r) => r.path === "/verify-clean-run").length, 1);
  assert.equal(getState().verification.id, "new");
});

test("reset of a terminal instance resumes same-ID polling and preserves action ownership", async (t) => {
  const h = await page(t, { ...empty, instances: [instance("i", "ready")] });
  await h.click("package-operation-status", "[data-instance-action=reset]");
  h.take("/operations/i/reset", "POST").resolve(response(instance("i", "resetting")));
  await flush();
  const status = h.take("/operations/i");
  status.resolve(response(instance("i", "ready")));
  await flush();
  await h.click("package-operation-status", "[data-instance-action=reset]");
  h.take("/operations/i/reset", "POST").resolve(response(instance("i", "resetting")));
  await flush();
  h.take("/operations/i").resolve(response(instance("i", "ready")));
  await flush();
  assert.equal(h.requests.filter((r) => r.path === "/operations/i").length, 2);
});

test("deleting another package preserves selected work, deleting selected work stops polling", async (t) => {
  const h = await page(t, {
    ...empty,
    prepares: [preparation("p", "running", "p2")],
    instances: [instance("i", "ready", "p2")],
  });
  const pending = h.take("/prepare/p");
  await action(h, "delete", "p1");
  h.take("/p1", "DELETE").resolve(response(null, 204));
  await flush();
  assert.match(h.ids["package-operation-status"].textContent, /i/);
  assert.match(h.ids["package-prepare-status"].textContent, /running/);
  await action(h, "delete", "p2");
  h.take("/p2", "DELETE").resolve(response(null, 204));
  await flush();
  assert.equal(pending.signal.aborted, true);
  pending.resolve(response(preparation("p", "completed", "p2")));
  await flush();
  assert.deepEqual(getState().prepares, {});
  assert.match(h.ids["package-prepare-status"].textContent, /No package preparation selected/);
  await h.advance(30000);
  assert.equal(h.requests.filter((r) => r.path === "/prepare/p").length, 1);
});

test("late reset and launch status cannot replace a newer launched instance", async (t) => {
  const h = await page(t, { ...empty, instances: [instance("old", "ready")] });
  await h.click("package-operation-status", "[data-instance-action=reset]");
  const reset = h.take("/operations/old/reset", "POST");
  await action(h, "preflight", "p2");
  h.take("/p2/preflight", "POST").resolve(
    response({ ready: true, warnings: [], warning_token: "token" }),
  );
  await flush();
  await h.click("package-action-status", "[data-warning-token]");
  h.take("/p2/launch", "POST").resolve(response(instance("new", "provisioning", "p2")));
  await flush();
  reset.resolve(response(instance("old", "resetting")));
  h.take("/operations/new").resolve(response(instance("new", "ready", "p2")));
  await flush();
  assert.match(h.ids["package-operation-status"].textContent, /new/);
  assert.equal(getState().operations.old.state, "ready");
  assert.equal(
    h.requests.some((r) => r.path === "/operations/old"),
    false,
  );
});

test("late preflight response cannot steal the selected package and launch target", async (t) => {
  const h = await page(t);
  await action(h, "preflight", "p1");
  const old = h.take("/p1/preflight", "POST");
  await action(h, "preflight", "p2");
  h.take("/p2/preflight", "POST").resolve(
    response({ ready: true, warnings: [], warning_token: "new" }),
  );
  await flush();
  old.resolve(response({ ready: true, warnings: [], warning_token: "old" }));
  await flush();
  assert.equal(getState().selectedPackageId, "p2");
  await h.click("package-action-status", "[data-warning-token]");
  h.take("/p2/launch", "POST").resolve(response(instance("new", "ready", "p2")));
  await flush();
  assert.match(h.ids["package-operation-status"].textContent, /new/);
});

test("activity transport failure stays visible without erasing the loaded library", async (t) => {
  const h = await page(t, empty, true);
  h.take("/activity").reject(new Error("activity unavailable"));
  await flush();
  assert.match(h.ids["package-error"].textContent, /activity unavailable/);
  assert.match(h.ids["package-library"].textContent, /p1@1.0/);
});

test("failed prepare cancellation retains a usable panel and resumes eligible polling", async (t) => {
  const h = await page(t, { ...empty, prepares: [preparation("p")] });
  h.take("/prepare/p").resolve(response(preparation("p")));
  await flush();
  await h.click("package-prepare-status", "[data-prepare-cancel]");
  h.take("/prepare/p/cancel", "POST").reject(new Error("cancel unavailable"));
  await flush();
  assert.match(h.ids["package-error"].textContent, /cancel unavailable/);
  assert.equal(getState().prepares.p.state, "running");
  h.take("/prepare/p").resolve(response(preparation("p", "completed")));
  await flush();
  assert.match(h.ids["package-prepare-status"].textContent, /completed/);
});

test("failed reset retains controls so the same terminal instance can be retried", async (t) => {
  const h = await page(t, { ...empty, instances: [instance("i", "ready")] });
  await h.click("package-operation-status", "[data-instance-action=reset]");
  h.take("/operations/i/reset", "POST").reject(new Error("reset unavailable"));
  await flush();
  assert.match(h.ids["package-error"].textContent, /reset unavailable/);
  await h.click("package-operation-status", "[data-instance-action=reset]");
  h.take("/operations/i/reset", "POST").resolve(response(instance("i", "resetting")));
  await flush();
  h.take("/operations/i").resolve(response(instance("i", "ready")));
  await flush();
  assert.match(h.ids["package-operation-status"].textContent, /ready/);
});

test("choosing an archive echoes its name and marks the picker selected", async (t) => {
  const harness = await page(t);
  const input = harness.ids["package-file"];
  const name = harness.ids["package-file-name"];
  const drop = harness.ids["package-file-drop"];
  name.dataset.placeholder = "Select a package archive";

  input.files = [{ name: "org.example.lab-1.0.0.astinuslab" }];
  await input.fire("change");
  assert.equal(name.textContent, "org.example.lab-1.0.0.astinuslab");
  assert.ok(drop.classList.contains("is-selected"));

  input.files = [];
  await input.fire("change");
  assert.equal(name.textContent, "Select a package archive");
  assert.ok(!drop.classList.contains("is-selected"));
});

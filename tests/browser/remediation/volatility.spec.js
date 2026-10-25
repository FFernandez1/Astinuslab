/* global window */
const { test, expect } = require("@playwright/test");
const { readFile } = require("node:fs/promises");
const { URL } = require("node:url");
const { Buffer } = require("node:buffer");
const A = "linux.pslist.PsList";
const B = "linux.pstree.PsTree";
const result = (run = "run-A", plugin = A) => ({
  status: "completed",
  job_id: "job-A",
  run_id: run,
  plugin,
  columns: ["PID", "COMM"],
  rows: [[42, run]],
  artifact_id: `artifact-${run}`,
});

async function setup(page) {
  // Readiness is a fixture; templates, JS and CSS are served by the actual app.
  await page.route("**/volatility", async (route) => {
    const response = await route.fetch();
    const body = (await response.text())
      .replace('data-analysis-ready="0"', 'data-analysis-ready="1"')
      .replace('data-vol3-enabled="0"', 'data-vol3-enabled="1"');
    await route.fulfill({ response, body });
  });
  await page.route("**/api/v1/volatility/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/dumps"))
      return route.fulfill({
        json: {
          dumps: ["A", "B"].map((id) => ({
            job_id: `job-${id}`,
            file: `memory-${id}.lime`,
            os_family: "linux",
            os_label: "Linux",
            symbols: "ready",
          })),
        },
      });
    if (url.pathname.endsWith("/catalog"))
      return route.fulfill({
        json: {
          count: 2,
          categories: [
            { id: "processes", label: "Processes", plugins: [A, B].map((name) => ({ name })) },
          ],
        },
      });
    if (url.pathname.endsWith("/queue")) return route.fulfill({ json: { runs: [] } });
    if (url.pathname.endsWith("/latest")) return route.fulfill({ json: { status: "none" } });
    if (url.pathname.endsWith("/runs")) return route.fulfill({ json: { run_id: "run-A" } });
    return route.fulfill({ json: result() });
  });
  await page.goto("/volatility");
  await page.getByRole("button", { name: A, exact: true }).click();
  await expect(page.locator("#results-notrun")).toBeVisible();
}

async function hold(page, pattern) {
  let arrived;
  const ready = new Promise((resolve) => {
    arrived = resolve;
  });
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const done = new Promise((resolve) => {
    page.route(pattern, async (route) => {
      arrived();
      const json = await gate;
      await route.fulfill({ json });
      resolve();
    });
  });
  return {
    ready,
    send: async (json) => {
      release(json);
      await done;
    },
  };
}

for (const phase of ["enqueue", "poll", "cache"]) {
  for (const change of ["plugin", "dump", "away-and-back"]) {
    test(`${phase} reply cannot cross ${change} selection`, async ({ page }) => {
      await setup(page);
      const pattern =
        phase === "enqueue"
          ? "**/api/v1/volatility/runs"
          : phase === "poll"
            ? "**/api/v1/volatility/runs/job-A/run-A"
            : "**/api/v1/volatility/runs/latest?*";
      const pending = await hold(page, pattern);
      if (phase === "cache") await page.getByRole("button", { name: A, exact: true }).click();
      else await page.locator("#run-btn").click();
      await pending.ready;
      // Subsequent cache requests must complete normally while the old one is held.
      if (phase === "cache") await page.unroute(pattern);
      if (change === "dump") await page.locator("#dump-select").selectOption("job-B");
      else {
        await page.getByRole("button", { name: B, exact: true }).click();
        if (change === "away-and-back")
          await page.getByRole("button", { name: A, exact: true }).click();
      }
      await pending.send(phase === "enqueue" ? { run_id: "run-A" } : result());
      // Drain fetch handlers through a renderer task, then inspect actual output/export.
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            globalThis.requestAnimationFrame(() => globalThis.requestAnimationFrame(resolve)),
          ),
      );
      await expect(page.locator("#res-body")).not.toContainText("run-A");
      await expect(page.locator("#export-csv")).toBeDisabled();
      await expect(page.locator("#evidence-link")).toBeHidden();
    });
  }
}

test("successful result and CSV agree; failed rerun clears all prior output", async ({ page }) => {
  await setup(page);
  await page.locator("#run-btn").click();
  await expect(page.locator("#res-body")).toContainText("run-A");
  await expect(page.locator("#evidence-link")).toHaveAttribute(
    "href",
    "/api/v1/artifacts/artifact-run-A/download",
  );
  const downloading = page.waitForEvent("download");
  await page.locator("#export-csv").click();
  const download = await downloading;
  expect(download.suggestedFilename()).toContain("run-A");
  expect(await readFile(await download.path(), "utf8")).toContain("42,run-A");
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: { ...result(), status: "failed", error: "fixture rerun failure" } }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#task-state")).toHaveText("failed");
  await expect(page.locator("#res-body")).toBeEmpty();
  await expect(page.locator("#export-csv")).toBeDisabled();
  await expect(page.locator("#evidence-link")).toBeHidden();
});

test("late cache cannot overwrite a newer completed run", async ({ page }) => {
  await setup(page);
  const pending = await hold(page, "**/api/v1/volatility/runs/latest?*");
  await page.getByRole("button", { name: A, exact: true }).click();
  await pending.ready;
  await expect(page.locator("#run-btn")).toBeDisabled();
  await page.route("**/api/v1/volatility/runs/latest?**", (route) =>
    route.fulfill({ json: { status: "none" } }),
  );
  await page.getByRole("button", { name: B, exact: true }).click();
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: result("run-A", B) }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#res-body")).toContainText("run-A");
  await pending.send(result("older-cache"));
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        globalThis.requestAnimationFrame(() => globalThis.requestAnimationFrame(resolve)),
      ),
  );
  await expect(page.locator("#res-body")).toContainText("run-A");
  await expect(page.locator("#res-body")).not.toContainText("older-cache");
});

test("ISF upload errors retain both actions and allow valid retry without reload", async ({
  page,
}) => {
  await setup(page);
  await page.locator("#ptab-activity-btn").click();
  const row = page
    .locator(".vol-isfrow")
    .filter({ has: page.locator("[data-upload-isf-input]") })
    .first();
  const upload = row.locator("[data-upload-isf-target]");
  await page.route("**/api/v1/volatility/targets/*/isf/upload", (route) =>
    route.fulfill({ status: 422, json: { detail: "fixture invalid symbols" } }),
  );
  await upload.focus();
  await row.locator("input[type=file]").setInputFiles({
    name: "symbols.json",
    mimeType: "application/json",
    buffer: Buffer.from("{}"),
  });
  await expect(row).toContainText("fixture invalid symbols");
  await expect(upload).toBeVisible();
  await expect(upload).toBeEnabled();
  await expect(upload).toBeFocused();
  await expect(row.locator(".vol-isf-form")).toBeVisible();
  await expect(row.getByRole("status")).toContainText("fixture invalid symbols");
  await page.route("**/api/v1/volatility/targets/*/isf/upload", (route) =>
    route.fulfill({ json: { job_id: "fixture-isf-job" } }),
  );
  await row.locator("input[type=file]").setInputFiles({
    name: "symbols.json",
    mimeType: "application/json",
    buffer: Buffer.from("{}"),
  });
  await expect(row.getByRole("link")).toHaveAttribute("href", "/jobs/fixture-isf-job");
  await expect(upload).toBeEnabled();
  await expect(row).not.toContainText("fixture invalid symbols");
});

test("ISF build error retains an upload alternative and a retryable build", async ({ page }) => {
  await setup(page);
  await page.locator("#ptab-activity-btn").click();
  const row = page
    .locator(".vol-isfrow")
    .filter({ has: page.locator(".vol-isf-form") })
    .first();
  const build = row.locator(".vol-isf-form button");
  // The server intentionally has Volatility disabled. This fixture enables the
  // rendered control as well as the readiness data attribute used by its JS.
  await build.evaluate((button) => {
    button.disabled = false;
  });
  await page.route("**/volatility/build", (route) =>
    route.fulfill({ status: 503, json: { detail: "fixture build unavailable" } }),
  );
  await build.click();
  await expect(row).toContainText("fixture build unavailable");
  await expect(build).toBeEnabled();
  await expect(row.locator("[data-upload-isf-target]")).toBeEnabled();
  await expect(build).toBeFocused();
  await page.route("**/volatility/build", (route) =>
    route.fulfill({ json: { job_id: "fixture-build-job" } }),
  );
  await build.click();
  await expect(row.getByRole("link")).toHaveAttribute("href", "/jobs/fixture-build-job");
  await expect(row.locator("[data-upload-isf-target]")).toBeEnabled();
});

test("latest failed attempt cannot restore earlier output even if payload includes rows", async ({
  page,
}) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs/latest?*", (route) =>
    route.fulfill({ json: { ...result(), status: "failed", error: "latest failed attempt" } }),
  );
  await page.getByRole("button", { name: A, exact: true }).click();
  await expect(page.locator("#task-state")).toHaveText("failed");
  await expect(page.locator("#res-body")).toBeEmpty();
  await expect(page.locator("#export-csv")).toBeDisabled();
  await expect(page.locator("#evidence-link")).toBeHidden();
});

test("result with conflicting plugin identity fails closed", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: result("run-A", B) }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#task-state")).toHaveText("failed");
  await expect(page.locator("#res-body")).toBeEmpty();
  await expect(page.locator("#export-csv")).toBeDisabled();

  // Failing closed is half the job; the other half is saying so. This is the
  // most consequential thing the workbench can tell an operator -- the result
  // on screen does not belong to the analysis they asked for -- and until the
  // failure state started showing the console it was written to a hidden pane.
  await expect(page.locator("#view-raw")).toBeVisible();
  await expect(page.locator("#view-raw")).not.toBeEmpty();
});

test("the identity refusal is spoken in the operator's language", async ({ page }) => {
  // Deliberately not English. The message used to be a literal in results.js,
  // and a literal is indistinguishable from a lookup while the page is served
  // in the language the literal happens to be written in -- so this asserts
  // the rendered text against the dictionary the server actually handed this
  // page, in a locale where the two would differ.
  await page.goto("/");
  await page.context().addCookies([{ name: "lang", value: "es", url: page.url() }]);
  await setup(page);
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: result("run-A", B) }),
  );
  await page.locator("#run-btn").click();

  // The state read off the pill's class: the label is translated, which is
  // the whole point of this test.
  await expect(page.locator("#task-pill")).toHaveClass(/vol-pill--failed/);
  const expected = await page.evaluate(() => window.AL_T.vol_identity_mismatch);
  expect(expected).toBeTruthy();
  expect(expected).not.toBe("Result identity does not match the requested analysis.");
  await expect(page.locator("#view-raw")).toBeVisible();
  await expect(page.locator("#view-raw")).toHaveText(expected);
});

test("unverified symbols remain visibly marked with their returned source", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: { ...result(), isf_low_trust: true, isf_source: "manual_upload" } }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#findings .warn")).toContainText("manual_upload");
  await expect(page.locator("#findings .warn")).toContainText("unverified symbols");
});

test("a run that cannot be enqueued says so in the server's own words", async ({ page }) => {
  await setup(page);
  const detail = "Vol3 plugin run requires a completed memory dump job";
  await page.route("**/api/v1/volatility/runs", (route) =>
    route.fulfill({ status: 409, json: { detail } }),
  );
  await page.locator("#run-btn").click();

  // The run never started, so there is no run record to fall back on: whatever
  // the operator is told here comes from the rejected POST or from nowhere.
  await expect(page.locator("#task-pill")).toHaveClass(/vol-pill--failed/);
  await expect(page.locator("#view-raw")).toBeVisible();
  await expect(page.locator("#view-raw")).toHaveText(detail);
  await expect(page.locator("#res-body")).toBeEmpty();
  await expect(page.locator("#export-csv")).toBeDisabled();
});

test("a refusal with no detail still names the failure and its status", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs", (route) =>
    route.fulfill({ status: 503, body: "" }),
  );
  await page.locator("#run-btn").click();

  await expect(page.locator("#task-pill")).toHaveClass(/vol-pill--failed/);
  // A bare status code is not an explanation, but it is what there is, and the
  // sentence around it has to come from the dictionary rather than a literal.
  const fallback = await page.evaluate(() => window.AL_T.vol_enqueue_failed);
  expect(fallback).toBeTruthy();
  await expect(page.locator("#view-raw")).toBeVisible();
  await expect(page.locator("#view-raw")).toHaveText(`${fallback} (503)`);
});

test("status loss is neutral and Check status observes the same accepted run", async ({ page }) => {
  await setup(page);
  let polls = 0;
  let posts = 0;
  await page.route("**/api/v1/volatility/runs", (route) => {
    posts++;
    return route.fulfill({ json: { run_id: "run-A" } });
  });
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) => {
    polls += 1;
    return polls <= 5
      ? route.fulfill({ status: 502, json: { detail: "bad gateway" } })
      : route.fulfill({ json: result() });
  });
  await page.locator("#run-btn").click();

  // The run was accepted and then became unreachable. A page that kept polling
  // forever would leave a scanline over an empty table and never say why.
  await expect(page.locator("#view-raw")).toContainText(
    "Status unavailable. This analysis may still be running.",
    { timeout: 15000 },
  );
  await expect(page.locator("#view-raw")).toBeVisible();
  await expect(page.locator("#task-pill")).not.toHaveClass(/vol-pill--failed/);
  await expect(page.locator("#tablewrap")).not.toHaveClass(/vol-running/);
  await expect(page.locator("#run-btn")).toBeDisabled();
  await expect(page.locator("#task-id")).toContainText("run-A");
  // It gave up rather than hammering the endpoint: the retry budget is finite.
  expect(polls).toBeLessThanOrEqual(6);
  await page.getByRole("button", { name: "Check status", exact: true }).click();
  await expect(page.locator("#task-state")).toHaveText("completed");
  expect(posts).toBe(1);
});

test("latest queued run resumes observation instead of displaying Not run yet", async ({
  page,
}) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs/latest?**", (route) =>
    route.fulfill({ json: { ...result(), status: "queued" } }),
  );
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: { ...result(), status: "running" } }),
  );
  await page.getByRole("button", { name: A, exact: true }).click();
  await expect(page.locator("#task-id")).toContainText("run-A");
  await expect(page.locator("#results-notrun")).toBeHidden();
  await expect(page.locator("#run-btn")).toBeDisabled();
});

test("reload automatically observes an accepted run without another POST", async ({ page }) => {
  await setup(page);
  let posts = 0;
  await page.route("**/api/v1/volatility/runs", (route) => {
    posts++;
    return route.fulfill({ json: { run_id: "run-A" } });
  });
  let completed = false;
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: { ...result(), status: completed ? "completed" : "running" } }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#task-id")).toContainText("run-A");
  const saved = await page.evaluate(() =>
    JSON.parse(window.sessionStorage.getItem("astinuslab.volatility.active-run.v1")),
  );
  expect(Object.keys(saved).sort()).toEqual([
    "artifact_id",
    "job_id",
    "plugin",
    "run_id",
    "selection_id",
  ]);
  completed = true;
  await page.reload();
  await expect(page.locator("#task-state")).toHaveText("completed");
  await expect(page.locator("#task-id")).toContainText("run-A");
  expect(posts).toBe(1);
  expect(
    await page.evaluate(() => window.sessionStorage.getItem("astinuslab.volatility.active-run.v1")),
  ).toBeNull();
});

test("authorization loss stops retries and keeps the accepted run recoverable", async ({
  page,
}) => {
  await setup(page);
  let reads = 0;
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) => {
    reads++;
    return route.fulfill({ status: 401, json: { detail: "expired" } });
  });
  await page.locator("#run-btn").click();
  await expect(page.locator("#view-raw")).toContainText("Sign in again");
  await expect(page.locator("#run-btn")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Check status", exact: true })).toBeVisible();
  expect(reads).toBe(1);
  expect(
    await page.evaluate(() => window.sessionStorage.getItem("astinuslab.volatility.active-run.v1")),
  ).toContain("run-A");
});

test("an explicit different input in the URL replaces saved observation", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.fulfill({ json: { ...result(), status: "running" } }),
  );
  await page.locator("#run-btn").click();
  await expect(page.locator("#task-id")).toContainText("run-A");
  let restoredReads = 0;
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) => {
    restoredReads++;
    return route.fulfill({ json: result() });
  });
  await page.goto("/volatility?job=job-B");
  await expect(page.locator("#dump-select")).toHaveValue("job-B");
  await expect(page.locator("#task-id")).not.toContainText("run-A");
  expect(restoredReads).toBe(0);
  expect(
    await page.evaluate(() => window.sessionStorage.getItem("astinuslab.volatility.active-run.v1")),
  ).toBeNull();
});

test("accepted YARA run reload uses its exact ID without restoring rules or querying latest", async ({
  page,
}) => {
  await setup(page);
  const yara = "yarascan.YaraScan";
  await page.route("**/api/v1/volatility/catalog?**", (route) =>
    route.fulfill({
      json: {
        count: 1,
        categories: [{ id: "yara", label: "YARA", plugins: [{ name: yara }] }],
      },
    }),
  );
  await page.reload();
  let latest = 0,
    posts = 0;
  await page.route("**/api/v1/volatility/runs/latest?**", (route) => {
    latest++;
    return route.fulfill({ json: { status: "none" } });
  });
  await page.route("**/api/v1/volatility/runs", (route) => {
    posts++;
    return route.fulfill({ json: { run_id: "yara-run" } });
  });
  let completed = false;
  await page.route("**/api/v1/volatility/runs/job-A/yara-run", (route) =>
    route.fulfill({
      json: { ...result("yara-run", yara), status: completed ? "completed" : "running" },
    }),
  );
  await page.getByRole("button", { name: yara, exact: true }).click();
  await page.locator("#yara-rules").fill("rule PRIVATE_RULE { condition: true }");
  await page.locator("#run-btn").click();
  await expect(page.locator("#task-id")).toContainText("yara-run");
  expect(
    await page.evaluate(() => window.sessionStorage.getItem("astinuslab.volatility.active-run.v1")),
  ).not.toContain("PRIVATE_RULE");
  completed = true;
  await page.reload();
  await expect(page.locator("#task-id")).toContainText("yara-run");
  await expect(page.locator("#task-state")).toHaveText("completed");
  await expect(page.locator("#yara-rules")).toHaveValue("");
  await expect(page.locator("#run-btn")).toBeDisabled();
  expect(posts).toBe(1);
  expect(latest).toBe(0);
});

for (const response of [
  { ...result(), job_id: "wrong" },
  { status: "running", job_id: "job-A", plugin: A },
]) {
  test(`latest result refuses incomplete or mismatched identity ${JSON.stringify(response)}`, async ({
    page,
  }) => {
    await setup(page);
    await page.route("**/api/v1/volatility/runs/latest?**", (route) =>
      route.fulfill({ json: response }),
    );
    await page.getByRole("button", { name: A, exact: true }).click();
    await expect(page.locator("#task-state")).toHaveText("failed");
    await expect(page.locator("#view-raw")).not.toBeEmpty();
    await expect(page.locator("#export-csv")).toBeDisabled();
  });
}

// A row in the Activity log is a record of a run. Removing one, or clearing the
// log, is the only destructive control on this page -- and both of them used to
// fail without saying anything at all: the row stayed, the button re-armed, and
// the operator clicked again.
const QUEUE_ROW = {
  run_id: "run-A",
  job_id: "job-A",
  plugin: A,
  dump_file: "memory-A.lime",
  status: "failed",
  elapsed: "1s",
};

async function openActivity(page, queueRoute) {
  await page.route("**/api/v1/volatility/queue", queueRoute);
  await page.reload();
  await page.getByRole("button", { name: A, exact: true }).click();
  await page.locator("#ptab-activity-btn").click();
  await expect(page.locator("#queue-body .vol-queue-del")).toHaveCount(1);
}

test("a refused delete of a run record says why and leaves the row", async ({ page }) => {
  await setup(page);
  await openActivity(page, (route) => route.fulfill({ json: { runs: [QUEUE_ROW] } }));

  const detail = "run output is retained evidence";
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({ status: 409, json: { detail } })
      : route.fulfill({ json: result() }),
  );
  await page.locator("#queue-body .vol-queue-del").click();

  // The server's own reason, not a sentence this page invented.
  await expect(page.locator("#al-toast-root .toast--error")).toHaveText(detail);
  // The record is still there, because it was not deleted -- and the control is
  // usable again, so the operator can act on what they were just told.
  await expect(page.locator("#queue-body .vol-queue-del")).toHaveCount(1);
  await expect(page.locator("#queue-body .vol-queue-del")).toBeEnabled();
});

test("a refused clear of the activity log says why after the operator confirmed it", async ({
  page,
}) => {
  await setup(page);
  await openActivity(page, (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({ status: 500, json: { detail: "retention lock held by another worker" } })
      : route.fulfill({ json: { runs: [QUEUE_ROW] } }),
  );

  await page.locator("#vol-queue-clear").click();
  await page.locator("[data-confirm-yes]").click();

  // Saying nothing is worst here: the operator answered a confirmation dialog,
  // so silence reads as "done" while the log is exactly as it was.
  await expect(page.locator("#al-toast-root .toast--error")).toHaveText(
    "retention lock held by another worker",
  );
  await expect(page.locator("#queue-body tr")).toHaveCount(1);
  await expect(page.locator("#vol-queue-clear")).toBeEnabled();
});

test("a refusal with no detail still names the action that failed", async ({ page }) => {
  await setup(page);
  await openActivity(page, (route) => route.fulfill({ json: { runs: [QUEUE_ROW] } }));
  await page.route("**/api/v1/volatility/runs/job-A/run-A", (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({ status: 503, body: "" })
      : route.fulfill({ json: result() }),
  );
  await page.locator("#queue-body .vol-queue-del").click();

  const fallback = await page.evaluate(() => window.AL_T.vol_queue_delete_failed);
  expect(fallback).toBeTruthy();
  await expect(page.locator("#al-toast-root .toast--error")).toHaveText(fallback);
});

test("a build refused without a reason names the action, in the page's language", async ({
  page,
}) => {
  // Not English, for the reason the identity refusal is not: a literal and a
  // lookup render identically in the language the literal is written in.
  await page.goto("/");
  await page.context().addCookies([{ name: "lang", value: "fr", url: page.url() }]);
  await setup(page);
  await page.locator("#ptab-activity-btn").click();
  const row = page
    .locator(".vol-isfrow")
    .filter({ has: page.locator(".vol-isf-form") })
    .first();
  const build = row.locator(".vol-isf-form button");
  await build.evaluate((button) => {
    button.disabled = false;
  });
  // No body at all: the server refused and said nothing, which is exactly when
  // the page has to supply the sentence itself.
  await page.route("**/volatility/build", (route) => route.fulfill({ status: 502, body: "" }));

  await build.click();
  const expected = await page.evaluate(() => window.AL_T.vol_isf_build_failed);
  expect(expected).toBeTruthy();
  await expect(row).toContainText(expected);
  await expect(build).toBeEnabled();
});

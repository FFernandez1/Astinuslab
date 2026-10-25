/* global document, window, innerWidth */
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const { Buffer } = require("node:buffer");
const { writeFileSync } = require("node:fs");
const path = require("node:path");
const { URL } = require("node:url");
const root = path.resolve(__dirname, "../../..");
const fixtures = JSON.parse(
  execFileSync(
    path.join(root, ".venv/bin/python"),
    [
      "-c",
      `
import json
from datetime import datetime
from types import SimpleNamespace as NS
from jinja2 import Environment, FileSystemLoader, select_autoescape
from astinuslab.i18n_data.en import TRANSLATIONS
env = Environment(loader=FileSystemLoader("src/astinuslab/web/templates"), autoescape=select_autoescape())
result = {}
for variant in ("empty", "one", "many", "changed", "active", "removed"):
    count = 0 if variant == "empty" else 1 if variant == "one" else 50
    jobs = [NS(id=f"{i:08d}-quality-job-abcdef0123456789", target_id="long-target-label-" + str(i) + "-" + "sample-" * 8,
        scenario_id="quality-scenario", status=["completed", "failed", "waiting_for_capacity", "running"][i % 4],
        capacity_reason=None, created_at=datetime(2026,9,6), execution_mode="immediate", workload_id=None, current_stage=None) for i in range(count)]
    if variant == "changed": jobs[0].status = "failed"
    if variant == "active": jobs[0].status = "running"
    if variant == "removed": jobs = jobs[1:]
    context = dict(jobs=jobs, jobs_total=len(jobs), jobs_page=1, jobs_pages=1, jobs_state="all", jobs_start=1 if jobs else 0, jobs_end=len(jobs), t=TRANSLATIONS, lang="en", supported_langs={"en": "English"}, csrf_token="fixture", asset_v="fixture",
        active_statuses={"running", "waiting_for_capacity", "preparing_image"},
        request=NS(state=NS(csp_nonce="fixture"), url=NS(path="/jobs")))
    result[variant] = {"page": env.get_template("index.html").render(**context), "fragment": env.get_template("partials/jobs_table.html").render(**context)}
print(json.dumps(result))
`,
    ],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}/src:${root}` },
      encoding: "utf8",
      maxBuffer: 5 * 1024 * 1024,
    },
  ),
);
const jobId = "00000000-quality-job-abcdef0123456789";
const row = `#job-row-${jobId}`;

async function setup(page, initial = "many") {
  const fixture = { next: initial, polls: 0, mutations: 0, errors: [] };
  page.on("pageerror", (error) => fixture.errors.push(error.message));
  await page.route("**/*", (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/static/")) return route.continue();
    if (request.method() !== "GET") {
      fixture.mutations++;
      return route.abort();
    }
    if (pathname === "/jobs")
      return route.fulfill({ contentType: "text/html", body: fixtures[initial].page });
    if (pathname === "/jobs-table-fragment") {
      fixture.polls++;
      expect(request.headers()["hx-request"]).toBe("true");
      return route.fulfill({ contentType: "text/html", body: fixtures[fixture.next].fragment });
    }
    fixture.errors.push(`Unexpected request: ${pathname}`);
    return route.abort();
  });
  await page.clock.install();
  await page.goto("/jobs");
  await page.evaluate(() => {
    window.fixtureSwaps = 0;
    document.addEventListener("htmx:afterSettle", (e) => {
      if (e.target.id === "jobs-table") window.fixtureSwaps++;
    });
  });
  return fixture;
}
async function poll(page, fixture, next) {
  fixture.next = next;
  const before = await page.evaluate(() => window.fixtureSwaps);
  await page.clock.runFor(5100);
  await expect.poll(() => page.evaluate(() => window.fixtureSwaps)).toBe(before + 1);
}

for (const [width, zoom] of [
  [1440, 1],
  [1024, 1],
  [768, 1],
  [1440, 2],
]) {
  test(`Jobs retain focused actions at ${width}px and CSS zoom ${zoom}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await setup(page);
    if (zoom === 2)
      await page.evaluate(() => {
        document.body.style.zoom = "2";
      });
    const open = page.locator(`${row} a`);
    await open.focus();
    const original = await open.elementHandle();
    await poll(page, fixture, "many");
    await expect(open).toBeFocused();
    expect(await original.evaluate((node) => node.isConnected)).toBe(false);
    const del = page.locator(`${row} [data-delete-job-id]`);
    await del.focus();
    const scrollLeft = await page.locator(".table-scroll").evaluate((node) => node.scrollLeft);
    await poll(page, fixture, "changed");
    await expect(del).toBeFocused();
    expect(await page.locator(".table-scroll").evaluate((node) => node.scrollLeft)).toBe(
      scrollLeft,
    );
    await expect(page.locator(`${row} .badge-failed`)).toHaveText("failed");
    expect(fixture.polls).toBe(2);
    expect(fixture.errors).toEqual([]);
    expect(fixture.mutations).toBe(0);
    const metrics = await page.evaluate(() => ({
      nodes: document.querySelectorAll("*").length,
      horizontalOverflow: document.documentElement.scrollWidth - innerWidth,
    }));
    expect(metrics.horizontalOverflow).toBeLessThanOrEqual(2);
    const metricsPath = testInfo.outputPath("metrics.json");
    writeFileSync(
      metricsPath,
      JSON.stringify({
        ...metrics,
        fragmentBytes: Buffer.byteLength(fixtures.many.fragment),
        polls: fixture.polls,
        replacedFocusedControl: true,
        zoomProbe: zoom === 2 ? "CSS zoom, not browser zoom" : "normal",
      }),
    );
    await testInfo.attach("fixture-metrics", {
      path: metricsPath,
      contentType: "application/json",
    });
    await page.screenshot({ path: testInfo.outputPath("jobs.png") });
  });
}

for (const next of ["active", "removed"]) {
  test(`disappearing Job action has safe focus fallback: ${next}`, async ({ page }) => {
    const fixture = await setup(page);
    await page.locator(`${row} [data-delete-job-id]`).focus();
    await poll(page, fixture, next);
    await expect(
      page.locator(next === "active" ? `${row} a` : 'a[href="/jobs/new"]'),
    ).toBeFocused();
  });
}

test("checked rows and confirmation suppress swaps, then polling resumes", async ({ page }) => {
  const fixture = await setup(page);
  const select = page.locator(`${row} .job-select`);
  await select.check();
  await select.focus();
  await page.clock.runFor(11000);
  expect(fixture.polls).toBe(0);
  await expect(select).toBeChecked();
  await expect(select).toBeFocused();
  await select.uncheck();
  const del = page.locator(`${row} [data-delete-job-id]`);
  await del.click();
  const files = page.locator("#delete-files-checkbox");
  await files.check();
  await files.focus();
  await page.clock.runFor(11000);
  expect(fixture.polls).toBe(0);
  await expect(files).toBeFocused();
  await expect(files).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(del).toBeFocused();
  await poll(page, fixture, "many");
  expect(fixture.mutations).toBe(0);
});

test("empty, one and 50 Jobs display the current count", async ({ page }) => {
  const fixture = await setup(page, "empty");
  await expect(page.locator(".count-badge")).toHaveText("0");
  await poll(page, fixture, "one");
  await expect(page.locator(".count-badge")).toHaveText("1");
  await poll(page, fixture, "many");
  await expect(page.locator(".count-badge")).toHaveText("50");
  await expect(page.locator("#jobs-table tbody tr")).toHaveCount(50);
});

test("a newer focus choice takes priority over removal fallback", async ({ page }) => {
  const fixture = await setup(page);
  await page.locator(`${row} [data-delete-job-id]`).focus();
  await page.evaluate(() => {
    document.addEventListener(
      "htmx:beforeSwap",
      () => document.querySelector('a[href="/images"]').focus(),
      { once: true },
    );
  });
  await poll(page, fixture, "removed");
  await expect(page.locator('a[href="/images"]')).toBeFocused();
});

test("resuming after background timer throttling does not burst requests", async ({ page }) => {
  const fixture = await setup(page);
  // Playwright fastForward fires each due timer once, modeling a throttled tab.
  await page.clock.fastForward(60000);
  await expect.poll(() => fixture.polls).toBe(1);
  await page.clock.runFor(5100);
  await expect.poll(() => fixture.polls).toBe(2);
  expect(fixture.errors).toEqual([]);
});

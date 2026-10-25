/* global document, performance */
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const { writeFileSync } = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../../..");

// This suite uses only the remediation server's disposable SQLite database.
// No Job is submitted or dispatched, and no native service is contacted.
function historyData(count, finish = false) {
  execFileSync(
    process.env.ASTINUSLAB_PYTHON || path.join(root, ".venv/bin/python"),
    [
      "-c",
      `
import os, sys
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import create_engine, delete, insert, update
from astinuslab.db.models import Job
root = Path(os.environ["ASTINUSLAB_REMEDIATION_ISO_DIR"]).resolve()
assert root.name.startswith("astinuslab-remediation-") and root.parent == Path("/tmp")
engine = create_engine("sqlite:///" + str(root / "ui.db"))
with engine.begin() as conn:
    if sys.argv[2] == "true":
        conn.execute(update(Job).where(Job.id == "pagination-active").values(status="completed"))
    else:
        conn.execute(delete(Job).where(Job.id.like("pagination-%")))
        now = datetime(2026, 1, 1)
        conn.execute(insert(Job), [dict(id=f"pagination-{n:05d}", target_id="synthetic-target", scenario_id="synthetic-scenario", execution_mode="immediate", status="completed", created_at=now) for n in range(int(sys.argv[1]))] + [dict(id="pagination-active", target_id="synthetic-target", scenario_id="synthetic-scenario", execution_mode="immediate", status="waiting_for_capture", created_at=now - timedelta(days=1))])
engine.dispose()
`,
      String(count),
      String(finish),
    ],
    { cwd: root, env: { ...process.env, PYTHONPATH: `${root}/src:${root}` } },
  );
}

for (const count of [100, 1000, 5000]) {
  test(`${count} historical Jobs render and poll one bounded page`, async ({ page }, testInfo) => {
    historyData(count);
    await page.goto("/jobs");
    await expect(page.locator("#jobs-table tbody tr")).toHaveCount(50);
    await expect(page.locator("#jobs-table tbody tr").first()).toHaveId(
      "job-row-pagination-active",
    );
    await expect(page.locator("#jobs-page-range")).toHaveText(`1–50 / ${count + 1}`);
    const start = Date.now();
    const response = await page.request.get("/jobs-table-fragment");
    const body = await response.body();
    expect(response.status()).toBe(200);
    expect(body.byteLength).toBeLessThan(125000);
    const metrics = await page.evaluate(() => ({
      nodes: document.querySelectorAll("#jobs-table *").length,
      navigationDurationMs: performance.getEntriesByType("navigation")[0].duration,
    }));
    const metricsPath = testInfo.outputPath("metrics.json");
    writeFileSync(
      metricsPath,
      JSON.stringify({
        count,
        ...metrics,
        fragmentBytes: body.byteLength,
        fetchMs: Date.now() - start,
      }),
    );
    await testInfo.attach("history-page-characterization", {
      path: metricsPath,
      contentType: "application/json",
    });
  });
}

test("history navigation, filters, back/forward and polling retain the requested page", async ({
  page,
}) => {
  historyData(100);
  await page.clock.install();
  await page.goto("/jobs");
  await page.locator("#jobs-next").click();
  await expect(page).toHaveURL(/\/jobs\?page=2&state=all$/);
  await expect(page.locator("#jobs-table tbody tr").first()).toHaveId("job-row-pagination-00050");
  await page.locator("#jobs-next").click();
  await expect(page.locator("#jobs-table tbody tr")).toHaveCount(1);
  await page.goBack();
  await expect(page).toHaveURL(/page=2&state=all$/);
  await expect(page.locator("#jobs-page-range")).toHaveText("51–100 / 101");
  await page.goForward();
  await expect(page).toHaveURL(/page=3&state=all$/);
  await expect(page.locator("#jobs-page-range")).toHaveText("101–101 / 101");
  await page.locator("#jobs-filter-finished").click();
  await page.locator("#jobs-next").click();
  await expect(page).toHaveURL(/page=2&state=finished$/);
  await page.locator("#jobs-filter-finished").focus();
  const response = page.waitForResponse((reply) =>
    reply.url().endsWith("/jobs-table-fragment?page=2&state=finished"),
  );
  await page.clock.runFor(5100);
  expect((await response).status()).toBe(200);
  await expect(page.locator("#jobs-filter-finished")).toBeFocused();
  await expect(page.locator("#jobs-page-range")).toHaveText("51–100 / 100");
  await expect(page.locator("#jobs-table tbody tr").first()).toHaveId("job-row-pagination-00049");
  await page.locator("#jobs-filter-active").click();
  await expect(page.locator("#jobs-table tbody tr")).toHaveCount(1);
  await expect(page.locator("#job-row-pagination-active .spinner")).toBeVisible();
  historyData(100, true);
  await page.clock.runFor(5100);
  await expect(page.locator("#jobs-table tbody tr")).toHaveCount(0);
  await page.locator("#jobs-filter-finished").click();
  await page.locator("#jobs-next").click();
  await page.locator("#jobs-next").click();
  await expect(page.locator("#job-delete-pagination-active")).toBeVisible();
  await expect(page.locator("#jobs-table .spinner")).toHaveCount(0);
});

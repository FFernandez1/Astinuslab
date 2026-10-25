const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

// Render the actual Jinja partials with synthetic scheduler state. This verifies
// presentation independently of the capacity agent's admission/host tests.
function renderCapacity(reason, status = "waiting_for_capacity") {
  const root = path.resolve(__dirname, "../../..");
  return execFileSync(
    process.env.ASTINUSLAB_PYTHON || path.join(root, ".venv/bin/python"),
    [
      "-c",
      `
import json, os, subprocess, sys
from datetime import datetime
from types import SimpleNamespace
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape
from astinuslab.i18n_data.en import TRANSLATIONS
from astinuslab.jobs.job_status import CANCELLABLE_STATUSES
data = json.loads(sys.argv[1])
loader = FileSystemLoader("src/astinuslab/web/templates")
if os.environ.get("ASTINUSLAB_UI_BASELINE_TEMPLATES") == "1":
    baseline = {name: subprocess.check_output(["git", "show", "6162761f8cb66838690a2b3a3e6f6135f56c419e:src/astinuslab/web/templates/" + name], text=True) for name in ["partials/jobs_table.html", "partials/live_status.html"]}
    loader = ChoiceLoader([DictLoader(baseline), loader])
env = Environment(loader=loader, autoescape=select_autoescape())
job = SimpleNamespace(id="ui-capacity-job", target_id="fixture-target", scenario_id="fixture-scenario", status=data["status"], capacity_reason=data["reason"], created_at=datetime(2026,9,6), execution_mode="immediate", workload_id=None, current_stage=None)
context = dict(job=job, jobs=[job], jobs_total=1, jobs_page=1, jobs_pages=1, jobs_state="all", jobs_start=1, jobs_end=1, t=TRANSLATIONS, is_active=True, can_cancel=job.status in CANCELLABLE_STATUSES, active_statuses={"waiting_for_capacity", "preparing_image"}, events=[], actions=[], request=SimpleNamespace(state=SimpleNamespace(csp_nonce="fixture")))
print(env.get_template("partials/jobs_table.html").render(**context))
print(env.get_template("partials/live_status.html").render(**context))
`,
      JSON.stringify({ reason, status }),
    ],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}/src:${root}`, ASTINUSLAB_FAKE_MODE: "1" },
      encoding: "utf8",
    },
  );
}

for (const reason of [
  "VM capacity: 2 of 2 slots reserved",
  "RAM headroom below reserve",
  "Storage headroom below reserve",
  "Waiting behind an earlier Job (FIFO)",
  null,
]) {
  test(`Jobs explain capacity wait: ${reason || "fallback"}`, async ({ page }) => {
    const body = renderCapacity(reason);
    await page.route("**/ui-capacity-fixture", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: `<html><head><link rel="stylesheet" href="/static/css/tokens.css"><link rel="stylesheet" href="/static/css/components.css"><link rel="stylesheet" href="/static/css/pages/jobs.css"></head><body>${body}</body></html>`,
      }),
    );
    await page.goto("/ui-capacity-fixture");
    await expect(page.locator("#jobs-table")).toContainText("Waiting for capacity");
    await expect(page.locator("#live-status")).toContainText("Waiting for capacity");
    const explanation =
      reason || "This Job will start automatically when capacity becomes available.";
    await expect(page.locator("#jobs-table")).toContainText(explanation);
    await expect(page.locator("#live-status")).toContainText(explanation);
    await expect(page.locator(".phase-step.is-active")).toHaveCount(0);
    await expect(
      page.locator("#live-status").getByRole("button", { name: "Cancel", exact: true }),
    ).toBeEnabled();
  });
}

test("admitted Job stops showing a retained capacity reason", async ({ page }) => {
  await page.route("**/ui-capacity-fixture", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: renderCapacity("stale reason", "preparing_image"),
    }),
  );
  await page.goto("/ui-capacity-fixture");
  await expect(page.locator("body")).not.toContainText("stale reason");
  await expect(page.locator(".phase-step.is-active")).toHaveCount(1);
});

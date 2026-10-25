/* global getComputedStyle, window, fetch */
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const { URL, URLSearchParams } = require("node:url");
const root = path.resolve(__dirname, "../../..");

// Render production templates with disposable state; POSTs are always intercepted.
function render(template, lang = "en") {
  return execFileSync(
    process.env.ASTINUSLAB_PYTHON || path.join(root, ".venv/bin/python"),
    [
      "-c",
      `
import sys
from datetime import datetime
from types import SimpleNamespace as NS
from jinja2 import Environment, FileSystemLoader, select_autoescape
from astinuslab.i18n import get_translations
env = Environment(loader=FileSystemLoader("src/astinuslab/web/templates"), autoescape=select_autoescape())
ctx = dict(t=get_translations(sys.argv[2]), lang=sys.argv[2], supported_langs={"en":"English"}, csrf_token="fixture", asset_v="fixture", request=NS(state=NS(csp_nonce="fixture"), url=NS(path="/jobs/fixture")),
    vault=dict(total_files=2,total_gb=0,groups=[],orphans=[dict(dirname="orphan-"+s,file_count=1,size_gb=0,modified=None) for s in "ab"]),
    entries=[dict(key="key-"+s,target_id="target",workload_id="workload-"+s,base_image="fixture",size_human="1 B",size_bytes=1,created_at="2026-01-01") for s in "abc"],total_size_human="3 B",
    job=NS(id="fixture",target_id="target",scenario_id="lab-session",created_at=datetime(2026,1,1),execution_mode="manual_trigger",status="waiting_for_capture",capture_type="memory",workload_id="fixture",lab_snapshot_name="fixture",guest_ip=None),workload=NS(display_name="Fixture workload",services=[]),events=[],actions=[],runtimes=[],lab_remaining_seconds=60)
print(env.get_template(sys.argv[1]).render(**ctx))
`,
      template,
      lang,
    ],
    { cwd: root, env: { ...process.env, PYTHONPATH: `${root}/src:${root}` }, encoding: "utf8" },
  );
}

async function fixture(page, template, lang) {
  await page.route("**/safety-fixture", (route) =>
    route.fulfill({ contentType: "text/html", body: render(template, lang) }),
  );
  await page.route("**/jobs/fixture/live-fragment**", (route) => route.fulfill({ status: 204 }));
  await page.goto("/safety-fixture");
}

test("orphan row confirmation and submission name only that row", async ({ page }) => {
  await fixture(page, "admin_dumps.html");
  const posts = [];
  await page.route("**/dumps/orphans/delete", (route) => {
    posts.push(new URLSearchParams(route.request().postData()));
    return route.fulfill({ status: 204 });
  });
  await page.locator('input[name="dirnames"][value="orphan-a"]').check();
  const row = page.locator('button[name="dirnames"][value="orphan-b"]');
  await row.click();
  await expect(page.locator("#confirm-dialog-message")).toContainText("orphan-b");
  await expect(page.locator("#confirm-dialog-message")).not.toContainText("orphan-a");
  await page.locator("[data-confirm-no]").click();
  expect(posts).toHaveLength(0);
  await row.click();
  await page.locator("[data-confirm-yes]").click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].getAll("dirnames")).toEqual(["orphan-b"]);
  await page.locator('#orphan-form button[type="submit"]:not([name])').click();
  await page.locator("[data-confirm-yes]").click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].getAll("dirnames")).toEqual(["orphan-a"]);
});

test("cache first middle last rows retain separate form ownership", async ({ page }) => {
  await fixture(page, "admin_workload_cache.html");
  const posts = [];
  await page.route("**/workload-cache/*delete", (route) => {
    posts.push({
      url: route.request().url(),
      values: new URLSearchParams(route.request().postData()),
    });
    return route.fulfill({ status: 204 });
  });
  await page.locator('input[name="keys"][value="key-a"]').check();
  for (const [index, key] of ["key-a", "key-b", "key-c"].entries()) {
    const row = page.locator("tbody tr").nth(index);
    expect(await row.locator('input[name="keys"]').evaluate((el) => el.form.id)).toBe("cache-form");
    const button = row.getByRole("button", { name: /Delete/ });
    expect(await button.evaluate((el) => new URL(el.form.action).pathname)).toBe(
      "/workload-cache/delete",
    );
    await button.click();
    await page.locator("[data-confirm-no]").click();
    expect(posts).toHaveLength(index);
    await button.click();
    await page.locator("[data-confirm-yes]").click();
    await expect.poll(() => posts.length).toBe(index + 1);
    expect(posts[index].values.get("key")).toBe(key);
    expect(posts[index].values.getAll("keys")).toEqual([]);
  }
  await page.locator('button[form="cache-form"]').click();
  await page.locator("[data-confirm-yes]").click();
  await expect.poll(() => posts.length).toBe(4);
  expect(posts[3].values.getAll("keys")).toEqual(["key-a"]);
});

async function chooseScenario(page, mode) {
  await page.goto("/jobs/new");
  await page.locator('[name="target_id"]').first().check({ force: true });
  await page.locator(`[name="scenario_id"][data-mode="${mode}"]`).first().check({ force: true });
  await page.locator("#wiz-next-btn").click();
}

test("Workload radios support native keyboard selection and visible focus", async ({ page }) => {
  await chooseScenario(page, "manual_trigger");
  const none = page.locator('[name="workload_id"][value=""]');
  await expect(none).toBeAttached();
  expect(await none.evaluate((el) => getComputedStyle(el).display)).not.toBe("none");
  await page.locator("[data-attacker-change]").focus();
  await page.keyboard.press("Tab");
  await expect(none).toBeFocused();
  await page.keyboard.press("ArrowRight");
  const selected = page.locator('[name="workload_id"]:checked');
  expect(await selected.inputValue()).not.toBe("");
  await expect(selected).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(none).toBeChecked();
  await page.keyboard.press("Space");
  await expect(none).toBeChecked();
});

for (const mode of ["immediate", "post_script"]) {
  test(`switching manual to ${mode} clears incompatible submitted values`, async ({ page }) => {
    await chooseScenario(page, "manual_trigger");
    await page.locator(".workload-card input:not(:disabled)").first().check({ force: true });
    await page.locator("#wiz-back-btn").click();
    await page.locator(`[name="scenario_id"][data-mode="${mode}"]`).first().check({ force: true });
    await page.locator("#wiz-next-btn").click();
    await page.locator("#wiz-next-btn").click();
    await expect(page.locator('[data-step="review"].wiz-stage-panel')).toBeVisible();
    await expect(page.locator("#wiz-review-content")).not.toContainText("WordPress");
    let values;
    await page.route("**/jobs/new", (route) => {
      values = new URLSearchParams(route.request().postData());
      return route.fulfill({ status: 204 });
    });
    await page.locator("#wiz-launch-btn").click();
    await expect.poll(() => Boolean(values)).toBe(true);
    expect(values.get("workload_id") || "").toBe("");
    expect(values.get("attacker_vm_id") || "").toBe("");
    expect(values.get("attacker_vm_vcpus") || "").toBe("");
  });
}

test("resource fractions stay editable and block review; memory conversion remains exact", async ({
  page,
}) => {
  await chooseScenario(page, "immediate");
  for (const field of ["vm_vcpus", "vm_disk_gb"]) {
    const input = page.locator(`[data-field="${field}"] [data-slider-input]`);
    for (const value of ["", "-1", "1.5", "1e20"]) {
      await input.fill(value);
      await page.locator("#wiz-next-btn").click();
      await expect(page.locator('[data-step="configure"].wiz-stage-panel')).toBeVisible();
      await expect(input).toHaveAttribute("aria-invalid", "true");
      await expect(input).toHaveValue(value);
    }
    await input.fill("1");
    await input.press("Tab");
  }
  await page.locator('[data-field="vm_memory_mb"] [data-slider-input]').fill("1.5");
  await page.locator("#wiz-next-btn").click();
  await expect(page.locator('[data-step="review"].wiz-stage-panel')).toBeVisible();
  await expect(page.locator('[name="vm_memory_mb"]')).toHaveValue("1536");
  await expect(page.locator("#wiz-review-content")).toContainText("1.5 GB RAM");
  await expect(page.locator(".nj-seqbox__h")).toContainText("Form review");
  await expect(page.locator(".nj-seqbox__h")).toContainText("Host readiness has not been checked");
  expect(
    await page
      .locator(".wiz-review-row")
      .first()
      .evaluate((el) => getComputedStyle(el, "::before").content),
  ).not.toContain("OK");
});

for (const lang of ["es", "fr", "ca"]) {
  test(`production Job export supplies localized Reset failure in ${lang}`, async ({ page }) => {
    await fixture(page, "job_detail.html", lang);
    await page.route("**/jobs/fixture/reset-lab", (route) =>
      route.fulfill({ status: 503, json: {} }),
    );
    const expected = await page
      .locator("#al-i18n")
      .evaluate((el) => JSON.parse(el.textContent).job_reset_failed);
    expect(expected).toBeTruthy();
    await page.locator("#btn-reset-lab-fixture").click();
    await page.locator("[data-confirm-yes]").click();
    await expect(page.locator("#al-toast-root")).toContainText(expected);
  });
}

test("server validation restores selections and editable resources", async ({ page }) => {
  await chooseScenario(page, "manual_trigger");
  const workload = page.locator(".workload-card input:not(:disabled)").first();
  await workload.check({ force: true });
  const saved = await page
    .locator("#wiz-form")
    .evaluate((form) => Object.fromEntries(new window.FormData(form)));
  saved.vm_vcpus = "1.5";
  const body = await page.evaluate(async (values) => {
    const response = await fetch("/jobs/new", {
      method: "POST",
      headers: window.csrfHeaders("POST"),
      body: new window.URLSearchParams(values),
    });
    if (response.status !== 422) throw new Error("Expected recoverable validation");
    return response.text();
  }, saved);
  await page.route("**/validation-fixture", (route) =>
    route.fulfill({ contentType: "text/html", body }),
  );
  await page.goto("/validation-fixture");
  await expect(page.locator('[name="target_id"]:checked')).toHaveValue(saved.target_id);
  await expect(page.locator('[name="scenario_id"]:checked')).toHaveValue(saved.scenario_id);
  await page.locator("#wiz-next-btn").click();
  await expect(page.locator('[name="workload_id"]:checked')).toHaveValue(saved.workload_id);
  await page.locator("#wiz-next-btn").click();
  const cpus = page.locator('[data-field="vm_vcpus"] [data-slider-input]');
  await expect(cpus).toHaveValue("1.5");
  await cpus.fill("2");
  await page.locator("#wiz-next-btn").click();
  await expect(page.locator('[data-step="review"].wiz-stage-panel')).toBeVisible();
});

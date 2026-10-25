const { test, expect } = require("@playwright/test");
const { mockPlaygroundApi, collectBrowserErrors } = require("../helpers/mocks");
const { openTargetWizard } = require("../helpers/target-wizard");

test.beforeEach(async ({ page }) => {
  await mockPlaygroundApi(page);
});

test("home redirects to playground @smoke", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/playground$/);
  await expect(page.locator(".pg-header-label")).toHaveText("Playground");
});

test("playground loads core controls without browser errors @smoke", async ({ page }) => {
  const browserErrors = collectBrowserErrors(page);

  await page.goto("/playground");

  await expect(page.getByRole("button", { name: "+ Target" })).toBeVisible();
  await expect(page.getByRole("button", { name: "+ Attacker" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Import recipe" })).toBeVisible();
  await expect(page.getByLabel("Lab status")).toBeVisible();
  await expect(page.getByLabel("VMs grouped by role")).toBeHidden();
  await expect(page.getByLabel("Playground connection map")).toBeVisible();
  expect(browserErrors).toEqual([]);
});

test("package export validates IDs and versions before submitting @smoke", async ({ page }) => {
  const session = { id: "pg-export-validation", state: "running", networks: [], vms: [] };
  await page.route("**/api/v1/playground/sessions/current-or-null", (route) =>
    route.fulfill({ json: session }),
  );
  await page.route("**/api/v1/playground/sessions/current", (route) =>
    route.fulfill({ json: session }),
  );
  let mutations = 0;
  await page.route("**/api/**", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    mutations++;
    return route.fulfill({ status: 400, json: { detail: "unexpected export submission" } });
  });
  await page.goto("/playground");
  const opener = page.locator("#btn-export-package");
  if ((await opener.count()) === 0) {
    const expected = ["1", "true", "yes", "on"].includes(
      String(process.env.ASTINUSLAB_PACKAGES_ENABLED || "").toLowerCase(),
    );
    if (expected)
      throw new Error("Portable packages were requested but the export control is absent");
    test.skip(true, "portable packages disabled; set ASTINUSLAB_PACKAGES_ENABLED=true to run this");
  }
  await opener.click();
  const form = page.locator("#export-package-form");
  const id = form.locator('[name="package_id"]');
  const version = form.locator('[name="version"]');
  await form.locator('[name="creator"]').fill("Fixture author");
  await id.fill("org.fixture_1-lab");
  await version.fill("2.4.0-rc.1-beta");
  expect(await form.evaluate((node) => node.checkValidity())).toBe(true);

  for (const [field, invalidValues, validValue] of [
    [id, ["ab", "-invalid", "invalid package", "x".repeat(129)], "org.fixture_1-lab"],
    [version, ["1.2", "invalid", "1.2.3-"], "2.4.0-rc.1-beta"],
  ]) {
    for (const invalid of invalidValues) {
      await field.fill(invalid);
      expect(await field.evaluate((node) => node.validity.patternMismatch)).toBe(true);
      await form.locator('[type="submit"]').click();
      await expect(field).toBeFocused();
      expect(mutations).toBe(0);
    }
    await field.fill(validValue);
  }
  expect(await form.evaluate((node) => node.checkValidity())).toBe(true);
});

test("target wizard opens and escape closes it @smoke", async ({ page }) => {
  await page.goto("/playground");

  const dialog = await openTargetWizard(page);

  await expect(page.getByText("Basics")).toBeVisible();
  await expect(dialog.getByRole("radio", { name: /Ubuntu 22\.04/ }).first()).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next" })).toBeVisible();

  await page.keyboard.press("Escape");

  await expect(page.getByRole("dialog", { name: "Create target" })).toBeHidden();
});

test("target wizard walks every step and back @smoke", async ({ page }) => {
  const browserErrors = collectBrowserErrors(page);
  await page.goto("/playground");

  const dialog = await openTargetWizard(page);

  // Future steps are disabled (jobs-style stepper); walk forward with Next.
  await expect(dialog.getByRole("button", { name: /Workload/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeVisible();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByRole("button", { name: "Next" })).toBeVisible();
  // Completed steps stay clickable for going back.
  await dialog.getByRole("button", { name: /Basics/ }).click();
  await expect(dialog.getByRole("button", { name: /Basics/ })).toHaveAttribute("data-active", "");

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  expect(browserErrors).toEqual([]);
});

test("target workload search keeps focus and resources default compact @smoke", async ({
  page,
}) => {
  await page.goto("/playground");

  const dialog = await openTargetWizard(page);
  await dialog.getByRole("button", { name: "Next" }).click();

  const search = dialog.locator(".workload-search-input");
  await search.fill("dv");

  await expect(search).toBeFocused();
  await expect(search).toHaveValue("dv");
  await expect(dialog.locator(".pg-v2-workload-card", { hasText: "DVWA" })).toBeVisible();

  await dialog.getByRole("button", { name: "Next" }).click();
  // Resources step: disk is now a slider (vcpu/memory/disk share one control).
  await expect(dialog.getByLabel("disk value")).toHaveValue("20");
  await expect(dialog.locator(".pg-bar-row", { hasText: /pool/i })).toHaveCount(0);
});

test("attacker auto-assigned notice stays inside network card @smoke", async ({ page }) => {
  await page.goto("/playground");

  await page.getByRole("button", { name: "+ Attacker" }).click();
  const dialog = page.getByRole("dialog", { name: "Create attacker" });
  const networkCard = dialog.locator(".pg-v2-field-networks");

  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("Attacker label")).toBeVisible();
  await expect(dialog.getByRole("radio", { name: /Kali Rolling/ }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Create attacker" })).toBeVisible();
  // The live manifest replaces the old review preview for the minimal creators.
  await expect(dialog.locator(".pg-hud-manifest")).toContainText("attacker manifest");
  await expect(dialog.locator(".pg-hud-manifest")).toContainText("20 GB");
  await expect(networkCard.locator(".pg-v2-infobox")).toHaveText(
    /Auto-assigned: default → 10\.42\.1\.0\/24/,
  );
  await expect(dialog.locator(":scope > .pg-v2-infobox")).toHaveCount(0);
});

test("attacker creator omits sizing and posts role defaults @smoke", async ({ page }) => {
  let createBody;
  await page.route("**/api/v1/playground/vms", async (route) => {
    createBody = route.request().postDataJSON();
    await route.fulfill({
      json: {
        id: "vm-attacker",
        job_id: "job-test",
        label: createBody.label,
        image_id: createBody.image_id,
        state: "queued",
        domain_name: "astinuslab-pg-job-test-attacker",
        role: "attacker",
        created_at: new Date().toISOString(),
        nics: [],
        services: [],
      },
    });
  });

  await page.goto("/playground");
  await page.getByRole("button", { name: "+ Attacker" }).click();
  const dialog = page.getByRole("dialog", { name: "Create attacker" });
  await expect(dialog).toBeVisible();

  // Attacker creator is Label + Image + Networks only — no sizing controls; the
  // VM is created with the silent role default (2 vCPU).
  await expect(dialog.getByRole("slider")).toHaveCount(0);

  await page.getByRole("button", { name: "Create attacker" }).click();

  await expect.poll(() => createBody).toMatchObject({ role: "attacker", vcpus: 2 });
});

test("unsafe service URL is not rendered as a clickable href @smoke", async ({ page }) => {
  const currentSession = {
    id: "job-xss",
    job_id: "job-xss",
    state: "running",
    networks: [],
    storage: {
      global_label: "test storage",
      global_used_bytes: 0,
      global_total_bytes: 1000000000,
    },
    vms: [
      {
        id: "vm-xss",
        job_id: "job-xss",
        label: "XSS target",
        image_id: "ubuntu-22.04",
        state: "running",
        role: "target",
        ip: "10.42.1.10",
        os: "Ubuntu",
        nics: [],
        services: [
          {
            name: "bad service",
            port: 80,
            url: "javascript:alert(1)",
            status: "ready",
          },
          {
            name: "blank service",
            port: 81,
            url: "   ",
            status: "ready",
          },
          {
            name: "good service",
            port: 82,
            url: "http://10.42.1.10:82/",
            status: "ready",
          },
        ],
      },
    ],
  };

  await page.route("**/api/v1/playground/sessions/current-or-null", async (route) => {
    await route.fulfill({ json: currentSession });
  });
  await page.route("**/api/v1/playground/sessions/current", async (route) => {
    await route.fulfill({ json: currentSession });
  });

  await page.goto("/playground");

  const unsafeService = page.locator(".pg-v2-service-row", { hasText: "bad service" });
  await expect(unsafeService).toBeVisible();
  await expect(unsafeService.locator("a")).toHaveCount(0);
  await expect(
    unsafeService.locator(".pg-v2-service-url", { hasText: "javascript:alert(1)" }),
  ).toBeVisible();

  const blankService = page.locator(".pg-v2-service-row", { hasText: "blank service" });
  await expect(blankService).toBeVisible();
  await expect(blankService.locator("a")).toHaveCount(0);

  const goodService = page.locator(".pg-v2-service-row", { hasText: "good service" });
  await expect(goodService.getByRole("link", { name: "http://10.42.1.10:82/" })).toHaveAttribute(
    "href",
    "http://10.42.1.10:82/",
  );
});

test("workload launch review explains isolated-network preparation @smoke", async ({ page }) => {
  await page.goto("/playground");
  const dialog = await openTargetWizard(page);
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByRole("link", { name: "Prepare workloads" })).toHaveAttribute(
    "href",
    "/help/user/PREBAKED_IMAGES.md",
  );
  await dialog.locator(".pg-v2-workload-card", { hasText: "DVWA" }).click();
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog).toContainText("Form review");
  await expect(dialog).toContainText("Host readiness has not been checked");
  await expect(dialog).toContainText("Cache availability is not checked here");
  await expect(dialog).not.toContainText("all systems nominal");
  await expect(dialog.getByRole("button", { name: "Create target" })).toBeEnabled();
});

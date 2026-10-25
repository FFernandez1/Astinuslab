const { test, expect } = require("@playwright/test");

// Portable packages ship disabled (`packages_enabled = False`), so these specs
// used to skip themselves on a 404 -- silently, on every CI run, which meant the
// redistribution-consent gate was covered by tests that had never executed. Skip
// only when the feature was not asked for; when CI enables it, a missing page is
// a failure, not a shrug.
const PACKAGES_EXPECTED = ["1", "true", "yes", "on"].includes(
  String(process.env.ASTINUSLAB_PACKAGES_ENABLED || "").toLowerCase(),
);

async function openPackagesPage(page) {
  const response = await page.goto("/packages");
  if (response.status() === 404) {
    if (PACKAGES_EXPECTED) {
      throw new Error(
        "ASTINUSLAB_PACKAGES_ENABLED is set for this run but /packages returned 404: " +
          "the backend did not pick the feature flag up, so the package workflow is untested.",
      );
    }
    test.skip(true, "portable packages disabled; set ASTINUSLAB_PACKAGES_ENABLED=true to run this");
  }
}

test("Help button is absent and legacy URLs redirect to documentation @smoke", async ({ page }) => {
  await page.goto("/jobs");
  await expect(page.locator('.sidebar-item[href^="/help"]')).toHaveCount(0);

  const docsURL = `http://localhost:${process.env.ASTINUSLAB_DOCS_PORT || 9000}/usage/getting-started/`;
  // Check the real response without requiring a second running web server.
  const response = await page.request.get("/help/usage/getting-started.md", {
    maxRedirects: 0,
  });
  expect(response.status()).toBe(303);
  expect(response.headers().location).toBe(docsURL);
});

test("Creator generates editable JSON from the active Playground @smoke", async ({ page }) => {
  const definition = {
    format_version: 1,
    package_id: "org.example.lab",
    version: "1.0.0",
    title: "Lab",
    creator: "Author",
    license: "CC-BY-4.0",
    vms: [{ id: "web" }],
  };
  await page.route("**/api/v1/lab-packages", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/lab-packages/activity", (route) =>
    route.fulfill({ json: { prepares: [], instances: [], verifications: [] } }),
  );
  let posted;
  await page.route("**/api/v1/lab-packages/definitions/from-session", (route) => {
    posted = route.request().postDataJSON();
    return route.fulfill({
      json: { definition, source_session_id: "pg-authored", resources_derived: true },
    });
  });
  await openPackagesPage(page);
  await page.getByRole("tab", { name: "Create a package", exact: true }).click();
  await page.locator("#creator-meta-title").fill("Lab");
  await page.locator("#creator-meta-creator").fill("Author");
  await page.locator("#creator-meta-license").fill("CC-BY-4.0");
  await page.getByRole("button", { name: "Load from current Playground" }).click();
  await expect(page.locator("#creator-authored-session")).toHaveValue("pg-authored");
  await expect(page.locator("#creator-definition")).toHaveValue(
    JSON.stringify(definition, null, 2),
  );
  expect(posted.title).toBe("Lab");
  await expect(page.locator("#creator-load-status")).toContainText("resource estimates");
  await page.getByRole("button", { name: "Save draft and verify clean run", exact: true }).click();
  await expect(page.locator("#creator-runbook")).toBeFocused();
  await expect(page.getByText("Export within 24 hours", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Read the workflow guide" })).toHaveAttribute(
    "href",
    "/help/user/PORTABLE_LABS.md",
  );
});

test("Completed verifications export after reload with explicit consent @smoke", async ({
  page,
}) => {
  const attempts = ["verified-one", "verified-two"].map((id) => ({
    id,
    state: "verified",
    progress_percent: 100,
    package_id: "org.example.lab",
    package_version: "1.0.0",
    package_title: "Lab",
    attestation_status: "unexpired",
    attestation_expires_at: "2099-01-01T00:00:00Z",
  }));
  await page.route("**/api/v1/lab-packages", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/lab-packages/activity", (route) =>
    route.fulfill({ json: { prepares: [], instances: [], verifications: attempts } }),
  );
  const requests = [];
  await page.route("**/api/v1/lab-packages/verifications/*/export", (route) => {
    const payload = route.request().postDataJSON();
    requests.push({ url: route.request().url(), payload });
    if (!payload.embed_assets || !payload.redistribution_confirmed) {
      return route.fulfill({
        status: 409,
        json: {
          detail: {
            code: "embedding_required",
            message: "This verified package requires embedded layers and redistribution consent.",
          },
        },
      });
    }
    return route.fulfill({
      contentType: "application/vnd.astinuslab.lab+zip",
      headers: { "Content-Disposition": 'attachment; filename="verified-lab.astinuslab"' },
      body: "test archive bytes",
    });
  });
  await openPackagesPage(page);
  await page.reload();
  await page.getByRole("tab", { name: "Create a package", exact: true }).click();
  const history = page.locator("#verification-history");
  await expect(history.locator("[data-verification-export]")).toHaveCount(2);
  const row = history.locator('[data-verification-row="verified-two"]');
  const button = row.getByRole("button", { name: /Export verified package/ });
  await button.click();
  await expect(page.locator("#package-error")).toContainText("requires embedded layers");
  await expect(button).toBeEnabled();
  await row.locator("[data-export-embed]").check();
  await row.locator("[data-export-confirm]").check();
  const downloadPromise = page.waitForEvent("download");
  await button.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("verified-lab.astinuslab");
  await expect(button).toBeEnabled();
  expect(requests.at(-1).url).toContain("/verifications/verified-two/export");
  expect(requests.at(-1).payload).toEqual({ embed_assets: true, redistribution_confirmed: true });
});

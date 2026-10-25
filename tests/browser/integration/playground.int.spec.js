// Tier 2 — INTEGRATION: real backend + DB, NO route mocks, NO VM mutation.
//
// This host runs libvirt, so submitting a VM "create" would boot a REAL VM and
// pollute the live session. Therefore Tier 2 deliberately exercises only the
// real backend's read/validation paths and UI wiring, and stops strictly before
// any provisioning POST. Booting real VMs is exclusively Tier 3's job.
//
// To run against an isolated DB instead of the developer's real storage, start
// the backend first (reuseExistingServer picks it up), e.g.:
//   ASTINUSLAB_DB_PATH=/tmp/astinuslab-e2e/astinuslab.db \
//   ASTINUSLAB_STORAGE_DIR=/tmp/astinuslab-e2e/storage \
//   python -m uvicorn astinuslab.main:app --host 127.0.0.1 --port 8000
// then: npm run test:integration

const { test, expect } = require("@playwright/test");
const { collectBrowserErrors } = require("../helpers/backend");
const { openTargetWizard } = require("../helpers/target-wizard");

test("playground renders against the real backend without browser errors @integration", async ({
  page,
}) => {
  const errors = collectBrowserErrors(page);

  await page.goto("/playground");

  // Core controls render off real session state.
  await expect(page.getByRole("button", { name: "+ Target" })).toBeVisible();
  await expect(page.getByRole("button", { name: "+ Attacker" })).toBeVisible();
  await expect(page.getByLabel("Lab status")).toBeVisible();

  // The target wizard opens and is populated from the LIVE catalog (no mocks).
  const dialog = await openTargetWizard(page);
  await expect(dialog.locator(".pg-hud-manifest .pg-hud-mani__v").first()).not.toBeEmpty();

  // Stop before provisioning — never submit Create.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  expect(errors).toEqual([]);
});

test("real backend read endpoints return valid shapes @integration", async ({ request }) => {
  const images = await request.get("/api/v1/playground/catalog/images?role=target");
  expect(images.ok()).toBeTruthy();
  const imageList = await images.json();
  expect(Array.isArray(imageList)).toBeTruthy();
  expect(imageList.length).toBeGreaterThan(0);
  expect(imageList[0]).toHaveProperty("id");

  const cidr = await request.get("/api/v1/playground/networks/suggest-cidr");
  expect(cidr.ok()).toBeTruthy();
  expect((await cidr.json()).subnet_cidr).toMatch(/^10\.42\.\d+\.0\/24$/);

  const currentSession = await request.get("/api/v1/playground/sessions/current-or-null");
  expect(currentSession.ok()).toBeTruthy();
  let sessionBody = await currentSession.json();
  let createdSessionId = null;
  if (!sessionBody) {
    const session = await request.post("/api/v1/playground/sessions");
    expect(session.ok()).toBeTruthy();
    sessionBody = await session.json();
    createdSessionId = sessionBody.job_id;
  }

  try {
    const recipe = await request.get("/api/v1/playground/sessions/current/recipe");
    expect(recipe.ok()).toBeTruthy();
    expect(await recipe.json()).toHaveProperty("schema_version");
  } finally {
    if (createdSessionId) {
      const cleanup = await request.delete(`/api/v1/playground/sessions/${createdSessionId}`);
      expect(cleanup.ok()).toBeTruthy();
    }
  }
});

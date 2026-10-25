const { test, expect } = require("@playwright/test");
const { mockPlaygroundApi, targetImages } = require("../helpers/mocks");

for (const [role, opener, label] of [
  ["target", "+ Target", "Target"],
  ["attacker", "+ Attacker", "Attacker"],
  ["network", "+ Network Node", "Network Node"],
]) {
  test(`empty ${role} catalogue offers repair without creating a VM @smoke`, async ({ page }) => {
    await mockPlaygroundApi(page);
    let mutations = 0;
    page.on("request", (request) => {
      if (request.method() === "POST") mutations++;
    });
    await page.route(`**/catalog/images?role=${role}`, (route) => route.fulfill({ json: [] }));
    await page.goto("/playground");
    await page.getByRole("button", { name: opener, exact: true }).click();
    const dialog = page.locator("#create-vm-modal");
    await expect(dialog).toContainText(`No ${label} images available`);
    await expect(dialog.getByRole("link", { name: "Manage images", exact: true })).toHaveAttribute(
      "href",
      "/images",
    );
    await expect(dialog.getByRole("link", { name: "Image guide", exact: true })).toHaveAttribute(
      "href",
      "/help/user/PREBAKED_IMAGES.md",
    );
    await expect(
      page.locator(role === "target" ? "#create-vm-next" : "#create-vm-submit"),
    ).toBeDisabled();
    expect(mutations).toBe(0);
  });
}

test("loading, failure and retry retain the typed label and do not create a lab @smoke", async ({
  page,
}) => {
  await mockPlaygroundApi(page);
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let calls = 0;
  let mutations = 0;
  page.on("request", (request) => {
    if (request.method() === "POST") mutations++;
  });
  await page.route("**/catalog/images?role=target", async (route) => {
    calls++;
    if (calls === 1) {
      await gate;
      return route.fulfill({ status: 503, json: { detail: "fixture unavailable" } });
    }
    return route.fulfill({ json: targetImages });
  });
  await page.goto("/playground");
  await page.getByRole("button", { name: "+ Target", exact: true }).click();
  const dialog = page.locator("#create-vm-modal");
  await expect(dialog).toContainText("Loading Target images");
  await dialog.getByLabel("target label", { exact: true }).fill("retained-name");
  release();
  await dialog.getByRole("button", { name: "Retry loading images" }).click();
  await expect(dialog.getByRole("radio", { name: /Ubuntu/ })).toBeVisible();
  await expect(dialog.getByLabel("target label", { exact: true })).toHaveValue("retained-name");
  await expect(dialog).toContainText("Form complete — host readiness not checked");
  await expect(dialog).not.toContainText("ready — launch on review");
  await page.locator("#create-vm-next").click();
  await expect(dialog).toContainText("No workload");
  await dialog.locator('[data-value="dvwa"]').click();
  await page.locator("#create-vm-next").click();
  await page.locator("#create-vm-next").click();
  await expect(dialog).toContainText("Cache availability is not checked here");
  await expect(dialog).toContainText("Egress-enabled networks allow outbound traffic");
  expect(mutations).toBe(0);
});

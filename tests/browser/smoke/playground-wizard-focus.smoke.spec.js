const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");
const { mockPlaygroundApi } = require("../helpers/mocks");

test.beforeEach(async ({ page }) => {
  await mockPlaygroundApi(page);
});

test("label input keeps focus across a session poll @smoke", async ({ page }) => {
  await page.goto("/playground");
  await page.getByRole("button", { name: "+ Target" }).click();
  const dialog = page.getByRole("dialog", { name: "Create target" });
  await expect(dialog).toBeVisible();

  const label = dialog.getByLabel("target label");
  await label.click();
  await label.type("web");
  await expect(label).toBeFocused();

  await page.waitForTimeout(6000);

  await expect(label).toBeFocused();
  await expect(label).toHaveValue("web");
});

test("image group has one Tab stop and arrow selection preserves wizard context @smoke", async ({
  page,
}) => {
  const workloadImages = [];
  let mutations = 0;
  page.on("request", (request) => {
    if (request.method() === "POST") mutations++;
  });
  await page.route("**/catalog/images?role=target", (route) =>
    route.fulfill({
      json: [
        { id: "ubuntu", label: "Ubuntu fixture" },
        { id: "debian", label: "Debian fixture" },
      ],
    }),
  );
  await page.route("**/catalog/workloads?image_id=**", (route) => {
    workloadImages.push(new URL(route.request().url()).searchParams.get("image_id"));
    return route.fulfill({ json: [] });
  });
  await page.goto("/playground");
  const opener = page.getByRole("button", { name: "+ Target", exact: true });
  await opener.click();
  const dialog = page.locator("#create-vm-modal");
  const group = dialog.getByRole("radiogroup", { name: "Target OS image", exact: true });
  await expect(group).toBeVisible();
  const label = dialog.getByLabel("target label", { exact: true });
  await label.fill("keyboard-lab");
  await label.press("Tab");
  await page.keyboard.press("Tab");
  await expect(group.getByRole("radio", { name: /Ubuntu/ })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  const selected = group.getByRole("radio", { name: /Debian/ });
  await expect(selected).toBeFocused();
  await expect(selected).toHaveAttribute("aria-checked", "true");
  await expect.poll(() => workloadImages.at(-1)).toBe("debian");
  await page.keyboard.press("Tab");
  await expect(selected).not.toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(selected).toBeFocused();
  await page.keyboard.press("Space");
  await expect(label).toHaveValue("keyboard-lab");
  expect(mutations).toBe(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

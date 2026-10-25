const { test, expect } = require("@playwright/test");
const { mockPlaygroundApi } = require("../helpers/mocks");
const { NAV } = require("../helpers/nav");

// Guards core product language against drift. AstinusLab's front-door
// workflow and attack-and-forensics framing depend on stable navigation
// copy (Playground, Jobs, Attacker, etc.). If these terms get renamed or
// dropped, the product direction silently erodes — this catches that.

test("sidebar exposes all canonical product terms @smoke", async ({ page }) => {
  await mockPlaygroundApi(page);
  await page.goto("/playground");

  const sidebar = page.locator("nav.sidebar");
  await expect(sidebar).toBeVisible();

  for (const { path, label } of NAV) {
    const link = sidebar.locator(`a.sidebar-item[href="${path}"]`);
    await expect(link).toContainText(label);
  }
});

test("playground front door names the lab workflow @smoke", async ({ page }) => {
  await mockPlaygroundApi(page);
  await page.goto("/playground");

  // Playground is the front-door workflow; title and header label must say so.
  await expect(page).toHaveTitle(/Playground/);
  await expect(page.locator(".pg-header-label")).toContainText("Playground");
});

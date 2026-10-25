const { test, expect } = require("@playwright/test");
const { collectBrowserErrors, mockPlaygroundApi } = require("../helpers/mocks");
const { NAV } = require("../helpers/nav");

for (const { path } of NAV) {
  test(`page ${path} loads without browser errors @smoke`, async ({ page }) => {
    const errors = collectBrowserErrors(page);

    // /playground fetches /sessions/current on load, which returns 404 by
    // design when idle (no active session). The browser logs that as a
    // console error. Mock it to an idle session so this no-error guard
    // tests the page, not the deliberate idle-state 404.
    if (path === "/playground") await mockPlaygroundApi(page);

    await page.goto(path);

    await expect(page.locator("body")).toBeVisible();
    expect(errors).toEqual([]);
  });
}

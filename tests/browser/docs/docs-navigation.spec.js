/* global document */
const { test, expect } = require("@playwright/test");

for (const locale of ["", "es/", "fr/", "ca/"]) {
  test(`mobile documentation keeps the article above the fold: ${locale || "en"}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/${locale}usage/getting-started/`);
    const heading = page.locator("article h1");
    await expect(heading).toBeInViewport();
    const before = await heading.boundingBox();
    expect(before.y).toBeLessThan(200);
    await expect(page.locator(".astinuslab-nav")).toBeHidden();
    const menu = page.locator(".astinuslab-drawer");
    await menu.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#__drawer")).toBeChecked();
    await expect(page.locator(".md-sidebar--primary")).toBeInViewport();
    expect((await heading.boundingBox()).y).toBe(before.y);
    await page.keyboard.press("Escape");
    await expect(page.locator("#__drawer")).not.toBeChecked();
    await expect(menu).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      390,
    );
  });

  test(`search shortcut focuses the query and restores focus: ${locale || "en"}`, async ({
    page,
  }) => {
    await page.goto(`/${locale}`);
    const trigger = page.locator(".astinuslab-search-trigger");
    const query = page.locator('[data-md-component="search-query"]');
    await trigger.focus();
    await page.keyboard.press("Control+k");
    await expect(page.locator("#__search")).toBeChecked();
    await expect(query).toBeFocused();
    await query.fill("capture");
    await page.keyboard.press("Escape");
    await expect(page.locator("#__search")).not.toBeChecked();
    await expect(trigger).toBeFocused();
    await page.keyboard.press("Meta+k");
    await expect(query).toBeFocused();
  });
}

test("search shortcut does not steal editing shortcuts", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() => {
    const input = document.createElement("input");
    input.id = "shortcut-editor";
    document.querySelector("article").prepend(input);
  });
  await page.locator("#shortcut-editor").focus();
  await page.keyboard.press("Control+k");
  await expect(page.locator("#__search")).not.toBeChecked();
  await expect(page.locator("#shortcut-editor")).toBeFocused();
});

test("published documentation and search omit internal review records", async ({ request }) => {
  for (const locale of ["", "es/", "fr/", "ca/"]) {
    const response = await request.get(`/${locale}search/search_index.json`);
    expect(response.ok()).toBeTruthy();
    const index = await response.json();
    expect(index.docs.length).toBeGreaterThan(0);
    const internal = index.docs.filter((doc) =>
      /(?:^|\/)(?:audits|remediation|plans|superpowers)\//.test(doc.location),
    );
    expect(internal.map((doc) => doc.location)).toEqual([]);
  }
  expect((await request.get("/remediation/capacity/")).status()).toBe(404);
});

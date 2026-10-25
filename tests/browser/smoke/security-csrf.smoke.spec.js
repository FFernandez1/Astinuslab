/* global fetch, window */
const { test, expect } = require("@playwright/test");

test("CSRF cookie, forms, HTMX, and fetch clients stay wired @smoke", async ({ page }) => {
  await page.goto("/jobs/new");

  const metaToken = await page
    .locator('meta[name="astinuslab-csrf-token"]')
    .getAttribute("content");
  expect(metaToken).toBeTruthy();
  await expect(page.locator('form[action="/jobs/new"] input[name="csrf_token"]')).toHaveValue(
    metaToken,
  );

  const missing = await page.evaluate(async () => {
    const response = await fetch("/set-lang", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "lang=en&next=%2Fjobs",
      redirect: "manual",
    });
    return { status: response.status, body: await response.json() };
  });
  expect(missing.status).toBe(403);
  expect(missing.body.code).toBe("csrf_required");

  let htmxHeader;
  await page.route("**/__csrf_htmx_probe", async (route) => {
    htmxHeader = route.request().headers()["x-csrf-token"];
    await route.fulfill({ status: 204, body: "" });
  });
  await page.evaluate(() => window.htmx.ajax("POST", "/__csrf_htmx_probe"));
  await expect.poll(() => htmxHeader).toBe(metaToken);

  let playgroundHeader;
  await page.route("**/api/v1/playground/networks", async (route) => {
    playgroundHeader = route.request().headers()["x-csrf-token"];
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.evaluate(async () => {
    const api = await import("/static/playground/api.js");
    await api.createNetwork({ name: "csrf-probe", subnet_cidr: "10.77.0.0/24", isolated: true });
  });
  expect(playgroundHeader).toBe(metaToken);
});

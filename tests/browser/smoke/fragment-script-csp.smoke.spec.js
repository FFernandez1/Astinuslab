/* global window, URL */
const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

// An inline <script> inside an htmx-swapped fragment carries that fragment
// response's CSP nonce, never the host page's, so `script-src 'self' 'nonce-…'`
// refuses it and the swapped-in controls stay unbound. The delete-job modal
// used to bind that way and went dead five seconds after /jobs loaded. Its
// behavior now lives in a page-level file delegated on document; this drives a
// real swap under a real nonce policy to prove it survives one.
const STATIC_ROOT = path.resolve(__dirname, "../../../src/astinuslab/web/static");
const TEMPLATES = path.resolve(__dirname, "../../../src/astinuslab/web/templates");
const PAGE_NONCE = "page-nonce-value";

// The real partial, with Jinja stripped: the test tracks the shipped markup
// rather than a copy that can drift away from it.
const modalMarkup = fs
  .readFileSync(path.join(TEMPLATES, "partials/delete_job_modal.html"), "utf8")
  .replace(/\{#[\s\S]*?#\}/g, "")
  .replace(/\{%[\s\S]*?%\}/g, "")
  .replace(/\{\{[\s\S]*?\}\}/g, "x");

const jobsTable = (swapCount) => `
  <div id="jobs-table" hx-get="/jobs-table-fragment" hx-trigger="every 120ms" hx-swap="outerHTML">
    <span id="swap-count">${swapCount}</span>
    <form id="bulk-delete-form" action="/bulk" method="post"></form>
    <button type="button" data-delete-job-id="job-abc" data-delete-job-row="#row">Delete</button>
    <div id="row">row</div>
    ${modalMarkup}
  </div>`;

test("delete-job modal still works after an htmx fragment swap @smoke", async ({ page }) => {
  await page.clock.install();
  await page.route("**/static/**", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: fs.readFileSync(
        path.join(STATIC_ROOT, new URL(route.request().url()).pathname.split("/").pop()),
      ),
    }),
  );

  let swaps = 0;
  await page.route("**/jobs-table-fragment", (route) => {
    swaps += 1;
    route.fulfill({
      contentType: "text/html",
      // A fresh nonce per response is exactly what breaks an inline fragment script.
      headers: {
        "Content-Security-Policy": `default-src 'self'; script-src 'self' 'nonce-fragment-${swaps}'`,
      },
      body: jobsTable(swaps),
    });
  });

  let deleteRequest = null;
  await page.route("**/jobs/job-abc/delete**", (route) => {
    deleteRequest = route.request().url();
    route.fulfill({ contentType: "text/html", body: "<div id='row'>deleted</div>" });
  });

  await page.route("**/host", (route) =>
    route.fulfill({
      contentType: "text/html",
      headers: {
        "Content-Security-Policy": `default-src 'self'; script-src 'self' 'nonce-${PAGE_NONCE}'`,
      },
      body: `<html><head>
        <meta name="astinuslab-csrf-token" content="tok">
        <script src="/static/htmx.min.js"></script>
        <script src="/static/dialog.js"></script>
        <script src="/static/delete-job-modal.js" defer></script>
        <!-- base.html ships this on every page; it is what holds the jobs-table
             poll while a modal is open, so the fixture needs it to be faithful. -->
        <script src="/static/state_preservation.js" defer></script>
      </head><body>${jobsTable(0)}</body></html>`,
    }),
  );

  await page.goto("/host");
  const overlay = page.locator("#delete-job-modal-overlay");

  // Wait for at least one poll to replace the modal nodes the page shipped with.
  await expect.poll(() => page.locator("#swap-count").textContent()).not.toBe("0");

  await page.locator("[data-delete-job-id]").click();
  await expect(overlay).toHaveClass(/modal-overlay--open/);

  // Cancel was bound imperatively in the old inline script and died on the swap.
  await page.locator('.modal-footer [data-action="close-delete-job-modal"]').click();
  await expect(overlay).not.toHaveClass(/modal-overlay--open/);

  await page.locator("[data-delete-job-id]").click();
  await expect(overlay).toHaveClass(/modal-overlay--open/);
  // With the overlay open the jobs-table poll is held, so the checkbox the
  // operator ticks is still the one the confirm handler reads.
  const swapsWhileOpen = swaps;
  await page.locator("#delete-files-checkbox").check();
  await page.clock.fastForward(500);
  await expect(overlay).toHaveClass(/modal-overlay--open/);
  await expect(page.locator("#delete-files-checkbox")).toBeChecked();
  expect(swaps).toBe(swapsWhileOpen);

  // Confirm captures the checkbox then closes the dialog; normal polling may
  // resume before Playwright's click resolves, so its count is no longer frozen.
  await page.locator("#confirm-delete-btn").click();
  await expect.poll(() => deleteRequest).toContain("delete_files=true");
  await expect(overlay).not.toHaveClass(/modal-overlay--open/);
});

test("classic scripts can reach the CSRF header helper @smoke", async ({ page }) => {
  await page.goto("/jobs");
  const token = await page.locator('meta[name="astinuslab-csrf-token"]').getAttribute("content");

  // job-live.js and the image-vault page script are classic scripts: they cannot
  // import csrfHeaders, and every unsafe method they send without it is a 403.
  const headers = await page.evaluate(() => ({
    available: typeof window.csrfHeaders === "function",
    post: window.csrfHeaders("POST", { Accept: "application/json" }),
    get: window.csrfHeaders("GET"),
  }));
  expect(headers.available).toBe(true);
  expect(headers.post["X-CSRF-Token"]).toBe(token);
  expect(headers.post.Accept).toBe("application/json");
  expect(headers.get["X-CSRF-Token"]).toBeUndefined();
});

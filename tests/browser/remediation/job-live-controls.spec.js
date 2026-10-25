/* global window, document, Event, URL, Buffer */
const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

// Browser-level presentation regression. Host acceptance is recorded separately.
test("status transitions refresh controls without losing forms @smoke", async ({ page }) => {
  const staticRoot = path.resolve(__dirname, "../../../src/astinuslab/web/static");
  await page.route("**/static/**", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: fs.readFileSync(
        path.join(staticRoot, new URL(route.request().url()).pathname.split("/").pop()),
      ),
    }),
  );
  const section = (
    ready,
  ) => `<div id="live-section" data-job-id="fixture" data-cancel-confirm="Cancel?">
    <div id="live-status" hx-get="/poll" hx-trigger="every 100ms" hx-swap="outerHTML"><span data-live-status-label>${ready ? "Ready" : "Booting"}</span></div>
    <div id="manual-override-fixture" hx-preserve><input id="retained-input" value=""></div>
    ${ready ? '<button id="btn-cancel-lab-fixture">Cancel lab</button><form id="cancel-fixture" action="/cancel" method="post"><input type="hidden" name="csrf_token" value="fixture-token"></form>' : ""}
    </div>`;
  let changed = false;
  await page.route("**/poll", (route) =>
    route.fulfill({
      contentType: "text/html",
      headers: changed ? { "HX-Retarget": "#live-section", "HX-Reswap": "outerHTML" } : {},
      body: changed
        ? section(true)
        : '<div id="live-status" hx-get="/poll" hx-trigger="every 100ms" hx-swap="outerHTML">Booting</div>',
    }),
  );
  await page.route("**/fixture", (route) =>
    route.fulfill({
      contentType: "text/html",
      headers: { "Content-Security-Policy": "default-src 'self'; script-src 'self'" },
      body: `<html><head><script src="/static/htmx.min.js"></script><script src="/static/job-live.js" defer></script></head><body><span data-live-job-status>Booting</span><textarea id="script">original</textarea>${section(false)}</body></html>`,
    }),
  );
  await page.goto("/fixture");
  await page.locator("#script").fill("operator script");
  await page.locator("#retained-input").fill("selected state");
  await page.evaluate(() => {
    window.confirmations = 0;
    window.confirmDialog = async () => {
      window.confirmations++;
      return true;
    };
  });
  changed = true;
  await expect(page.getByRole("button", { name: "Cancel lab" })).toBeVisible();
  await expect(page.locator("[data-live-job-status]")).toHaveText("Ready");
  await expect(page.locator("#script")).toHaveValue("operator script");
  await expect(page.locator("#retained-input")).toHaveValue("selected state");
  changed = false;
  // Repeated unrelated swaps must not bind the same button twice.
  await page.evaluate(() => {
    document.dispatchEvent(new Event("htmx:afterSwap"));
    document.dispatchEvent(new Event("htmx:afterSwap"));
  });
  let submitted;
  await page.route("**/cancel", (route) => {
    submitted = route.request().postData();
    return route.fulfill({ status: 204 });
  });
  await page.getByRole("button", { name: "Cancel lab" }).click();
  await expect.poll(() => submitted).toBe("csrf_token=fixture-token");
  expect(await page.evaluate(() => window.confirmations)).toBe(1);
});

test("initial lab controls wait for deferred confirmation support @smoke", async ({ page }) => {
  const staticRoot = path.resolve(__dirname, "../../../src/astinuslab/web/static");
  await page.route("**/job-live.js", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: fs.readFileSync(path.join(staticRoot, "job-live.js")),
    }),
  );
  await page.route("**/confirm-support.js", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.confirmations=0;window.confirmDialog=async()=>{window.confirmations++;return true;};",
    }),
  );
  await page.route("**/initial-fixture", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: `
    <html><head><script src="/job-live.js" defer></script><script src="/confirm-support.js" defer></script></head>
    <body><div id="live-section" data-job-id="initial" data-cancel-confirm="Cancel?">
    <button id="btn-cancel-lab-initial">Cancel lab</button><form id="cancel-initial" action="/cancel-initial" method="post"></form>
    </div></body></html>`,
    }),
  );
  let submitted = 0;
  await page.route("**/cancel-initial", (route) => {
    submitted++;
    return route.fulfill({ status: 204 });
  });
  await page.goto("/initial-fixture");
  await page.getByRole("button", { name: "Cancel lab" }).click();
  await expect.poll(() => submitted).toBe(1);
  expect(await page.evaluate(() => window.confirmations)).toBe(1);
});

// ── Failure copy on the live Job page ────────────────────────────────────────
// Every control here acts on a real machine, and each one can be refused for a
// reason the operator has to act on. The page is served in four languages; its
// failure copy was written in one, and the reset control threw away the
// server's explanation before anyone could read it.

const STATIC_ROOT = path.resolve(__dirname, "../../../src/astinuslab/web/static");
// Deliberately not English: a literal in the source and a dictionary lookup are
// indistinguishable while the page is served in the language the literal is
// written in.
const ES = {
  job_reset_failed: "No s'ha pogut reiniciar",
  job_upload_failed: "Ha fallat la pujada de {item}",
  job_upload_item_dump: "Bolcat de memòria",
};

async function servePage(page, body) {
  // Before any page script: job-live.js captures window.confirmDialog when it
  // wires its handlers, so setting it after the load leaves every confirmed
  // control answering "no" and the test asserting against a request that was
  // never sent.
  await page.addInitScript(() => {
    window.confirmDialog = async () => true;
  });
  await page.route("**/static/**", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: fs.readFileSync(
        path.join(STATIC_ROOT, new URL(route.request().url()).pathname.split("/").pop()),
      ),
    }),
  );
  await page.route("**/live-fixture", (route) =>
    route.fulfill({
      // With no charset the accented copy in the dictionary below is decoded as
      // latin-1 and the assertion fails on mojibake rather than on the product.
      contentType: "text/html; charset=utf-8",
      body: `<html><head>
        <script type="application/json" id="al-i18n">${JSON.stringify(ES)}</script>
        <script src="/static/i18n.js"></script>
        <script src="/static/toast.js" defer></script>
        <script src="/static/job-live.js" defer></script>
      </head><body>
        <div id="al-toast-root" class="toast-root" aria-live="assertive"></div>
        ${body}
      </body></html>`,
    }),
  );
  await page.goto("/live-fixture");
}

const RESET_SECTION = `<div id="live-section" data-job-id="live" data-reset-confirm="Reset?">
  <button id="btn-reset-lab-live">Reset lab</button></div>`;

test("a refused lab reset repeats the server's reason", async ({ page }) => {
  await servePage(page, RESET_SECTION);
  const detail = "a capture is still in flight on this lab";
  await page.route("**/jobs/live/reset-lab", (route) =>
    route.fulfill({ status: 409, json: { detail } }),
  );

  await page.getByRole("button", { name: "Reset lab" }).click();
  // Not a status code, and not the word "Error": the operator is told the one
  // thing they can act on.
  await expect(page.locator("#al-toast-root .toast--error")).toHaveText(detail);
});

test("a refused lab reset with no reason names the action from the dictionary", async ({
  page,
}) => {
  await servePage(page, RESET_SECTION);
  await page.route("**/jobs/live/reset-lab", (route) => route.fulfill({ status: 503, body: "" }));

  await page.getByRole("button", { name: "Reset lab" }).click();
  await expect(page.locator("#al-toast-root .toast--error")).toHaveText(ES.job_reset_failed);
});

test("a refused dump upload states which upload failed, in the page's language", async ({
  page,
}) => {
  await servePage(
    page,
    `<div id="live-section" data-job-id="live">
       <div id="manual-override-live">
         <span data-upload-status hidden></span>
         <button data-upload-dump>Upload</button>
         <input type="file" data-upload-dump-input>
       </div></div>`,
  );
  await page.route("**/api/v1/jobs/live/dump/upload", (route) =>
    route.fulfill({ status: 500, body: "" }),
  );

  await page.locator("[data-upload-dump-input]").setInputFiles({
    name: "memory.lime",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("not a real dump"),
  });

  const status = page.locator("[data-upload-status]");
  await expect(status).toBeVisible();
  await expect(status).toHaveText(ES.job_upload_failed.replace("{item}", ES.job_upload_item_dump));
  await expect(status).toHaveClass(/text-danger/);
});

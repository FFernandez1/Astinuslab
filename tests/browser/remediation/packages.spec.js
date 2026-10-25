const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");

async function setup(page) {
  const attempt = (id, state) => ({
    id,
    state,
    draft_id: `draft-${id}`,
    progress_percent: 100,
    authored_session_id: null,
    error_code: null,
    error_summary: null,
    package_id: "quality",
    package_title: "Quality lab",
    package_version: "1.0",
    attestation_status: state === "verified" ? "unexpired" : "unavailable",
    attestation_expires_at: state === "verified" ? "2099-01-01T00:00:00Z" : null,
  });
  const running = attempt("v-running", "running");
  const failed = {
    ...attempt("v-failed", "failed"),
    error_code: "checkpoint_failed",
    error_summary:
      "A clean-run checkpoint did not pass. Review the checkpoint definition and guest state before rebuilding the lab.",
  };
  const instance = {
    id: "instance-1",
    package_record_id: "package-1",
    state: "ready",
    session_id: "owned-session",
  };
  const packages = [1, 2].map((n) => ({
    id: `package-${n}`,
    package_id: `quality-${n}`,
    version: "1.0",
    title: n === 1 ? "Quality lab" : "Other lab",
    archive_sha256: `digest-${n}`,
    archive_size_bytes: 1024,
    manifest_sha256: `manifest-${n}`,
    signature_status: "unsigned",
    reproducibility: "unlocked",
  }));
  const fixture = {
    resets: 0,
    refuse: false,
    polls: 0,
    running,
    failed,
    instance,
    currentSession: "owned-session",
    verifications: [attempt("v-newer", "verified"), running, failed],
    exports: [],
  };
  await page.route("**/api/v1/playground/sessions/current-or-null", (route) =>
    route.fulfill({ json: { session_id: fixture.currentSession } }),
  );
  await page.route("**/api/v1/lab-packages**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1/lab-packages", "");
    if (!path)
      return route.fulfill({
        json: packages,
      });
    if (path === "/activity")
      return route.fulfill({
        json: {
          prepares: [],
          instances: [instance],
          verifications: fixture.verifications,
          recent_terminal_verification_count: 1,
        },
      });
    if (path === "/verifications/v-running") {
      fixture.polls++;
      return route.fulfill({ json: running });
    }
    if (path.endsWith("/export")) {
      fixture.exports.push(route.request().postDataJSON());
      return route.fulfill({ status: 409, json: { detail: "embedding_required" } });
    }
    if (path === "/operations/instance-1/reset") {
      fixture.resets++;
      return fixture.refuse
        ? route.fulfill({ status: 409, json: { detail: "Reset refused by fixture" } })
        : route.fulfill({ json: instance });
    }
    if (path === "/operations/instance-1") return route.fulfill({ json: instance });
    if (path.endsWith("/experience")) {
      const pkg = packages.find((item) => path === `/${item.id}/experience`);
      return route.fulfill({
        json: {
          ...pkg,
          package_record_id: pkg.id,
          runbook: `# Retained ${pkg.title}\n<script>window.BAD_PACKAGE = true</script>\n[bad](javascript:alert(1))`,
          experience: {
            objectives: ["Read the evidence"],
            hints: ["Start with the process list"],
            checkpoints: [],
          },
          resources: { minimum: { cpu_threads: 2, memory_mb: 2048, free_disk_bytes: 4294967296 } },
        },
      });
    }
    if (path.endsWith("/preflight"))
      return route.fulfill({
        json: {
          ready: true,
          errors: [],
          warnings: [],
          required_disk_bytes: 4294967296,
          retained_disk_bytes: 1024,
          launch_disk_bytes: 4294966272,
        },
      });
    throw new Error(`Unexpected fixture request: ${path}`);
  });
  await page.clock.install();
  await page.goto("/packages");
  await expect(page.getByLabel("Verification attempt", { exact: true })).toHaveValue("v-running");
  return fixture;
}

test("export versions, expiry and authoritative refusal preserve consent", async ({ page }) => {
  const fixture = await setup(page);
  const verified = fixture.verifications[0];
  const deadline = Date.now() + 60000;
  await page.clock.setFixedTime(new Date(deadline - 60000));
  fixture.verifications = [
    { ...verified, id: "version-one", attestation_expires_at: new Date(deadline).toISOString() },
    { ...verified, id: "version-two", package_version: "2.0" },
    { ...verified, id: "expired", attestation_status: "expired" },
    fixture.running,
  ];
  await page.reload();
  await page.getByRole("tab", { name: "Create a package", exact: true }).click();
  const first = page.locator('[data-verification-row="version-one"]');
  const second = page.locator('[data-verification-row="version-two"]');
  await expect(first).toContainText("quality@1.0");
  await expect(second).toContainText("quality@2.0");
  await expect(page.locator('[data-verification-export="expired"]')).toBeDisabled();
  await second.locator("[data-export-confirm]").check();
  await second.locator("button").click();
  await expect(second).toContainText("embedding_required");
  await expect(second.locator("[data-export-confirm]")).toBeChecked();
  await expect(second.locator("[data-export-embed]")).not.toBeChecked();
  expect(fixture.exports).toEqual([{ embed_assets: false, redistribution_confirmed: true }]);
  await first.locator("[data-export-embed]").check();
  await page.clock.setFixedTime(new Date(deadline + 1));
  await page.clock.runFor(60001);
  await expect(first.locator("button")).toBeDisabled();
  await expect(first).toContainText("Attestation expired");
  await expect(first.locator("[data-export-embed]")).toBeChecked();
});

for (const width of [1440, 768]) {
  test(`verification failure survives reload and selection at ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await setup(page);
    await page.getByRole("tab", { name: "Create a package", exact: true }).click();
    const panel = page.locator("#verification-status");
    const copy = panel.getByRole("button", { name: "Copy verification ID" });
    await copy.focus();
    await page.evaluate(() => {
      globalThis.qualityAnnouncements = 0;
      new globalThis.MutationObserver(() => globalThis.qualityAnnouncements++).observe(
        globalThis.document.querySelector("#verification-status"),
        { childList: true, subtree: true },
      );
    });
    const polls = fixture.polls;
    await page.clock.fastForward(5100);
    await expect.poll(() => fixture.polls).toBeGreaterThan(polls);
    await expect(copy).toBeFocused();
    expect(await page.evaluate(() => globalThis.qualityAnnouncements)).toBe(0);
    const selector = page.getByLabel("Verification attempt", { exact: true });
    await selector.focus();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect(selector).toHaveValue("v-failed");
    await expect(panel).toContainText(fixture.failed.error_summary);
    await expect(panel.getByRole("button", { name: /Export/ })).toHaveCount(0);
    await expect(page.locator("#verification-history [data-verification-export]")).toHaveCount(1);
    await expect.poll(() => page.evaluate(() => globalThis.qualityAnnouncements)).toBe(1);
    await page.clock.fastForward(10000);
    expect(await page.evaluate(() => globalThis.qualityAnnouncements)).toBe(1);
    await page.screenshot({
      path: testInfo.outputPath(`verification-failure-${width}.png`),
      fullPage: true,
    });
    await page.reload();
    await page.getByRole("tab", { name: "Create a package", exact: true }).click();
    await selector.selectOption("v-failed");
    await expect(panel).toContainText(fixture.failed.error_summary);
  });
}

for (const choice of ["Escape", "Cancel", "Confirm"]) {
  test(`reset requires confirmation and restores focus after ${choice}`, async ({ page }) => {
    const fixture = await setup(page);
    const reset = page.getByRole("button", { name: "Reset", exact: true });
    await reset.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("Quality lab");
    await expect(dialog).toContainText("Current VM changes will be discarded");
    await expect(dialog).toContainText("Captured evidence is kept.");
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
    if (choice === "Escape") await page.keyboard.press("Escape");
    else await dialog.getByRole("button", { name: choice, exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(reset).toBeFocused();
    expect(fixture.resets).toBe(choice === "Confirm" ? 1 : 0);
  });
}

test("reset refusal stays retryable with its cause", async ({ page }) => {
  const fixture = await setup(page);
  fixture.refuse = true;
  for (let n = 1; n <= 2; n++) {
    await page.getByRole("button", { name: "Reset", exact: true }).click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Confirm", exact: true })
      .click();
    await expect(page.locator("#package-error")).toContainText("Reset refused by fixture");
    await expect.poll(() => fixture.resets).toBe(n);
  }
});

test("recipient instructions reopen safely with keyboard navigation at 768px", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 768, height: 1000 });
  await setup(page);
  await expect(page.locator("#creator-verify-form")).toBeHidden();
  const use = page.getByRole("tab", { name: "Use a package", exact: true });
  await use.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Create a package", exact: true })).toBeFocused();
  await expect(page.locator("#creator-verify-form")).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  const open = page
    .locator('[data-package-id="package-1"]')
    .getByRole("button", { name: "Open package", exact: true });
  await open.click();
  const detail = page.locator("#package-detail");
  await expect(detail.getByRole("heading", { level: 3 })).toBeFocused();
  await expect(detail).toContainText("Retained Quality lab");
  await expect(detail).toContainText("<script>window.BAD_PACKAGE = true</script>");
  await expect(detail.locator("script, [href^='javascript:']")).toHaveCount(0);
  expect(await page.evaluate(() => globalThis.BAD_PACKAGE)).toBeUndefined();
  await expect(
    page.locator("#package-library").getByRole("button", { name: "Prepare", exact: true }),
  ).toHaveCount(0);
  await detail.getByRole("link", { name: "Back to local library", exact: true }).click();
  await expect(page.locator("#library-heading")).toBeFocused();
  await page.reload();
  await open.click();
  await expect(detail).toContainText("Retained Quality lab");
  await page.screenshot({ path: testInfo.outputPath("package-recipient-768.png"), fullPage: true });
});

test("late instructions cannot replace selection and runtime keeps its own package name", async ({
  page,
}) => {
  await setup(page);
  let release, started;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const pending = new Promise((resolve) => {
    started = resolve;
  });
  await page.route("**/package-2/experience", async (route) => {
    started();
    await gate;
    return route.fallback();
  });
  await page
    .locator('[data-package-id="package-2"]')
    .getByRole("button", { name: "Open package", exact: true })
    .click();
  await pending;
  await page
    .locator('[data-package-id="package-1"]')
    .getByRole("button", { name: "Open package", exact: true })
    .click();
  await expect(page.locator("#package-detail")).toContainText("Retained Quality lab");
  release();
  await expect(page.locator("#package-detail")).not.toContainText("Other lab");
  await page
    .locator('[data-package-id="package-2"]')
    .getByRole("button", { name: "Open package", exact: true })
    .click();
  await expect(page.locator("#package-detail")).toContainText("Retained Other lab");
  await page
    .locator("#package-detail")
    .getByRole("button", { name: "Preflight", exact: true })
    .click();
  await expect(page.locator("#package-action-status")).toContainText("Other lab · quality-2@1.0");
  await expect(page.locator("#package-action-status")).toContainText("4 GiB");
  await expect(page.locator("#package-operation-status")).toContainText(
    "Quality lab · quality-1@1.0",
  );
});

test("Playground handoff verifies the current session and cleanup has no invented repair action", async ({
  page,
}) => {
  const fixture = await setup(page);
  fixture.currentSession = "unrelated";
  await page.getByRole("button", { name: "Open lab in Playground", exact: true }).click();
  await expect(page.locator("#package-error")).toContainText(
    "no longer the active Playground session",
  );
  await expect(page).toHaveURL(/\/packages$/);
  fixture.currentSession = "owned-session";
  await page.route("**/playground", (route) =>
    route.fulfill({ contentType: "text/html", body: "<h1>Matched Playground</h1>" }),
  );
  await page.getByRole("button", { name: "Open lab in Playground", exact: true }).click();
  await expect(page).toHaveURL(/\/playground$/);
  fixture.instance.state = "cleanup_incomplete";
  await page.goto("/packages");
  const operation = page.locator("#package-operation-status");
  await expect(operation).toContainText("Cleanup incomplete. Resources may still be reserved.");
  await expect(operation.getByRole("link", { name: "Logs", exact: true })).toHaveAttribute(
    "href",
    "/logs",
  );
  await expect(operation.getByRole("button", { name: /Retry|Reset|Open lab/ })).toHaveCount(0);
});

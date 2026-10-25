const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");

async function setup(page) {
  const vm = {
    id: "ui-vm",
    label: "UI fixture VM",
    image_id: "ubuntu-22.04",
    role: "target",
    state: "running",
    ip: "10.2.0.2",
    nics: [],
    services: [],
    can_capture_memory: true,
    can_capture_disk: true,
    can_stop: true,
    can_reboot: true,
  };
  const session = {
    id: "ui-session",
    session_id: "ui-session",
    job_id: "ui-session",
    status: "running",
    started_at: "2026-09-06T10:00:00Z",
    vms: [vm],
    networks: [1, 2].map((n) => ({
      id: `net-${n}`,
      name: `Network ${n}`,
      state: "active",
      subnet_cidr: `10.${n}.0.0/24`,
    })),
  };
  const fixture = {
    session,
    artifacts: [],
    captures: [],
    capturesError: false,
    artifactsError: false,
    polls: 0,
  };
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url());
    const endpoint = url.pathname;
    if (endpoint.endsWith("/current-or-null") || endpoint.endsWith("/sessions/current")) {
      fixture.polls++;
      return route.fulfill({ json: fixture.session });
    }
    if (endpoint.endsWith("/artifacts"))
      return fixture.artifactsError
        ? route.abort("failed")
        : route.fulfill({ json: fixture.artifacts });
    if (endpoint.endsWith("/reports")) return route.fulfill({ json: [] });
    if (endpoint.endsWith("/ui-vm/captures")) {
      if (route.request().method() === "POST")
        return route.fulfill({
          json: { acquisition_id: `capture-${fixture.captures.length + 1}` },
        });
      return fixture.capturesError
        ? route.abort("failed")
        : route.fulfill({ json: fixture.captures });
    }
    if (endpoint.endsWith("/events")) {
      const id = endpoint.split("/").at(-2);
      fixture.captures.push({ id, status: "completed" });
      fixture.artifacts.push({
        id: `artifact-${id}`,
        acquisition_id: id,
        filename: `${id}.lime`,
        kind: "memory",
        sha256: `fixture-sha-${id}`,
        manifest_path: "fixture",
        sha256_sidecar_path: "fixture",
      });
      return route.fulfill({
        contentType: "text/event-stream",
        body: `event: completed\ndata: {"status":"completed","message":"published ${id}"}\n\n`,
      });
    }
    if (endpoint.includes("/catalog/")) return route.fulfill({ json: [] });
    if (endpoint.endsWith("/recent-failure")) return route.fulfill({ json: null });
    return route.fulfill({ json: {} });
  });
  await page.clock.install();
  await page.goto("/playground");
  await expect(page.locator("#pg-detail-panel")).toContainText("no captures taken yet");
  return fixture;
}

async function cleanupFixture(page) {
  const fixture = await setup(page);
  fixture.cleanupStatus = "destroy_requested";
  fixture.cleanupReads = 0;
  fixture.deletes = 0;
  fixture.cleanupError = null;
  await page.route("**/sessions/ui-session/cleanup-status", (route) => {
    fixture.cleanupReads++;
    if (fixture.cleanupError === "network") return route.abort("failed");
    if (fixture.cleanupError)
      return route.fulfill({ status: fixture.cleanupError, json: { detail: "fixture" } });
    return route.fulfill({
      json: {
        session_id: "ui-session",
        status: fixture.cleanupStatus,
        finished_at: ["cancelled", "destroy_failed"].includes(fixture.cleanupStatus)
          ? "2026-09-06T10:00:00Z"
          : null,
        error_code: fixture.cleanupStatus === "destroy_failed" ? "cleanup_incomplete" : null,
      },
    });
  });
  await page.route("**/sessions/ui-session", async (route) => {
    expect(route.request().method()).toBe("DELETE");
    fixture.deletes++;
    if (fixture.deleteGate) await fixture.deleteGate;
    return route.fulfill({
      status: 202,
      json: { session_id: "ui-session", job_id: "ui-session", resource_id: "ui-session" },
    });
  });
  fixture.end = async () => {
    await page.getByRole("button", { name: "End Session", exact: true }).click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Confirm", exact: true })
      .click();
  };
  fixture.pollCleanup = async () => {
    const before = fixture.cleanupReads;
    await page.clock.fastForward(5100);
    await expect.poll(() => fixture.cleanupReads).toBeGreaterThan(before);
  };
  return fixture;
}

test("End Session tracks acceptance, reload and explicit success without losing evidence access", async ({
  page,
}) => {
  const fixture = await cleanupFixture(page);
  let accept;
  fixture.deleteGate = new Promise((resolve) => {
    accept = resolve;
  });
  await fixture.end();
  const status = page.locator("#pg-top-metrics");
  await expect(status).toContainText("Requesting cleanup");
  await expect(status).toContainText("ui-session");
  await expect(page.getByRole("button", { name: "+ Target", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Capture RAM", exact: true })).toBeDisabled();
  const oldPolls = fixture.polls;
  await page.clock.fastForward(11000);
  expect(fixture.cleanupReads).toBe(0);
  expect(fixture.polls).toBe(oldPolls);
  accept();
  await expect(status).toContainText("Cleanup requested");
  await fixture.pollCleanup();
  await fixture.pollCleanup();
  await expect(status).not.toContainText("Session ended");
  await page.reload();
  await expect(status).toContainText("Cleanup requested");
  expect(fixture.deletes).toBe(1);
  expect(
    await page.evaluate(() =>
      globalThis.sessionStorage.getItem("astinuslab.playground.cleanup-session.v1"),
    ),
  ).toBe("ui-session");
  fixture.cleanupStatus = "destroying";
  await fixture.pollCleanup();
  await expect(status).toContainText("Cleaning up");
  fixture.cleanupStatus = "cancelled";
  fixture.session = null;
  await fixture.pollCleanup();
  await expect(status).toContainText("Session ended. Captured evidence is kept.");
  await expect(status.getByRole("link", { name: "Storage", exact: true })).toHaveAttribute(
    "href",
    "/storage",
  );
  const create = page.getByRole("button", { name: "+ Target", exact: true });
  await expect(create).toBeEnabled();
  await create.click();
  expect(
    await page.evaluate(() =>
      globalThis.sessionStorage.getItem("astinuslab.playground.cleanup-session.v1"),
    ),
  ).toBeNull();
  await expect(status).not.toContainText("Session ended");
  expect(fixture.deletes).toBe(1);
});

for (const problem of ["destroy_failed", 401, 404, "network"]) {
  test(`cleanup ${problem} preserves identity and never claims success`, async ({ page }) => {
    const fixture = await cleanupFixture(page);
    await fixture.end();
    const status = page.locator("#pg-top-metrics");
    await expect(status).toContainText("Cleanup requested");
    if (problem === "destroy_failed") fixture.cleanupStatus = problem;
    else fixture.cleanupError = problem;
    await fixture.pollCleanup();
    await expect(status).toContainText(
      problem === "destroy_failed"
        ? "Cleanup incomplete"
        : problem === 404
          ? "Cleanup record unavailable"
          : "Cleanup status unavailable",
    );
    await expect(status).toContainText("ui-session");
    await expect(status).not.toContainText("Session ended");
    await expect(page.getByRole("button", { name: "+ Target", exact: true })).toBeDisabled();
    expect(
      await page.evaluate(() =>
        globalThis.sessionStorage.getItem("astinuslab.playground.cleanup-session.v1"),
      ),
    ).toBe("ui-session");
    if (problem === "network") {
      fixture.cleanupError = null;
      await fixture.pollCleanup();
      await expect(status).toContainText("Cleanup requested");
    }
    if (problem === "destroy_failed") {
      await status.getByRole("button", { name: "Dismiss", exact: true }).click();
      expect(
        await page.evaluate(() =>
          globalThis.sessionStorage.getItem("astinuslab.playground.cleanup-session.v1"),
        ),
      ).toBeNull();
    }
    expect(fixture.deletes).toBe(1);
  });
}

test("two captures refresh artifact and acquisition membership without reload", async ({
  page,
}) => {
  await setup(page);
  for (const n of [1, 2]) {
    await page.getByRole("button", { name: "Capture RAM", exact: true }).click();
    await expect(page.locator("#stream-log")).toContainText(`published capture-${n}`);
    await expect(page.locator("#pg-detail-panel")).toContainText(`capture-${n}.lime`);
    await expect(page.getByRole("link", { name: "manifest", exact: true })).toHaveCount(n);
    await expect(page.getByRole("link", { name: "sha256", exact: true })).toHaveCount(n);
  }
  await expect(page.locator("#pg-detail-panel")).toContainText("fixture-sha-capture-1");
  await expect(page.locator("#pg-detail-panel")).toContainText("fixture-sha-capture-2");
});

for (const field of ["capturesError", "artifactsError"]) {
  test(`${field} allows retry after publication`, async ({ page }) => {
    const fixture = await setup(page);
    fixture[field] = true;
    await page.getByRole("button", { name: "Capture RAM", exact: true }).click();
    await expect(page.locator("#stream-log")).toContainText("published capture-1");
    await expect(page.getByRole("button", { name: "Retry evidence" })).toBeVisible();
    fixture[field] = false;
    await page.getByRole("button", { name: "Retry evidence" }).click();
    await expect(page.locator("#pg-detail-panel")).toContainText("capture-1.lime");
    await expect(page.getByRole("button", { name: "Retry evidence" })).toHaveCount(0);
  });
}

test("detail focus, open disclosure and unfinished NIC choice survive polls and capture", async ({
  page,
}) => {
  const fixture = await setup(page);
  const capture = page.getByRole("button", { name: "Capture RAM", exact: true });
  await capture.focus();
  const before = fixture.polls;
  fixture.session.vms[0].ip = "10.2.0.3";
  await page.clock.fastForward(5100);
  await expect.poll(() => fixture.polls).toBeGreaterThan(before);
  await expect(page.locator("#pg-detail-panel")).toContainText("10.2.0.3");
  await expect(capture).toBeFocused();
  await page.locator("#pg-detail-panel summary").click();
  const select = page.getByRole("combobox", { name: "Attach network", exact: true });
  await select.selectOption("net-2");
  await select.focus();
  for (let i = 0; i < 3; i++) {
    const count = fixture.polls;
    fixture.session.vms[0].cpu_pct = 20 + i;
    await page.clock.fastForward(5100);
    await expect.poll(() => fixture.polls).toBeGreaterThan(count);
    await expect(page.locator("#pg-detail-panel")).toContainText(`${20 + i}%`);
    await expect(select).toBeVisible();
    await expect(select).toBeFocused();
    await expect(select).toHaveValue("net-2");
  }
  await capture.click();
  await expect(page.locator("#pg-detail-panel")).toContainText("capture-1.lime");
  await expect(capture).toBeFocused();
  await expect(select).toBeVisible();
  await expect(select).toHaveValue("net-2");
  const attaching = page.waitForRequest("**/sessions/current/vms/ui-vm/nics");
  await page.getByRole("button", { name: "Attach network", exact: true }).click();
  expect((await attaching).postDataJSON()).toEqual({ network_id: "net-2" });
});

for (const dismiss of ["Escape", "Cancel", "Confirm"]) {
  test(`confirmation contains focus and restores invoker after ${dismiss}`, async ({ page }) => {
    await setup(page);
    const opener = page.getByRole("button", { name: "End Session", exact: true });
    // A failed DELETE leaves the invoker mounted even for Confirm.
    await page.route("**/api/v1/playground/sessions/ui-session", (route) =>
      route.fulfill({ status: 503, json: { detail: "fixture cleanup deferred" } }),
    );
    await opener.click();
    const dialog = page.getByRole("alertdialog");
    const cancel = dialog.getByRole("button", { name: "Cancel", exact: true });
    const confirm = dialog.getByRole("button", { name: "Confirm", exact: true });
    await expect(cancel).toBeFocused();
    await expect(dialog).toHaveAccessibleDescription(/session/i);
    await page.keyboard.press("Shift+Tab");
    await expect(confirm).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(cancel).toBeFocused();
    // Native focus() cannot reach an inert background control.
    await opener.evaluate((node) => node.focus());
    await expect(cancel).toBeFocused();
    if (dismiss === "Escape") await page.keyboard.press("Escape");
    else await dialog.getByRole("button", { name: dismiss, exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
    await expect(opener).toBeEnabled();
  });
}

for (const width of [320, 390]) {
  test(`footer controls fit at ${width}px with long labels`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page);
    // Exercise label expansion without depending on which translations are installed.
    await page
      .locator(".pg-foot-meta")
      .first()
      .evaluate((node) => {
        node.textContent = "Session: ui-session-with-a-very-long-identifier-0123456789";
      });
    await page.locator("#btn-end-session").evaluate((node) => {
      node.textContent = "Finalizar la sesión de laboratorio";
    });
    await page.locator(".pg-footer").scrollIntoViewIfNeeded();
    const footer = await page.locator(".pg-footer").boundingBox();
    for (const button of await page.locator(".pg-footer button:visible").all()) {
      await button.focus();
      const rect = await button.boundingBox();
      expect(rect.x).toBeGreaterThanOrEqual(footer.x);
      expect(rect.x + rect.width).toBeLessThanOrEqual(footer.x + footer.width + 1);
      expect(rect.x + rect.width).toBeLessThanOrEqual(width);
      await expect(button).toBeInViewport();
    }
    await page.screenshot({ path: testInfo.outputPath(`footer-${width}.png`), fullPage: true });
  });
}

test("confirmation restores a VM invoker replaced by polling", async ({ page }) => {
  const fixture = await setup(page);
  const opener = page
    .locator("#pg-detail-panel")
    .getByRole("button", { name: "Destroy VM", exact: true });
  await opener.click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  const before = fixture.polls;
  fixture.session.vms[0].ip = "10.2.0.3";
  await page.clock.fastForward(5100);
  await expect.poll(() => fixture.polls).toBeGreaterThan(before);
  await expect(page.locator("#pg-detail-panel")).toContainText("10.2.0.3");
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
});

test("nested network dialog accepts input and restores the attacker dialog stack", async ({
  page,
}) => {
  const fixture = await setup(page);
  await page.route("**/networks/suggest-cidr?*", (route) =>
    route.fulfill({ json: { subnet_cidr: "10.42.8.0/24" } }),
  );
  const attacker = page.getByRole("button", { name: "+ Attacker", exact: true });
  await attacker.click();
  const parent = page.getByRole("dialog", { name: "Create attacker", exact: true });
  const addNetwork = parent.getByRole("button", { name: "+ New", exact: true });
  const parentLabel = parent.getByLabel("attacker label");
  await parentLabel.fill("pending-attacker");

  for (const finish of ["cancel", "create"]) {
    await addNetwork.click();
    const child = page.locator("#create-network-modal");
    const name = child.getByRole("textbox", { name: "Name", exact: true });
    await expect(name).toBeFocused();
    await name.fill("nested-network");
    await child.getByRole("textbox", { name: "Subnet CIDR", exact: true }).fill("10.42.8.0/24");
    // The visible parent and page remain inaccessible while the child is active.
    await parentLabel.evaluate((node) => node.focus());
    await expect(child.getByRole("textbox", { name: "Subnet CIDR", exact: true })).toBeFocused();
    await page.locator("#create-network-submit").focus();
    await page.keyboard.press("Tab");
    await expect(page.locator("#create-network-close")).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(page.locator("#create-network-submit")).toBeFocused();
    if (finish === "cancel") {
      fixture.session.networks.push({
        id: "net-3",
        name: "Polled network",
        state: "active",
        subnet_cidr: "10.3.0.0/24",
      });
      await page.clock.fastForward(5100);
      await expect(page.locator("#create-vm-modal")).toContainText("Polled network");
      await expect(page.locator("#create-network-submit")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(addNetwork).toBeFocused();
    } else {
      await page.locator("#create-network-submit").click();
      await expect(parent).toContainText("nested-network");
    }
    await expect(child).toBeHidden();
    await expect(parent).toBeVisible();
    await expect(parentLabel).toHaveValue("pending-attacker");
    await parentLabel.focus();
    await expect(parentLabel).toBeFocused();
    // Background focus stays blocked until the outer dialog also closes.
    await attacker.evaluate((node) => node.focus());
    await expect(parentLabel).toBeFocused();
  }
  await page.keyboard.press("Escape");
  await expect(parent).toBeHidden();
  await expect(attacker).toBeFocused();
  // A new top-level opening must not inherit either nested inert snapshot.
  await attacker.click();
  await parentLabel.focus();
  await expect(parentLabel).toBeFocused();
  await parentLabel.fill("next-attacker");
  await expect(parentLabel).toHaveValue("next-attacker");
});

for (const outcome of ["success", "network error"]) {
  test(`abandoned capture-list ${outcome} can reload after selecting a network`, async ({
    page,
  }) => {
    const fixture = await setup(page);
    let started;
    const pending = new Promise((resolve) => {
      started = resolve;
    });
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    let completed;
    const released = new Promise((resolve) => {
      completed = resolve;
    });
    let loads = 0;
    await page.route("**/vms/ui-vm/captures", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      loads += 1;
      if (loads === 1) {
        started();
        await gate;
        if (outcome === "network error") await route.abort("failed");
        else await route.fulfill({ json: fixture.captures });
        completed();
        return;
      }
      return route.fulfill({ json: fixture.captures });
    });
    await page.getByRole("button", { name: "Capture RAM", exact: true }).click();
    await pending;
    await page.locator('.pg-v2-map-node[data-node-id="net-1"]').click();
    await expect(page.locator("#pg-detail-panel")).toContainText("Network Details");
    release();
    await released;
    // Allow the fetch success/catch/finally handlers to finish while the network is selected.
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          globalThis.requestAnimationFrame(() => globalThis.requestAnimationFrame(resolve)),
        ),
    );
    await page.locator('.pg-v2-map-node[data-node-id="ui-vm"]').click();
    await expect(page.locator("#pg-detail-panel")).toContainText("capture-1.lime");
    await expect(page.getByRole("link", { name: "manifest", exact: true })).toBeVisible();
    await expect.poll(() => loads).toBe(2);
  });
}

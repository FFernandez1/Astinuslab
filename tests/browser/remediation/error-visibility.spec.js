// Every failure the Playground catches must reach the operator.
//
// These cover paths where the code wrote an error into `state.errors` that no
// renderer read, so the failure was invisible: a refused destroy looked like a
// dead button, and an unreachable backend still rendered the lab as live.
const { test, expect } = require("@playwright/test");
const { URL } = require("node:url");

const VM = {
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

function sessionFixture() {
  return {
    id: "ui-session",
    session_id: "ui-session",
    job_id: "ui-session",
    status: "running",
    started_at: "2026-09-06T10:00:00Z",
    vms: [VM],
    networks: [{ id: "net-1", name: "Network 1", state: "active", subnet_cidr: "10.1.0.0/24" }],
  };
}

// One router for every spec here. `overrides` returns a response for the paths a
// given test wants to break, and falls through to healthy fixtures otherwise.
async function mount(page, overrides = () => null) {
  const fixture = { session: sessionFixture(), failSession: false };
  await page.route("**/api/v1/**", (route) => {
    const endpoint = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const override = overrides({ endpoint, method, route, fixture });
    if (override) return override;
    if (endpoint.endsWith("/current-or-null") || endpoint.endsWith("/sessions/current")) {
      if (fixture.failSession)
        return route.fulfill({ status: 500, json: { detail: "session backend unreachable" } });
      return route.fulfill({ json: fixture.session });
    }
    if (endpoint.endsWith("/artifacts") || endpoint.endsWith("/reports"))
      return route.fulfill({ json: [] });
    if (endpoint.endsWith("/captures")) return route.fulfill({ json: [] });
    if (endpoint.includes("/catalog/")) return route.fulfill({ json: [] });
    if (endpoint.endsWith("/recent-failure")) return route.fulfill({ json: null });
    return route.fulfill({ json: {} });
  });
  return fixture;
}

test("a refused VM destroy tells the operator why", async ({ page }) => {
  await mount(page, ({ endpoint, method, route }) =>
    method === "DELETE" && endpoint.endsWith("/vms/ui-vm")
      ? route.fulfill({
          status: 409,
          json: { detail: "VM destroy unavailable in state destroying", code: "conflict" },
        })
      : null,
  );
  await page.goto("/playground");
  const panel = page.locator("#pg-detail-panel");
  await expect(panel).toContainText("UI fixture VM");

  await panel.getByRole("button", { name: "Destroy VM", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Confirm", exact: true }).click();

  // The VM is still there, and the reason it is still there is on screen.
  await expect(panel).toContainText("destroy unavailable in state destroying");
  await expect(page.locator('.pg-v2-map-node[data-node-id="ui-vm"]')).toBeVisible();
});

test("a failing session poll stops the page claiming to be live", async ({ page }) => {
  const fixture = await mount(page);
  await page.clock.install();
  await page.goto("/playground");
  await expect(page.locator("#pg-detail-panel")).toContainText("UI fixture VM");
  await expect(page.locator("#playground-root")).not.toHaveAttribute("data-stale", "true");

  fixture.failSession = true;
  await page.clock.fastForward(5100);

  // The lab is deliberately still rendered -- losing it would look like data
  // loss -- but it must no longer present itself as live, and the reason must
  // be readable.
  await expect(page.locator("#playground-root")).toHaveAttribute("data-stale", "true");
  await expect(page.locator("#pg-alerts")).toContainText("session backend unreachable");
  await expect(page.locator("#pg-detail-panel")).toContainText("UI fixture VM");

  // Recovery clears both without a reload.
  fixture.failSession = false;
  await page.clock.fastForward(5100);
  await expect(page.locator("#playground-root")).not.toHaveAttribute("data-stale", "true");
  await expect(page.locator("#pg-alerts")).toBeHidden();
});

test("a failed catalog load is explained instead of showing an empty picker", async ({ page }) => {
  await mount(page, ({ endpoint, route }) =>
    endpoint.includes("/catalog/images")
      ? route.fulfill({ status: 503, json: { detail: "catalog unavailable" } })
      : null,
  );
  await page.goto("/playground");
  await page.getByRole("button", { name: "+ Target", exact: true }).click();
  const picker = page.locator("#create-vm-modal");
  await expect(picker).toContainText("Could not load Target images.");
  await expect(picker.getByRole("button", { name: "Retry loading images" })).toBeVisible();
  await expect(page.locator("#create-vm-next")).toBeDisabled();
});

test("a failed recipe export is reported", async ({ page }) => {
  await mount(page, ({ endpoint, route }) =>
    endpoint.endsWith("/recipe")
      ? route.fulfill({ status: 500, json: { detail: "recipe export failed" } })
      : null,
  );
  await page.goto("/playground");
  await page.locator("#btn-export-recipe").click();
  await expect(page.locator("#pg-alerts")).toContainText("recipe export failed");
});

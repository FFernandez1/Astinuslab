/* global window, document, MutationObserver */
const { test, expect } = require("@playwright/test");

// The store gates re-renders on a shallow-equality check of each subscriber's
// slice. normalizeSession rebuilt every VM object on every poll, and mergeMetrics
// stamped a sample carrying sampled_at and byte counters onto each running VM,
// so the gate never closed: an unchanged lab repainted the map and the details
// panel every five seconds, taking focus, scroll and selection with it.
//
// These drive real polls and watch the DOM: a steady lab must not touch it, and
// a lab that genuinely moves still must.
const VM = (overrides = {}) => ({
  id: "vm-a",
  label: "web-01",
  role: "target",
  state: "running",
  image_id: "ubuntu-22.04",
  nics: [{ id: "nic-a", network_id: "net-a", ip: "10.42.0.5", state: "active" }],
  services: [],
  ...overrides,
});

const session = (vm) => ({
  id: "pg-1",
  session_id: "pg-1",
  job_id: "job-1",
  status: "running",
  state: "running",
  vms: [vm],
  networks: [{ id: "net-a", name: "lab", state: "active", isolated: true }],
  storage: { global_label: "test storage", global_used_bytes: 0, global_total_bytes: 1e9 },
});

// A steady VM still produces a brand new sample every tick: the timestamp and
// the byte counters always move. Only cpu_pct and mem_pct are rendered.
const metrics = (sample) => ({
  "vm-a": {
    vm_id: "vm-a",
    state: "running",
    cpu_pct: 0,
    mem_pct: 41.2,
    mem_used_mb: 412,
    mem_total_mb: 1000,
    rx_bytes: { vnet0: 1000 + sample * 812 },
    tx_bytes: { vnet0: 500 + sample * 93 },
    sampled_at: `2026-09-18T10:00:${String(sample * 5).padStart(2, "0")}Z`,
  },
});

async function mockLab(page, state) {
  await page.route("**/api/v1/playground/sessions/current-or-null", (route) =>
    route.fulfill({ json: state.session || session(VM({ state: state.vmState })) }),
  );
  await page.route("**/api/v1/playground/sessions/current/metrics", (route) =>
    route.fulfill({ json: metrics(state.sample++) }),
  );
  await page.route("**/api/v1/playground/sessions/recent-failure", (route) =>
    route.fulfill({ json: null }),
  );
  await page.route("**/api/v1/playground/sessions/current/topology-layout", (route) =>
    route.fulfill({ json: { nodes: [] } }),
  );
  await page.route("**/api/v1/playground/vms/*/captures", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/playground/catalog/**", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/artifacts**", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/reports**", (route) => route.fulfill({ json: [] }));
}

// Counts DOM mutations under the map and the details panel from now on.
async function watchRepaints(page) {
  await page.evaluate(() => {
    window.__repaints = 0;
    window.__observer = new MutationObserver((records) => {
      window.__repaints += records.length;
    });
    for (const id of ["map-world", "pg-detail-panel"]) {
      const node = document.getElementById(id);
      if (node) {
        window.__observer.observe(node, { childList: true, subtree: true, characterData: true });
      }
    }
  });
}

test("a steady lab survives two polls without repainting @smoke", async ({ page }) => {
  const state = { vmState: "running", sample: 0 };
  await mockLab(page, state);
  await page.goto("/playground");

  await expect(page.locator("#map-world")).toContainText("web-01");
  await expect(page.locator("#pg-detail-panel")).toContainText("web-01");
  await watchRepaints(page);

  // Two full poll intervals (5s each) plus settle.
  await page.waitForTimeout(11_500);

  expect(state.sample).toBeGreaterThan(1); // the polls really did run
  expect(await page.evaluate(() => window.__repaints)).toBe(0);
  await expect(page.locator("#map-world")).toContainText("web-01");
});

test("a lab that actually changes still repaints @smoke", async ({ page }) => {
  const state = { vmState: "running", sample: 0 };
  await mockLab(page, state);
  await page.goto("/playground");

  await expect(page.locator("#map-world")).toContainText("web-01");
  await watchRepaints(page);

  state.vmState = "stopped";
  await expect
    .poll(() => page.evaluate(() => window.__repaints), { timeout: 12_000 })
    .toBeGreaterThan(0);
});

for (const width of [1440, 768]) {
  test(`24 VMs and eight networks retain map focus through polls at ${width}px @smoke`, async ({
    page,
  }, testInfo) => {
    const networks = Array.from({ length: 8 }, (_, i) => ({
      id: `net-${i}`,
      name: `segment-${i}`,
      state: "active",
      isolated: true,
      subnet_cidr: `10.42.${i}.0/24`,
    }));
    const vms = Array.from({ length: 24 }, (_, i) =>
      VM({
        id: `vm-${i}`,
        label: `fixture-target-${i}`,
        nics: [
          {
            id: `nic-${i}`,
            network_id: `net-${i % 8}`,
            ip: `10.42.${i % 8}.${10 + i}`,
            state: "active",
          },
        ],
      }),
    );
    const state = { sample: 0, session: { ...session(vms[0]), networks, vms } };
    await page.setViewportSize({ width, height: 1000 });
    await mockLab(page, state);
    await page.clock.install();
    await page.goto("/playground");
    await expect(page.locator('#map-world [data-type="vm"]')).toHaveCount(24);
    await expect(page.locator('#map-world [data-type="network"]')).toHaveCount(8);
    await expect(page.locator("#pg-top-metrics")).toContainText("24 of 24 hosts running");
    const node = page.locator('#map-world [data-node-id="vm-0"]');
    await node.focus();
    await watchRepaints(page);
    for (let i = 0; i < 2; i++) {
      const samples = state.sample;
      await page.clock.runFor(5100);
      await expect.poll(() => state.sample).toBeGreaterThan(samples);
    }
    await expect(node).toBeFocused();
    expect(await page.evaluate(() => window.__repaints)).toBe(0);
    await expect(page.getByRole("button", { name: "+ Target", exact: true })).toBeInViewport();
    await page.screenshot({
      path: testInfo.outputPath("scaled-topology.png"),
      animations: "disabled",
    });
  });
}

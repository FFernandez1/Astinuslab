const { test, expect } = require("@playwright/test");
const { mockPlaygroundApi } = require("../helpers/mocks");

test("arrow key moves a map node and saves @smoke", async ({ page }) => {
  await mockPlaygroundApi(page);
  const session = {
    id: "pg-1",
    job_id: "pg-1",
    session_id: "pg-1",
    status: "running",
    started_at: new Date().toISOString(),
    networks: [],
    vms: [
      {
        id: "vm-1",
        job_id: "pg-1",
        session_id: "pg-1",
        label: "tgt",
        image_id: "ubuntu-22.04",
        state: "running",
        role: "target",
        domain_name: "d",
        created_at: new Date().toISOString(),
        nics: [],
        services: [],
      },
    ],
  };
  await page.route("**/api/v1/playground/sessions/current-or-null", (route) =>
    route.fulfill({ json: session }),
  );
  await page.route("**/api/v1/playground/sessions/current", (route) =>
    route.fulfill({ json: session }),
  );
  await page.route("**/api/v1/playground/sessions/current/topology-layout", (route) =>
    route.fulfill({ json: { nodes: [{ node_id: "vm-1", node_type: "vm", x: 40, y: 60 }] } }),
  );
  let patched = null;
  await page.route("**/api/v1/playground/sessions/current/topology-layout/**", (route) => {
    patched = route.request().postDataJSON();
    return route.fulfill({ json: patched });
  });

  await page.goto("/playground");
  const node = page.locator('.pg-v2-map-node[data-node-id="vm-1"]');
  await expect(node).toBeVisible();
  const before = parseFloat(await node.evaluate((n) => n.style.left));
  await node.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => parseFloat(await node.evaluate((n) => n.style.left)))
    .toBeGreaterThan(before);
  await expect.poll(() => patched && patched.x).toBeGreaterThan(before);
});

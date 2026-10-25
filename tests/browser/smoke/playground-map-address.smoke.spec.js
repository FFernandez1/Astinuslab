const { test, expect } = require("@playwright/test");
const { mockPlaygroundApi } = require("../helpers/mocks");

// A machine is keyed by segment name once it holds an address, and the segment
// names this app generates are long. The stat column has to grow for them: when
// it did not, the label ran straight over the address it was labelling.
const SEGMENT = "network-fuzzy-packet-42";

const baseVm = {
  job_id: "pg-1",
  session_id: "pg-1",
  image_id: "ubuntu-22.04",
  state: "running",
  domain_name: "d",
  created_at: new Date().toISOString(),
  services: [],
};

test("a provisioned node keeps its segment label clear of its address @smoke", async ({ page }) => {
  await mockPlaygroundApi(page);
  const session = {
    id: "pg-1",
    job_id: "pg-1",
    session_id: "pg-1",
    status: "running",
    started_at: new Date().toISOString(),
    networks: [{ id: "net-1", name: SEGMENT, subnet_cidr: "10.42.2.0/24", state: "running" }],
    vms: [
      {
        ...baseVm,
        id: "vm-1",
        label: "target-sneaky-packet-07",
        role: "target",
        ip: "10.42.2.14",
        nics: [{ id: "nic-1", network_id: "net-1", ip: "10.42.2.14", state: "active" }],
      },
      {
        ...baseVm,
        id: "dev-1",
        label: "network-wobbly-portal-19",
        role: "network",
        function_tags: ["firewall"],
        nics: [{ id: "nic-2", network_id: "net-1", ip: "10.42.2.1", ordinal: 0, state: "active" }],
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
    route.fulfill({ json: { nodes: [] } }),
  );

  await page.goto("/playground");
  const node = page.locator('.pg-v2-map-node[data-node-id="vm-1"]');
  await expect(node).toBeVisible();
  await expect(node.locator('.node-stat .k[data-seg="true"]')).toHaveText(SEGMENT);

  // Geometry, not text: the defect was one box painted over another. A cell
  // whose text is wider than the cell overflows it, and with overflow visible
  // that text lands on the cell beside it.
  const cells = await page.$$eval(".node-stat, .dev-port", (rows) => {
    const view = rows[0]?.ownerDocument?.defaultView;
    return rows.flatMap((row) => {
      const boxes = [...row.children].map((cell) => cell.getBoundingClientRect());
      return [...row.children].map((cell, i) => ({
        text: cell.textContent,
        spills:
          view?.getComputedStyle(cell).overflowX === "visible" &&
          cell.scrollWidth > cell.clientWidth + 1,
        gapBefore: i === 0 ? 1 : boxes[i].left - boxes[i - 1].right,
      }));
    });
  });
  expect(cells.length).toBeGreaterThan(4);
  for (const cell of cells) {
    expect(cell.spills, `"${cell.text}" overflows its column`).toBe(false);
    expect(cell.gapBefore, `"${cell.text}" sits on the cell before it`).toBeGreaterThan(0);
  }

  // The column grew to hold a generated segment name rather than clipping it.
  const key = node.locator('.node-stat .k[data-seg="true"]');
  expect(await key.evaluate((k) => k.scrollWidth <= k.clientWidth + 1)).toBe(true);

  // A label the column cannot hold is ellipsised, so the whole name has to stay
  // reachable somewhere on the node.
  await expect(node.locator('.node-stat .k[data-seg="true"]')).toHaveAttribute("title", SEGMENT);
});

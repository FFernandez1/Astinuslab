// Headless smoke for the playground page. Boots uvicorn at 127.0.0.1:8765,
// drives the page through key states, dumps screenshots to /tmp/pg-*.png.
// Fails (exit 1) on any console error or pageerror.
const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const PORT = 8765;
const URL = `http://127.0.0.1:${PORT}/playground`;
const SHOT_DIR = "/tmp";
const SHELL_BIN = path.join(
  process.env.HOME,
  ".cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell",
);
function uvicornBin() {
  const candidates = [
    process.env.UVICORN_BIN,
    path.join(process.cwd(), ".venv/bin/uvicorn"),
    path.resolve(process.cwd(), "..", "..", ".venv/bin/uvicorn"),
  ].filter(Boolean);
  return candidates.find((p) => fs.existsSync(p)) || "uvicorn";
}

const now = new Date().toISOString();
const fixtureSession = {
  job_id: "pg-mockup",
  status: "running",
  started_at: now,
  storage: {
    global_used_bytes: 19756849561,
    global_total_bytes: 257698037760,
    global_label: "18.4 / 240 GiB",
  },
  networks: [
    {
      id: "net-internal",
      job_id: "pg-mockup",
      name: "net-internal",
      subnet_cidr: "10.42.1.0/24",
      isolated: true,
      libvirt_net_name: "pg-internal",
      state: "active",
      attached_vm_count: 2,
      can_delete: false,
    },
    {
      id: "net-dmz",
      job_id: "pg-mockup",
      name: "net-dmz",
      subnet_cidr: "10.42.2.0/24",
      isolated: true,
      libvirt_net_name: "pg-dmz",
      state: "active",
      attached_vm_count: 1,
      can_delete: false,
    },
  ],
  vms: [
    {
      id: "vm-web01",
      job_id: "pg-mockup",
      label: "tgt-web-01",
      image_id: "ubuntu-24-04-server",
      state: "running",
      domain_name: "astinuslab-pg-web01",
      role: "target",
      created_at: now,
      workload_id: "wordpress-6.4",
      workload_display_name: "WordPress 6.4",
      workload_state: "ready",
      services: [
        {
          name: "WordPress",
          port: 80,
          url: "http://10.42.1.10/",
          credentials: { user: "admin", pass: "admin" },
          status: "ready",
        },
      ],
      nics: [
        {
          id: "nic-web",
          job_network_id: "net-internal",
          mac: "52:54:00:00:00:10",
          ip: "10.42.1.10",
        },
      ],
      can_capture_memory: true,
      can_capture_disk: true,
      can_destroy: true,
    },
    {
      id: "vm-db01",
      job_id: "pg-mockup",
      label: "tgt-db-01",
      image_id: "debian-12",
      state: "running",
      domain_name: "astinuslab-pg-db01",
      role: "target",
      created_at: now,
      workload_id: "mysql-8",
      workload_display_name: "MySQL 8",
      workload_state: "ready",
      services: [
        {
          name: "MySQL",
          port: 3306,
          url: "10.42.1.20:3306",
          credentials: { user: "root", pass: "debian" },
          status: "ready",
        },
      ],
      nics: [
        {
          id: "nic-db",
          job_network_id: "net-internal",
          mac: "52:54:00:00:00:20",
          ip: "10.42.1.20",
        },
      ],
      can_capture_memory: true,
      can_capture_disk: true,
      can_destroy: true,
    },
    {
      id: "vm-kali01",
      job_id: "pg-mockup",
      label: "atk-kali-01",
      image_id: "kali-rolling",
      state: "running",
      domain_name: "astinuslab-pg-kali01",
      role: "attacker",
      created_at: now,
      workload_id: null,
      services: [],
      nics: [
        { id: "nic-kali", job_network_id: "net-dmz", mac: "52:54:00:00:00:30", ip: "10.42.2.30" },
      ],
      can_capture_memory: true,
      can_capture_disk: true,
      can_destroy: true,
    },
  ],
};
const fixtureTopology = {
  nodes: [
    { node_id: "vm-web01", node_type: "vm", x: 40, y: 60 },
    { node_id: "net-internal", node_type: "network", x: 320, y: 66 },
    { node_id: "vm-db01", node_type: "vm", x: 560, y: 60 },
    { node_id: "vm-kali01", node_type: "vm", x: 40, y: 320 },
    { node_id: "net-dmz", node_type: "network", x: 320, y: 326 },
  ],
};
const targetImages = [
  { id: "ubuntu-24-04-server", label: "Ubuntu 24.04 Server", distro: "ubuntu", version: "24.04" },
  { id: "debian-12", label: "Debian 12", distro: "debian", version: "12" },
];
const attackerImages = [
  { id: "kali-rolling", label: "Kali Linux Rolling", distro: "kali", version: "rolling" },
  { id: "parrot-security", label: "Parrot Security", distro: "parrot", version: "6.1" },
];
const workloads = [
  { id: "", display_name: "No workload", compatibility_status: "compatible", service_labels: [] },
  {
    id: "wordpress-6.4",
    display_name: "WordPress 6.4",
    compatibility_status: "compatible",
    service_labels: ["wordpress", "mysql"],
    tags: ["cms"],
    cve_ids: [],
  },
  {
    id: "dvwa",
    display_name: "DVWA",
    compatibility_status: "compatible",
    service_labels: ["apache", "mysql", "php"],
    tags: ["training"],
    cve_ids: [],
  },
];

async function waitFor(predicate, timeoutMs = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await predicate()) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("timeout");
}

async function probe() {
  try {
    const r = await fetch(URL);
    return r.status === 200;
  } catch (_) {
    return false;
  }
}

async function mockPlaygroundApi(page) {
  await page.route("**/api/v1/playground/sessions/current", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fixtureSession),
    }),
  );
  await page.route("**/api/v1/playground/sessions/current-or-null", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fixtureSession),
    }),
  );
  await page.route("**/api/v1/playground/sessions/current/metrics", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) }),
  );
  await page.route("**/api/v1/playground/sessions/recent-failure", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "null" }),
  );
  await page.route("**/api/v1/playground/sessions/current/topology-layout", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fixtureTopology),
    }),
  );
  await page.route("**/api/v1/playground/sessions/current/topology-layout/**", (route) => {
    if (route.request().method() === "PATCH")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: route.request().postData() || "{}",
      });
    return route.continue();
  });
  await page.route("**/api/v1/playground/catalog/images?role=target", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(targetImages),
    }),
  );
  await page.route("**/api/v1/playground/catalog/images?role=attacker", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(attackerImages),
    }),
  );
  await page.route("**/api/v1/playground/catalog/images?role=network", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }),
  );
  await page.route("**/api/v1/playground/catalog/workloads**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(workloads),
    }),
  );
  await page.route("**/api/v1/playground/networks/suggest-cidr**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ subnet_cidr: "10.42.2.0/24" }),
    }),
  );
  await page.route("**/api/v1/playground/vms/*/captures", (route) =>
    route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ acquisition_id: "acq-mock" }),
    }),
  );
  await page.route("**/api/v1/playground/captures/acq-mock/events", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: 'data: {"message":"[00:01] mock capture line"}\n\n',
    }),
  );
}

async function main() {
  const uv = spawn(
    uvicornBin(),
    [
      "astinuslab.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(PORT),
      "--log-level",
      "warning",
    ],
    { stdio: ["ignore", "inherit", "inherit"] },
  );
  let exited = false;
  uv.on("exit", () => {
    exited = true;
  });

  try {
    await waitFor(probe, 30000);
    const launchOpts = fs.existsSync(SHELL_BIN) ? { executablePath: SHELL_BIN } : {};
    const browser = await chromium.launch(launchOpts);
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(`PAGEERROR: ${e.message}`));
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(`CONSOLE_ERR: ${m.text()}`);
    });
    await mockPlaygroundApi(page);

    await page.goto(URL, { waitUntil: "networkidle" });
    await page.screenshot({ path: `${SHOT_DIR}/pg-idle.png`, fullPage: true });
    await page.waitForSelector('.pg-v2-map-node[data-type="vm"]', { timeout: 5000 });
    await page.screenshot({ path: `${SHOT_DIR}/pg-populated-map.png`, fullPage: true });
    await page.click('.pg-v2-map-node[data-node-id="vm-web01"]');
    await page.screenshot({ path: `${SHOT_DIR}/pg-detail-populated.png`, fullPage: true });
    await page.click('.pg-v2-capture-actions button:has-text("Capture RAM")');
    await page.waitForSelector("#capture-section:not([hidden])", { timeout: 5000 });
    await page.screenshot({ path: `${SHOT_DIR}/pg-capture.png`, fullPage: true });

    const tgt = await page.$('[data-open-create="target"]');
    if (tgt) {
      await tgt.click();
      await page.waitForSelector("#create-vm-modal:not([hidden])", { timeout: 5000 });
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${SHOT_DIR}/pg-target-basics.png`, fullPage: true });
      const stepCount = (await page.$$(".pg-hud-tick")).length;
      for (let i = 1; i < stepCount; i++) {
        await page.click("#create-vm-next");
        await page.waitForTimeout(500);
        await page.screenshot({ path: `${SHOT_DIR}/pg-target-step${i}.png`, fullPage: true });
      }
      await page.click("#create-vm-close");
    }
    const atk = await page.$('[data-open-create="attacker"]');
    if (atk) {
      await atk.click();
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${SHOT_DIR}/pg-attacker.png`, fullPage: true });
      await page.click("#create-vm-close");
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.click('[data-open-create="target"]');
    await page.waitForSelector("#create-vm-modal:not([hidden])", { timeout: 5000 });
    await page.click("#create-vm-next");
    await page.click("#create-vm-next");
    await page.waitForTimeout(300);
    await page.click(".pg-hud-netadd");
    await page.waitForSelector("#create-network-modal:not([hidden])", { timeout: 5000 });
    await page.screenshot({ path: `${SHOT_DIR}/pg-network-modal.png`, fullPage: true });

    await browser.close();
    if (errors.length) {
      console.error("FAIL:\n" + errors.join("\n"));
      process.exitCode = 1;
    } else {
      console.log("OK — screenshots in /tmp/pg-*.png");
    }
  } finally {
    if (!exited) uv.kill("SIGTERM");
  }
}
main().catch((e) => {
  console.error(e);
  process.exit(1);
});

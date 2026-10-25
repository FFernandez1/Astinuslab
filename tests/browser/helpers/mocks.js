const emptySession = {
  id: null,
  job_id: null,
  state: "idle",
  networks: [],
  vms: [],
  storage: {
    global_label: "test storage",
    global_used_bytes: 0,
    global_total_bytes: 1000000000,
  },
};

const targetImages = [
  {
    id: "ubuntu-22.04",
    label: "Ubuntu 22.04",
    distro: "Ubuntu",
    version: "22.04",
  },
];

const attackerImages = [
  {
    id: "kali-rolling",
    label: "Kali Rolling",
    distro: "Kali",
    version: "rolling",
  },
];

const workloads = [
  {
    id: "dvwa",
    display_name: "DVWA",
    compatibility_status: "compatible",
    service_labels: ["HTTP"],
    tags: ["web"],
    cve_ids: [],
  },
];

async function mockPlaygroundApi(page) {
  await page.route("**/api/v1/playground/sessions/current/metrics", async (route) => {
    await route.fulfill({ json: {} });
  });
  await page.route("**/api/v1/playground/sessions/current-or-null", async (route) => {
    await route.fulfill({ json: emptySession });
  });
  await page.route("**/api/v1/playground/sessions/current", async (route) => {
    await route.fulfill({ json: emptySession });
  });
  await page.route("**/api/v1/playground/catalog/images?role=target", async (route) => {
    await route.fulfill({ json: targetImages });
  });
  await page.route("**/api/v1/playground/catalog/images?role=attacker", async (route) => {
    await route.fulfill({ json: attackerImages });
  });
  await page.route("**/api/v1/playground/catalog/images?role=network", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/v1/playground/catalog/workloads?image_id=**", async (route) => {
    await route.fulfill({ json: workloads });
  });
  await page.route("**/api/v1/playground/networks/suggest-cidr**", async (route) => {
    await route.fulfill({ json: { subnet_cidr: "10.42.2.0/24" } });
  });
}

const IGNORED_CONSOLE_PATTERNS = [];

function isIgnoredConsoleError(text) {
  return IGNORED_CONSOLE_PATTERNS.some((re) => re.test(text));
}

function collectBrowserErrors(page) {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !isIgnoredConsoleError(message.text())) {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    if (!isIgnoredConsoleError(error.message)) errors.push(error.message);
  });
  return errors;
}

module.exports = {
  mockPlaygroundApi,
  collectBrowserErrors,
  IGNORED_CONSOLE_PATTERNS,
  emptySession,
  targetImages,
  attackerImages,
  workloads,
};

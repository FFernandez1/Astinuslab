const { defineConfig, devices } = require("@playwright/test");
const os = require("node:os");
const path = require("node:path");

const catalogPath = (name) => path.join(__dirname, "config", "catalog", `${name}.example.yaml`);

const smokeImagesDir = path.join(__dirname, "tests", "browser", "fixtures", "images");
const isFullE2E = process.env.ASTINUSLAB_E2E_FULL === "1";
// Smoke/integration default off 8000 on purpose: 8000 is the port a developer's
// own instance listens on, and reuseExistingServer is false, so sharing it made
// `npm run test:smoke` fail on any machine already running the app.
const port = isFullE2E ? 8000 : Number(process.env.ASTINUSLAB_SMOKE_PORT || 8125);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("ASTINUSLAB_SMOKE_PORT must be an integer from 1 to 65535");
}
const baseURL = `http://127.0.0.1:${port}`;
const smokeDbPath = path.join(
  os.tmpdir(),
  "astinuslab-smoke-" + process.pid + "-" + Date.now() + ".db",
);
const smokeStorageDir = path.join(
  os.tmpdir(),
  "astinuslab-smoke-storage-" + process.pid + "-" + Date.now(),
);
const pythonBin = process.env.ASTINUSLAB_PYTHON || path.join(__dirname, ".venv", "bin", "python");
const shellQuote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";

const smokeCatalogEnv = {
  ASTINUSLAB_TARGETS_CATALOG_PATH: catalogPath("targets"),
  ASTINUSLAB_SCENARIOS_CATALOG_PATH: catalogPath("scenarios"),
  ASTINUSLAB_WORKLOADS_CATALOG_PATH: catalogPath("workloads"),
  ASTINUSLAB_ATTACKERS_CATALOG_PATH: catalogPath("attackers"),
  ASTINUSLAB_NETWORK_NODES_CATALOG_PATH: catalogPath("network_nodes"),
  ASTINUSLAB_IMAGES_DIR: smokeImagesDir,
};

module.exports = defineConfig({
  metadata: { smokeStorageDir },
  testDir: "./tests/browser",
  // Playwright's default testMatch also collects tests/browser/unit/**/*.test.mjs
  // and helpers/preconditions.test.js. Those are node:test files run by
  // `npm run test:js:unit`; loading them here executed them during collection
  // while reporting none of their results. Spec files only.
  testMatch: /.*\.spec\.[cm]?[jt]s$/,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  fullyParallel: true,
  // Without retries `trace: "on-first-retry"` never produces a trace, so a CI
  // failure arrives with no artifact to triage from.
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: {
    // Full E2E boots real VMs whose provisioning runs on a Celery worker, so it
    // uses a wrapper that starts the worker alongside uvicorn and the real
    // catalog/images/DB. Smoke/integration mock those POSTs: uvicorn only, with
    // the fixture catalog and an isolated DB.
    command: isFullE2E
      ? "ASTINUSLAB_PYTHON=" + shellQuote(pythonBin) + " bash tools/dev/e2e_full_server.sh"
      : shellQuote(pythonBin) +
        " -m astinuslab.dbcli init && " +
        shellQuote(pythonBin) +
        ` -m uvicorn astinuslab.main:app --host 127.0.0.1 --port ${port}`,
    env: {
      ...process.env,
      ...(isFullE2E ? {} : smokeCatalogEnv),
      ...(isFullE2E ? {} : { ASTINUSLAB_DB_PATH: smokeDbPath }),
      ...(isFullE2E ? {} : { ASTINUSLAB_STORAGE_DIR: smokeStorageDir }),
      ASTINUSLAB_API_PORT: String(port),
    },
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: isFullE2E ? 60_000 : 30_000,
  },
  projects: [
    {
      name: "smoke",
      grep: /@smoke/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "full",
      grep: /@full/,
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});

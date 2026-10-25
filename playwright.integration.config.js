const { defineConfig, devices } = require("@playwright/test");
const os = require("os");
const path = require("path");

// Self-isolating Tier-2 config. Always starts its OWN backend on a dedicated
// port, backed by a throwaway temp DB + storage with a fake libvirt/playground
// host, so `npm run test:integration` can never touch the developer's real
// state. The old shared-webServer approach (playwright.config.js) relied on a
// manual env step documented in the README; this removes that footgun.
const ISO_DIR = path.join(os.tmpdir(), `astinuslab-int-${process.pid}`);
const PORT = 8099;
const BASE_URL = `http://127.0.0.1:${PORT}`;

// Unsafe methods now require a CSRF token (plan Task 9). A browser gets the
// cookie from the server and echoes it back through csrf.js; the `request`
// fixture has no page to do that, so seed the matching cookie/header pair here
// rather than teaching each spec about it.
const CSRF_TOKEN = "integration-csrf-token";
const pythonBin = process.env.ASTINUSLAB_PYTHON || path.join(__dirname, ".venv", "bin", "python");
const shellQuote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";

// Publish the dir so global-setup.js and the webServer below agree on it.
process.env.ASTINUSLAB_INT_ISO_DIR = ISO_DIR;

// Fail fast: refuse to run if anything points us at the repo's real storage.
const repoStorage = path.resolve(__dirname, "storage");
for (const v of ["ASTINUSLAB_DB_PATH", "ASTINUSLAB_STORAGE_DIR"]) {
  const ambient = process.env[v];
  if (ambient && path.resolve(ambient).startsWith(repoStorage)) {
    throw new Error(`integration config refuses ${v}=${ambient} (points at real storage)`);
  }
}

module.exports = defineConfig({
  testDir: "./tests/browser",
  // Spec files only: the node:test unit files under tests/browser/unit are run
  // by `npm run test:js:unit`, not by Playwright. See playwright.config.js.
  testMatch: /.*\.spec\.[cm]?[jt]s$/,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  globalSetup: require.resolve("./tests/browser/integration/global-setup.js"),
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    ...devices["Desktop Chrome"],
    extraHTTPHeaders: { "X-CSRF-Token": CSRF_TOKEN },
    storageState: {
      cookies: [
        {
          name: "astinuslab_csrf",
          value: CSRF_TOKEN,
          domain: "127.0.0.1",
          path: "/",
          expires: -1,
          httpOnly: false,
          secure: false,
          sameSite: "Strict",
        },
      ],
      origins: [],
    },
  },
  webServer: {
    command:
      shellQuote(pythonBin) +
      " -m astinuslab.dbcli init && " +
      shellQuote(pythonBin) +
      ` -m uvicorn astinuslab.main:app --host 127.0.0.1 --port ${PORT}`,
    url: `${BASE_URL}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      ...process.env,
      ASTINUSLAB_DB_PATH: path.join(ISO_DIR, "astinuslab.db"),
      ASTINUSLAB_STORAGE_DIR: path.join(ISO_DIR, "storage"),
      ASTINUSLAB_PLAYGROUND_DISK_DIR: path.join(ISO_DIR, "disks"),
      ASTINUSLAB_PLAYGROUND_CAPTURE_DIR: path.join(ISO_DIR, "captures"),
      ASTINUSLAB_IMAGES_DIR: path.join(ISO_DIR, "images"),
      ASTINUSLAB_TARGETS_CATALOG_PATH: path.join(ISO_DIR, "catalog", "targets.yaml"),
      ASTINUSLAB_WORKLOADS_CATALOG_PATH: path.join(ISO_DIR, "catalog", "workloads.yaml"),
      ASTINUSLAB_ATTACKERS_CATALOG_PATH: path.join(ISO_DIR, "catalog", "attackers.yaml"),
      ASTINUSLAB_FAKE_LIBVIRT: "1",
      ASTINUSLAB_FAKE_PLAYGROUND: "1",
      ASTINUSLAB_CELERY_RECONCILIATION_ENABLED: "false",
    },
  },
  projects: [{ name: "integration", grep: /@integration/ }],
});

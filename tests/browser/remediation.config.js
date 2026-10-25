// Dedicated fake-mode renderer tests: no workers, VM operations, or shared data.
const { defineConfig } = require("@playwright/test");
const { mkdtempSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
// One throwaway directory per run, under the OS temp dir. Two things were wrong
// with the previous `mkdtempSync(root/storage/ui-remediation-)`: it wrote into
// the repository's real storage tree (which playwright.integration.config.js
// explicitly refuses to do), and because the config is re-required in every
// worker process it left one orphaned directory per load, never cleaned up.
// Publishing the path through the environment makes workers reuse the first
// one, exactly as the integration config does.
const data =
  process.env.ASTINUSLAB_REMEDIATION_ISO_DIR ||
  mkdtempSync(path.join(os.tmpdir(), "astinuslab-remediation-"));
process.env.ASTINUSLAB_REMEDIATION_ISO_DIR = data;
const port = process.env.ASTINUSLAB_UI_TEST_PORT || "8126";
const python = process.env.ASTINUSLAB_PYTHON || path.join(root, ".venv/bin/python");
const quote = (value) => "'" + value.replace(/'/g, "'\\''") + "'";
module.exports = defineConfig({
  testDir: "./remediation",
  // Spec files only, matching the other configs. See playwright.config.js.
  testMatch: /.*\.spec\.[cm]?[jt]s$/,
  // Traces and screenshots stay outside the throwaway data dir so the global
  // teardown can delete server state without destroying failure artifacts.
  outputDir: path.join(root, "test-results", "remediation"),
  globalTeardown: require.resolve("./remediation/global-teardown.js"),
  workers: 1,
  timeout: 20000,
  expect: { timeout: 3000 },
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  projects: [{ name: "remediation" }],
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
  webServer: {
    command: `${quote(python)} -m astinuslab.dbcli init && ${quote(python)} -m uvicorn astinuslab.main:app --host 127.0.0.1 --port ${port}`,
    cwd: root,
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    env: {
      ...process.env,
      PYTHONPATH: `${root}/src:${root}`,
      ASTINUSLAB_DATA_DIR: data,
      ASTINUSLAB_DB_PATH: path.join(data, "ui.db"),
      ASTINUSLAB_STORAGE_DIR: path.join(data, "storage"),
      ASTINUSLAB_PLAYGROUND_DISK_DIR: path.join(data, "disks"),
      ASTINUSLAB_PLAYGROUND_CAPTURE_DIR: path.join(data, "captures"),
      ASTINUSLAB_API_PORT: port,
      ASTINUSLAB_FAKE_MODE: "1",
      ASTINUSLAB_FAKE_LIBVIRT: "1",
      ASTINUSLAB_FAKE_PLAYGROUND: "1",
      ASTINUSLAB_VOL3_ENABLED: "false",
      ASTINUSLAB_PACKAGES_ENABLED: "true",
      ASTINUSLAB_CELERY_RECONCILIATION_ENABLED: "false",
      ASTINUSLAB_IMAGES_DIR: path.join(root, "tests/browser/fixtures/images"),
      ...Object.fromEntries(
        ["targets", "scenarios", "workloads", "attackers", "network_nodes"].map((name) => [
          `ASTINUSLAB_${name.toUpperCase()}_CATALOG_PATH`,
          path.join(root, `config/catalog/${name}.example.yaml`),
        ]),
      ),
    },
  },
});

const { defineConfig, devices } = require('@playwright/test');
const path = require('path');

// Same contract as the other tiers: the interpreter is supplied, not assumed.
// Hardcoding .venv/bin/python is why this tier could never run in CI, where
// there is no virtualenv.
const python = process.env.ASTINUSLAB_PYTHON || path.join(__dirname, '.venv', 'bin', 'python');
const quote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";

module.exports = defineConfig({
  testDir: './tests/browser/docs',
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  retries: process.env.CI ? 2 : 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:9010',
    trace: 'on-first-retry',
    ...devices['Desktop Chrome'],
  },
  webServer: {
    command: `${quote(python)} -m mkdocs serve -f docs/mkdocs.yml -a 127.0.0.1:9010`,
    url: 'http://127.0.0.1:9010',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});

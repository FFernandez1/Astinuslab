const { collectBrowserErrors } = require("./mocks");

// Tier-2 (integration) and Tier-3 (full) share the live backend started by
// playwright.config's webServer at `baseURL`. Re-export the browser-error
// collector so those specs use a single source of truth.
module.exports = { collectBrowserErrors };

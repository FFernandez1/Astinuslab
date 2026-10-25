const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Remove the throwaway server data directory published by remediation.config.js.
// Guarded: only a path inside the OS temp dir carrying this tier's prefix is
// ever removed, so a mis-set environment variable cannot delete real state.
// Traces live in test-results/remediation and are deliberately left behind.
module.exports = async () => {
  const dir = process.env.ASTINUSLAB_REMEDIATION_ISO_DIR;
  if (!dir) return;
  const resolved = path.resolve(dir);
  const prefix = path.join(path.resolve(os.tmpdir()), "astinuslab-remediation-");
  if (!resolved.startsWith(prefix)) return;
  fs.rmSync(resolved, { recursive: true, force: true });
};

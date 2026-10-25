const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const LIBVIRT_URI = process.env.LIBVIRT_URI || "qemu:///system";
const REPOSITORY_STORAGE_DIR = path.resolve(__dirname, "../../../storage");

function fullTierEnabled() {
  return process.env.ASTINUSLAB_E2E_FULL === "1";
}

function libvirtReachable() {
  try {
    execFileSync("virsh", ["-c", LIBVIRT_URI, "version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function activeKeyPath() {
  const storageDir = process.env.ASTINUSLAB_STORAGE_DIR || REPOSITORY_STORAGE_DIR;
  const activeKeyFile = path.resolve(storageDir, "ssh_keys/.active");
  if (!fs.existsSync(activeKeyFile)) return null;
  const name = fs.readFileSync(activeKeyFile, "utf8").trim();
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return null;
  const priv = path.resolve(path.dirname(activeKeyFile), name);
  return fs.existsSync(priv) ? priv : null;
}

// Default-off runs skip. Explicit full-tier requests fail on missing prerequisites.
function skipReason() {
  if (!fullTierEnabled()) return "ASTINUSLAB_E2E_FULL!=1";
  if (!libvirtReachable()) throw new Error("Full tier requested but libvirt not reachable");
  if (!activeKeyPath()) {
    throw new Error(
      "Full tier requested but no active SSH key (generate one on Security at /ssh-keys)",
    );
  }
  return null;
}

module.exports = { fullTierEnabled, libvirtReachable, activeKeyPath, skipReason, LIBVIRT_URI };

const { execFileSync } = require("node:child_process");
const { LIBVIRT_URI } = require("./preconditions");

// Resolve a libvirt domain's first IPv4 address. A freshly-booted VM may not
// have a DHCP lease yet, so try the default (lease) source then the ARP table.
// Returns null (never throws) when no address is known yet.
function domainIp(domainName) {
  for (const source of ["lease", "arp"]) {
    try {
      const out = execFileSync(
        "virsh",
        ["-c", LIBVIRT_URI, "domifaddr", domainName, "--source", source],
        { encoding: "utf8" },
      );
      // Lines look like: vnet0  52:54:..  ipv4  10.42.1.23/24
      const m = out.match(/ipv4\s+(\d+\.\d+\.\d+\.\d+)\//);
      if (m) return m[1];
    } catch {
      // domain not found yet / source unavailable — try next source.
    }
  }
  return null;
}

// Run a command over SSH from the host into a VM using the active keystore key.
function sshExec(keyPath, user, ip, command) {
  return execFileSync(
    "ssh",
    [
      "-i",
      keyPath,
      "-o",
      "StrictHostKeyChecking=no",
      "-o",
      "UserKnownHostsFile=/dev/null",
      "-o",
      "ConnectTimeout=10",
      `${user}@${ip}`,
      command,
    ],
    { encoding: "utf8" },
  );
}

module.exports = { domainIp, sshExec };

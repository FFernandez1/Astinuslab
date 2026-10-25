// Tier 3 — FULL E2E (host->VM SSH reachability against RUNNING lab VMs).
//
// Complements full-vm-ssh.full.spec.js (which boots a fresh VM): this test
// proves the host can SSH into whatever target VMs are already running in the
// current Playground session, using the active keystore key. It mutates
// nothing and tears nothing down.
//
// Opt-in + local only. Skips unless ASTINUSLAB_E2E_FULL=1. An explicit request
// fails if libvirt is unreachable or the active SSH key is absent. This optional
// existing-target probe still skips when there are no running target VMs to reach.
//
//   Run: npm run test:e2e:full
//   Optional: ASTINUSLAB_E2E_SSH_USER (guest login; default: ubuntu)

const { test, expect } = require("@playwright/test");
const { skipReason, activeKeyPath } = require("../helpers/preconditions");
const { domainIp, sshExec } = require("../helpers/ssh");

const reason = skipReason();
const SSH_USER = process.env.ASTINUSLAB_E2E_SSH_USER || "ubuntu";

async function runningTargets(request) {
  const res = await request.get("/api/v1/playground/sessions/current");
  if (!res.ok()) return [];
  return ((await res.json()).vms || []).filter((v) => v.state === "running" && v.role === "target");
}

function vmIp(vm) {
  const nicIp = (vm.nics || []).map((n) => n.ip).find(Boolean);
  return nicIp || domainIp(vm.domain_name);
}

test.describe("host->VM SSH into running lab VMs @full", () => {
  test.skip(!!reason, `full tier disabled: ${reason}`);
  test.setTimeout(2 * 60 * 1000);

  test("SSH into every running target VM from the host", async ({ request }) => {
    const targets = await runningTargets(request);
    test.skip(targets.length === 0, "no running target VMs to reach");

    const key = activeKeyPath();
    for (const vm of targets) {
      const ip = vmIp(vm);
      expect(ip, `no IPv4 for ${vm.domain_name}`).toBeTruthy();
      const out = sshExec(key, SSH_USER, ip, "echo astinuslab-ok && uname -a");
      expect(out, `ssh into ${vm.label} (${ip})`).toContain("astinuslab-ok");
    }
  });
});

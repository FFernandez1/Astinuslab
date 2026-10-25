// Tier 3 — FULL E2E: boots a REAL libvirt VM through the UI and proves
// host->VM SSH reachability using the active keystore key.
//
// Opt-in + local only. Skips unless ASTINUSLAB_E2E_FULL=1. An explicit request
// fails if libvirt is unreachable or the active SSH key is absent.
// Tears down ONLY what it added --
// computed as a diff against the session as it stood before the tier ran, so a
// timed-out poll cannot leave a real VM behind, and a pre-existing lab is never
// touched. See helpers/vm-teardown.js.
//
//   Pre: generate/activate an SSH key on Security (/ssh-keys); libvirt + KVM up.
//   Run: npm run test:e2e:full
//   Optional: ASTINUSLAB_E2E_IMAGE   (catalog image id; must have its backing
//             file present on the host; default: ubuntu-24.04)
//             ASTINUSLAB_E2E_SSH_USER (guest login; default: ubuntu)

const { test, expect } = require("@playwright/test");
const { skipReason, activeKeyPath } = require("../helpers/preconditions");
const { domainIp, sshExec } = require("../helpers/ssh");
const { openTargetWizard, advanceToCreate } = require("../helpers/target-wizard");
const { vmsToTearDown } = require("../helpers/vm-teardown");

const reason = skipReason();
const SSH_USER = process.env.ASTINUSLAB_E2E_SSH_USER || "ubuntu";
// Catalog image id to boot. Use a *prepared* image: its backing file must be
// present AND it must apply the NoCloud seed (cloud-init) so the active SSH key
// is authorized. ubuntu-24.04 (noble) is prepared on this host; stock images
// like ubuntu-20-04-legacy/debian-12 boot but don't consume the seed (no key),
// and ubuntu-22-04-default's backing file is missing. Override per environment.
const IMAGE_ID = process.env.ASTINUSLAB_E2E_IMAGE || "ubuntu-24.04";

function isTransientSessionFetchError(error) {
  const message = String(error?.message ?? error);
  return (
    message.includes("socket hang up") ||
    message.includes("ECONNRESET") ||
    message.includes("fetch failed")
  );
}

async function currentVms(request, { tolerateTransientErrors = false } = {}) {
  try {
    const res = await request.get("/api/v1/playground/sessions/current");
    if (!res.ok()) return [];
    return (await res.json()).vms || [];
  } catch (e) {
    if (tolerateTransientErrors && isTransientSessionFetchError(e)) {
      return [];
    }
    throw e;
  }
}

// Resolve a VM's IPv4: prefer the backend's own nic resolution, fall back to
// the host's libvirt lease/arp tables.
async function resolveVmIp(request, vmId, domainName) {
  const vm = (await currentVms(request, { tolerateTransientErrors: true })).find(
    (v) => v.id === vmId,
  );
  const nicIp = (vm?.nics || []).map((n) => n.ip).find(Boolean);
  return nicIp || domainIp(domainName);
}

test.describe("full VM + host->VM SSH @full", () => {
  test.skip(!!reason, `full tier disabled: ${reason}`);
  test.setTimeout(15 * 60 * 1000); // real VM boot is slow

  // Both are read by afterAll, so they are set before anything can boot a VM
  // and are never reassigned out from under teardown.
  let preexisting = new Set();
  const createdVmIds = [];

  test.beforeAll(async ({ request }) => {
    // VMs that already exist: teardown must never touch these.
    preexisting = new Set((await currentVms(request)).map((v) => v.id));
  });

  test("boot a real target and SSH into it from the host", async ({ page, request }) => {
    // Build a minimal lab: one target. Creating it auto-provisions/boots it
    // (there is no separate launch step in this product). The target wizard is
    // multi-step (Basics/Workload/Resources/Review); the default image is
    // preselected, so advance to the last step then submit.
    await page.goto("/playground");
    // Pick a bootable OS image explicitly: the default first image may lack its
    // backing file on this host.
    const dialog = await openTargetWizard(page, { imageId: IMAGE_ID });
    const submit = await advanceToCreate(dialog);
    await submit.click();
    await expect(dialog).toBeHidden();

    // Identify the VM(s) this test created.
    await expect
      .poll(
        async () =>
          (await currentVms(request, { tolerateTransientErrors: true })).filter(
            (v) => !preexisting.has(v.id),
          ).length,
        {
          timeout: 60_000,
          intervals: [1000],
        },
      )
      .toBeGreaterThan(0);
    createdVmIds.push(
      ...(await currentVms(request)).filter((v) => !preexisting.has(v.id)).map((v) => v.id),
    );

    // Wait until every created VM reports running.
    await expect
      .poll(
        async () => {
          const mine = (await currentVms(request, { tolerateTransientErrors: true })).filter((v) =>
            createdVmIds.includes(v.id),
          );
          return mine.length > 0 && mine.every((v) => v.state === "running");
        },
        { timeout: 12 * 60 * 1000, intervals: [5000] },
      )
      .toBe(true);

    // Host -> VM SSH reachability for each created VM.
    const key = activeKeyPath();
    const mine = (await currentVms(request)).filter((v) => createdVmIds.includes(v.id));
    for (const vm of mine) {
      // A freshly-booted VM needs time to acquire a DHCP lease.
      let ip;
      await expect
        .poll(async () => (ip = await resolveVmIp(request, vm.id, vm.domain_name)), {
          timeout: 180_000,
          intervals: [5000],
        })
        .toBeTruthy();

      // ...and time for cloud-init to bring up sshd + inject the key.
      let out = "";
      await expect
        .poll(
          () => {
            try {
              out = sshExec(key, SSH_USER, ip, "echo astinuslab-ok && uname -a");
              return out.includes("astinuslab-ok");
            } catch {
              return false;
            }
          },
          { timeout: 180_000, intervals: [5000] },
        )
        .toBe(true);
      expect(out).toContain("astinuslab-ok");
    }
  });

  test.afterAll(async ({ request }) => {
    // Teardown is a diff, not a replay of what the body remembered. The
    // readiness poll above is the most likely thing to time out on a slow host,
    // and it throws before an id is recorded -- which used to leave a real VM
    // and its disk running. Anything in the session that was not there when the
    // tier started is this tier's, whether or not the test lived long enough to
    // write it down. See helpers/vm-teardown.js.
    const doomed = vmsToTearDown({
      preexistingIds: preexisting,
      currentVms: await currentVms(request, { tolerateTransientErrors: true }),
      recordedIds: createdVmIds,
    });
    for (const id of doomed) {
      await request.delete(`/api/v1/playground/vms/${id}`).catch(() => {});
    }
  });
});

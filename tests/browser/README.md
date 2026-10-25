# UI / E2E tests

Four Playwright tiers, run as named projects. Three are selected by tag
(`@smoke` / `@integration` / `@full`) inside the shared `tests/browser` tree;
the remediation tier has its own config and its own directory.

| Tier | Command | Backend | VMs | Where |
|------|---------|---------|-----|-------|
| Smoke | `npm run test:smoke` | mocked API | none | everywhere + CI |
| Integration | `npm run test:integration` | real backend + DB | none (read-only) | local + CI |
| Remediation | `npm run test:remediation` | real backend, fake mode | none (simulated) | local + CI |
| Full E2E | `npm run test:e2e:full` | real backend | **real libvirt VMs** | this host only |

`npm run test:docs` builds the docs site and checks its table of contents; it is
a separate config and is not part of the four tiers above.

## Smoke (`@smoke`)

Real frontend against an isolated backend, with route/API mocks. Walks the real-user path and asserts every
primary control on **Playground** and **Jobs** renders/opens/responds, plus a
light "page loads, zero browser errors" pass over every nav route. Fast; this is
exactly what CI runs.

`panel-geometry.smoke.spec.js` checks logs/settings control sizes, Volatility
capture-context reflow and keyboard traversal at 390px and 1280px, and title
alignment with the shell on four pages at 1280px and 1920px. It uses a unique
synthetic log per test and intercepts the analysis API; mutation requests are
blocked. Run it with `npx playwright test panel-geometry --project=smoke`.

## Integration (`@integration`)

Real backend, **no route mocks**, **no VM mutation**. Because this product boots
a real VM the moment one is created (no dry-run mode), Tier 2 exercises only the
real read/validation + UI-wiring paths and stops strictly before provisioning.

Self-isolating — just run it:

```bash
npm run test:integration
```

`playwright.integration.config.js` starts its own backend on port 8099 against a
throwaway temp DB + storage with a fake libvirt/playground host, and refuses to
start if pointed at the repo's real `storage/`. No manual env setup, and it can
never touch your real local state.

## Remediation (`tests/browser/remediation/`)

Real backend in full fake mode (`ASTINUSLAB_FAKE_MODE`, fake libvirt and
playground, Volatility disabled), route-mocked at the edges. This tier owns the
behaviours that only a browser can prove: a stale reply that must not cross a
newer selection, focus restoration across a 5-second poll, dialog focus
containment and invoker restoration, error-then-retry recovery on evidence and
ISF endpoints, and narrow-viewport footer layout.

```bash
npm run test:remediation
```

`tests/browser/remediation.config.js` starts its own backend on port 8126
(`ASTINUSLAB_UI_TEST_PORT` overrides) against a throwaway directory under the OS
temp dir, removed by the global teardown. Traces from failures are kept in
`test-results/remediation/`.

Specs use `page.clock` rather than real waits — prefer that pattern in new tests
in any tier.

## Full E2E (`@full`)

Two tests, both proving host→VM SSH with the active keystore key:

- `host-ssh-reachability.full.spec.js` — SSHes into every **already-running**
  target VM in the current session. Mutates nothing; skips if none are running.
- `full-vm-ssh.full.spec.js` — boots a **fresh** target VM through the
  Playground UI, waits for `running`, resolves its IP (backend `nics` →
  `virsh domifaddr`), SSHes in, and tears down **only** the VM it created.

The tier skips when `ASTINUSLAB_E2E_FULL` is not `1`. An explicit request (including
`npm run test:e2e:full`) fails at collection when either prerequisite is missing:

- libvirt reachable (`virsh -c qemu:///system version`)
- an active SSH key exists (generate one on Security at `/ssh-keys`)

For acceptance, the fresh-VM row must run and pass. The already-running-target
probe is optional and still skips when there are no targets; that skip provides
no existing-target evidence. Selecting the tier requires authorization for its
real host/VM operations. Unit checks of prerequisites use synthetic probes and
do not establish host readiness.

The fresh-boot test uses a **prepared** image: its backing file must exist AND
it must apply the NoCloud seed (cloud-init) so the active key is authorized.
Default `ubuntu-24.04` (noble) works. Stock `ubuntu-20-04-legacy` / `debian-12`
boot but don't consume the seed on this host (no key → no SSH); `ubuntu-22-04-
default`'s backing file is missing.

Optional: `ASTINUSLAB_E2E_IMAGE` (default `ubuntu-24.04`),
`ASTINUSLAB_E2E_SSH_USER` (default `ubuntu`),
`LIBVIRT_URI` (default `qemu:///system`).

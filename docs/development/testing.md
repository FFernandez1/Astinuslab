# Testing

Automated tests are configured in `pyproject.toml` and GitHub Actions.

After `./astinuslab.sh setup`, install the pinned frontend toolchain and the
browser used by Playwright before running JavaScript or browser tiers:

```bash
npm ci
npx playwright install chromium
```

## Backend Tests

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

Run a specific file:

```bash
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

Run the CI coverage gate:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Coverage threshold `84` is enforced by the command above and `.github/workflows/ci.yml`.

One percentage over 30,000 statements cannot notice a single module collapsing:
a few hundred well-tested lines elsewhere pay for it and the total barely moves.
The modules whose defects are *silent* — reconciliation, spawn and teardown,
resource custody, evidence capture and publication, retention, the capacity
slot — therefore carry their own floors, listed in
`tools/quality/check_coverage_floors.py` and enforced in CI against the JSON the
same coverage run emits:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python \
  --cov=src/astinuslab --cov-report=json:coverage.json --cov-fail-under=86 -q
.venv/bin/python tools/quality/check_coverage_floors.py coverage.json
```

Floors are integer minimums, not exact restatements: coverage moves by fractions
whenever an unrelated test touches a shared helper, and a gate that fails on a
tenth of a point teaches people to edit the number without reading it. A module
sitting three or more points clear is reported so the floor can be raised;
`--raise-stale-floors` turns that report into a failure for a deliberate sweep.

The measurement is **branch** coverage (`branch = true` in `pyproject.toml`), not
line coverage. This codebase is dense in guard clauses — traversal refusals,
fake-mode refusals, capacity deferrals, ACL ownership checks — and a guard whose
body is never taken still counts as a covered line. Counting arcs instead
reported 84.3% where line coverage reported 86.7%, so the threshold was reset to
the measured figure when the stricter measurement was switched on. Ratchet it
upward as the uncovered arcs are closed; do not lower it.

### Backend test folder layout

Tests under `tests/python/` are organised into domain folders:

| Folder | Contents |
| --- | --- |
| `tests/python/api/` | HTTP API contract, v1 extended, and OpenAPI contract tests |
| `tests/python/forensics/` | Volatility 3, symbol cache/providers, capture, acquisition, ISF, and Windows forensics tests |
| `tests/python/jobs/` | Job runtimes, state-machine transitions, state support, and repository tests |
| `tests/python/playground/` | Playground API, orchestrator, repositories, UI, and integration tests |
| `tests/python/lab_packages/` | Portable lab package authoring, export, import, signing, verification, content store, archive safety, coordinator allocation, and launch tests |
| `tests/python/host/` | Host helpers, sudoers, network-isolation, and libvirt storage helper tests |
| `tests/python/security/` | Cross-cutting privileged-boundary contracts asserted against the installer, host helpers, and route sources |
| `tests/python/ui/` | Server-rendered FastAPI/Jinja route tests (run by pytest, **not** a browser) |
| `tests/python/integration/` | Cross-service integration tests, including a real Celery worker subprocess with isolated SQLite broker, result, and application databases |
| `tests/python/e2e/` | Backend-owned pytest/Playwright coverage of playground operations and authenticated login, mutation, and logout; run explicitly in CI |
| `tests/python/host_real/` | Opt-in real-hypervisor tier (marked `host_real`) — skips in ordinary runs; when `ASTINUSLAB_REAL_HOST=1`, unavailable prerequisites fail the suite. Runs through the scheduled/self-hosted real-host workflow |
| `tests/python/core/`, `db/`, `storage/`, `images/`, `orchestration/`, `virtualization/`, `workloads/` | Tests owned by the corresponding application domain |
| `tests/python/tooling/` | Repository/deployment checks; explicitly selected separately from application discovery |
| `tests/python/` (root) | Shared `conftest.py`, package markers and support modules; no root-level test files |

Run a domain folder in isolation:

```bash
.venv/bin/python -m pytest tests/python/playground -q
.venv/bin/python -m pytest tests/python/forensics -q
.venv/bin/python -m pytest tests/python/jobs -q
.venv/bin/python -m pytest tests/python/lab_packages -q
.venv/bin/python -m pytest tests/python/ui -q
.venv/bin/python -m pytest tests/python/host -q
.venv/bin/python -m pytest tests/python/e2e -q
ASTINUSLAB_REAL_HOST=1 .venv/bin/python -m pytest tests/python/host_real -q  # real KVM/libvirt host only
```

**`tests/python/ui/` vs `tests/browser/`**

- `tests/python/ui/` — pytest tests for server-rendered FastAPI/Jinja routes (`test_ui.py`, `test_lab_ui.py`, `test_settings_routes.py`, `test_storage_admin.py`). No browser; run with `pytest`.
- `tests/browser/` — Playwright browser tests (smoke / integration / full VM tiers). Run with `npm run test:smoke`, `npm run test:integration`, or `ASTINUSLAB_E2E_FULL=1 npm run test:e2e:full`.

## Repository Unit Tests

Installer, host-helper, launcher, and documentation-theme unit tests live under `tests/python/tooling`:

```bash
.venv/bin/python -m pytest tests/python/tooling -q
```

## Test Tiers

Use the lowest safe tier that covers the change:

| Tier | Command | Scope | Safety |
| --- | --- | --- | --- |
| Tier 0 static | `.venv/bin/python -m ruff check .`, `.venv/bin/python -m mypy src tests/python ops/deploy`, `.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict`, `npm run lint:js` | Syntax, imports, typing, docs, JavaScript lint | No VM or host mutation |
| Tier 1 backend | `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q` | Backend unit, service, repository, API, and server-rendered UI tests | Uses fakes/fixtures; e2e files are skipped unless requested explicitly |
| Tier 1b repository tooling | `.venv/bin/python -m pytest tests/python/tooling -q` | Installer, launcher, host-helper, and docs-theme unit tests | Uses temporary files and fake scripts |
| Tier 2 backend e2e | `.venv/bin/python -m pytest tests/python/e2e -q` | Backend-owned Playwright browser coverage against a fake-libvirt backend | Runs in CI after frontend smoke tests |
| Tier 2 UI smoke | `npm run test:smoke` | Browser smoke tests with route/API mocks | No provisioning POSTs or libvirt mutation |
| Tier 2 UI integration | `npm run test:integration` | Real-backend read/validation checks | Runs in CI with its own fake host, temporary DB/storage, and dedicated server |
| Tier 3 full VM e2e | `ASTINUSLAB_E2E_FULL=1 npm run test:e2e:full` | Full browser + backend + libvirt VM workflows | Manual only; can mutate libvirt and host storage |

### Select checks for a change

Use the [shared verification matrix](verification-matrix.md) for change-specific
commands, prerequisites and evidence limits. It is maintained in English with
localized-site fallback so the command matrix has one source.

### Driving a job past admission

Creating a Job is safe to test anywhere. *Running* one is not: a job run
installs a base image through `sudo libvirt_storage_admin.sh` and defines a
domain, so on a host where that sudo rule is configured — the normal state of a
machine that runs this product — executing a job from a test provisions a real
VM with a real disk. Neither `ASTINUSLAB_FAKE_LIBVIRT` nor
`ASTINUSLAB_FAKE_PLAYGROUND` covers that path.

```bash
ASTINUSLAB_FAKE_ORCHESTRATION=1
```

replaces the bindings on `JobRunDeps` that reach the host, and nothing else: the
runners walk the real status graph and write the real events. A `manual_trigger`
job reaches `waiting_for_capture`, so stage progression, cancel and the capture
trigger are reachable. A capture job runs the same boot and then **fails** where
it would have produced evidence — faking a boot is faking an observation, faking
an acquisition is forging an artifact, and the flag does not do it.

Any command that would cross into the host raises rather than returning a canned
success, so a path the fake does not anticipate fails the test instead of
reaching the machine. The flag is off unless set, is reported by `/health` and
the fake-mode banner, and is deliberately separate from the other two so the
Jobs tier can keep testing admission against a backend that never executes.

Playwright browser tiers default to `.venv/bin/python` and can be pointed at another interpreter explicitly when a release host or CI image uses a different venv path:

```bash
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:smoke
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:integration
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:e2e:full
```

If the developer server already occupies port 8000, choose a free smoke-test port:

```bash
ASTINUSLAB_SMOKE_PORT=18087 npm run test:smoke
```

The smoke runner uses that port for the backend, browser URL, health check, and
CSRF origin validation. It always starts its own server. The full VM tier keeps
its existing port 8000; the integration tier uses its separate configuration.

The default backend suite includes a real worker-process test with eager mode
disabled. It uses the application's SQLite Celery transport and temporary state,
so it needs no external broker or hypervisor. Other unit and browser tests still
use task stubs or eager execution. Passing those safe tiers does not establish
real VM, Volatility, broker restart, or redelivery behavior.

Never run Tier 3 on a host with active lab state until you have checked running VMs, networks, and storage paths. Keep full VM e2e off default PR CI unless a dedicated libvirt runner exists.

## Release evidence (real libvirt)

CI fakes libvirt, so before tagging a release run the real-VM paths on a
libvirt host and record the result. This is the proportionate control for a
local/LAN lab: a green pytest suite is required, but it is not sufficient for
GO because CI does not exercise every production libvirt, guest, capture, and
artifact path.

1. Before collecting host evidence, start from the release commit: `git status --short` must be empty, and record the exact `git rev-parse HEAD` value in the release notes or evidence bundle.
2. Ensure a prepared image (its backing file present in `images/definitions/`)
   and an active SSH key generated on the Security page (`/ssh-keys`).
3. Run the Tier-3 full E2E: `npm run test:e2e:full` (see the Test Tiers table
   above).
4. Drive the required host-verified workflows below on the recorded release commit and prepare the seven-row JSON plus proof files described in the internal [release acceptance procedure](RELEASE_ACCEPTANCE.md).
5. Collect and independently validate the private bundle with `collect_release_evidence.sh` and `validate_release_evidence.py` as documented there.
6. Keep the private bundle under release-owner control. Only its generated, reviewed `public/` projection is a publication candidate.

| # | Workflow | How to drive | Evidence required |
| --- | --- | --- | --- |
| 1 | Jobs / `immediate`, memory | API or UI, any target | Completed job, memory artifact triad, independent sha256, workflow proof |
| 2 | Jobs / `post_script` or interactive, disk or `both` | API or UI, any target (disk is rejected in `immediate` mode) | Completed job, disk artifact triad, independent sha256, workflow proof |
| 3 | Jobs / `post_script` | Scenario `workload-sample-post-script` | Completed job, artifact triad, independent sha256, event log showing `running_scenario_script` to `waiting_for_capture` |
| 4 | Interactive Job capture | Trigger capture from the UI | Completed job, artifact triad, Playwright trace, screenshot |
| 5 | Playground workload install | Install DVWA from an already-baked local image without external egress | Completed workload identity plus reachability or equivalent backend proof; no artifact triad |
| 6 | Playground memory capture | UI capture button | Completed acquisition, memory artifact triad, Playwright trace, screenshot |
| 7 | Playground disk capture | UI capture button | Completed acquisition, disk artifact triad, resume proof, Playwright trace, screenshot |

GO requires evidence for all seven rows produced on the release commit on a
real KVM host. A missing row without a complete six-field waiver forces NO-GO;
any complete, unexpired waiver caps the verdict at GO-WITH-RISKS. No waiver is
planned for 1.0.0.

The collector validates raw artifacts in place but never copies them. Private
`evidence.json` is authoritative; copied manifests, sidecars, proofs, checksums,
and `HOST_WORKFLOW_CHECKLIST.md` are generated views or integrity material.
Never edit the checklist into PASS. The standalone validator must report PASS
for the exact clean candidate before release review.

## Pytest Markers

Core markers are registered in `pyproject.toml` so focused runs can stay explicit:

```bash
.venv/bin/python -m pytest --markers
.venv/bin/python -m pytest tests/python -m "not e2e" --collect-only -q
.venv/bin/python -m pytest tests/python -m e2e --collect-only -q
.venv/bin/python -m pytest tests/python/tooling -q
```

Backend tests are auto-marked by directory (see `tests/python/conftest.py`):
`api/` → `api`, `ui/` → `ui`, `host/` → `host`, `e2e/` → `e2e`, `integration/` → `integration`, everything else
→ `unit`. Select with `-m`:

```bash
pytest tests/python -m api              # API contract tests only
pytest tests/python -m "not host"       # skip subprocess/host-helper tests
pytest tests/python -m "unit or api"    # fast lanes
```

## Lint and Type Check

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
```

Ruff selects `E9`, `F`, `I`, `UP`, `B`, `S` and `C901`, with the scoped
exceptions in `pyproject.toml`. `C901` is a ratchet pinned to the most complex
function in the tree, so nothing new may be worse than what is already worst;
lower it when that function is dealt with.

Mypy runs on Python 3.12 with every strict flag the tree already satisfies:
`check_untyped_defs`, `no_implicit_optional`, `warn_redundant_casts`,
`strict_equality`, `extra_checks`, `disallow_subclassing_any` and
`warn_unused_ignores`. Each was measured at zero errors before being enabled,
so none is aspirational.

The rest of `--strict` is not affordable tree-wide yet. What it would cost is
counted per error code by `tests/python/tooling/test_mypy_strictness.py`, which
restates the numbers exactly and fails if any of them grows. A few modules run
ahead of the tree under per-module overrides; promoting one shows up there as a
drop. Run it with the rest of the tooling suite:

```bash
.venv/bin/python -m pytest tests/python/tooling -q
```

## Install, upgrade and rollback acceptance

`tests/shell/host/upgrade_matrix.sh` is the only harness that installs
AstinusLab system-wide for real. Fourteen rows cover a genuine prior-version
upgrade, induced migration/service/readiness failures, split-identity
migration and rollback, a custom storage root, both uninstall modes, and an
unsafe install-parent refusal.

For the first public release only, the explicit local `RELEASE_MODE=first` path
records historical row 0 as not applicable and retains rows 1–13. See the
[first-release command and provenance requirements](RELEASE_ACCEPTANCE.md#install-upgrade-and-rollback-acceptance).
The default mode and the GitHub workflow still require a genuine published prior
version; candidate-local fixtures do not prove historical upgrade compatibility.

Dispatch it as the **Upgrade acceptance (disposable host)** workflow. It is
manual and unscheduled on purpose: it runs as root, creates and removes service
identities, rewrites systemd units, ACLs and MAC policy, and uninstalls twice.
It refuses to start unless the target host carries
`/etc/astinuslab-disposable-host`, and it never runs on the `[self-hosted,
libvirt]` nightly runner, which is provisioned by hand.

Provisioning the target is in
[Upgrade acceptance host provisioning](../dev/upgrade-acceptance-host.md).

## Shell Scripts

When ShellCheck is installed:

```bash
shellcheck ops/host/*.sh
```

## Documentation

```bash
.venv/bin/python -m pip install "mkdocs-material>=9.5" "mkdocs-static-i18n>=1.2"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

## CI

`.github/workflows/ci.yml` runs on push and pull request. The backend job runs on Python 3.12 and 3.13; the frontend/Playwright job, including backend e2e pytest, runs on Python 3.12:

1. Install `libvirt-dev` and `pkg-config`.
2. Install `requirements-dev.lock` — generated from `uv.lock`, never hand-edited.
   See [Dependency locking](../architecture/dependency-locking.md).
3. Install the project with `python -m pip install -e . --no-deps`.
4. Run Ruff lint and format check.
5. Run Mypy.
6. Run backend pytest with coverage gate.
7. Run repository tooling tests from `tests/python/tooling`.
8. Run frontend lint, formatting, JavaScript unit tests, and Playwright smoke and integration tests.
9. Install Python Playwright Chromium.
10. Run backend e2e pytest from `tests/python/e2e`.
11. Upload HTML coverage and Playwright report artifacts.

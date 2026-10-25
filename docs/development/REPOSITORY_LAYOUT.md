# Repository Layout

AstinusLab keeps source code, catalogs, docs, tests, and local runtime state in stable locations. This map is a contributor guide only; it does not relocate runtime paths.

> The staged reorganization target is approved, but this page continues to describe paths that have actually landed. See the approved [Repository target layout](../architecture/repository-target-layout.md) and its migration matrix; do not use future paths until their stage merges.

## Source code

```text
src/astinuslab/*.py: 10

src
src/astinuslab
src/astinuslab/api
src/astinuslab/core
src/astinuslab/db
src/astinuslab/forensics
src/astinuslab/i18n_data
src/astinuslab/images
src/astinuslab/jobs
src/astinuslab/live_session
src/astinuslab/migrations
src/astinuslab/orchestration
src/astinuslab/playground
src/astinuslab/storage
src/astinuslab/ui
src/astinuslab/virtualization
src/astinuslab/web
src/astinuslab/workloads
```

- `src/astinuslab/`: installable FastAPI app, Celery tasks, SQLAlchemy models, orchestration helpers, package-owned Alembic migrations, and web resources under `web/`.
- `tests/`: Python application and tooling suites, shell contracts, and browser tests.
- `guest/`: guest-side scripts, templates, and helpers used inside lab VMs. Windows image-prep Autounattend examples live in `guest/windows/`.
- `ops/host/`: installed host-side libvirt, storage, ACL, and portable Windows build helpers.
- `ops/images/`: operator-facing image download and provenance metadata helpers.
- `ops/deploy/`: canonical install, privileged-root configuration, and uninstall implementations.
- `ops/systemd/`: source templates for installed API, runner, and analysis services.
- `tools/dev/`: contributor-only local development helpers; these never ship in an installed release.
- `tools/quality/`: repository quality-policy checks invoked directly by CI.
- `tools/release/`: source-controlled release evidence, validation, and review contracts; these remain in source archives but never ship in an installed release.

## Configuration and catalogs

- `astinuslab.toml`: default local configuration file.
- `config/catalog/`: target, attacker, workload, and image catalog metadata.
- `images/definitions/*.meta.json`: small VM image provenance/catalog metadata that may be reviewed and committed.

## Documentation

- `docs/`: MkDocs source pages. Canonical docs taxonomy: `docs/user/` for human workflows, `docs/api/` for endpoint-focused API docs, `docs/reference/` for stable config/spec facts, `docs/development/` for contributor internals, and `docs/community/` for governance/authors.
- `docs/mkdocs.yml`: documentation navigation, theme, and i18n configuration.

## Local runtime and generated files

These paths are local runtime/generated state. Do not move them as part of structure cleanup, and do not delete them while AstinusLab jobs, Playground sessions, Celery workers, or docs servers are active.

- `storage/`: SQLite databases, Celery broker/result databases, PID files, logs, dumps, SSH keys, temporary files, workload cache, and Playground state. `storage/.gitkeep` keeps the directory present; Stage G verifies that no other runtime entry is tracked and that installed services use the stable configured data root.
- `build/`: ignored root scratch for local/generated build output. Reusable Windows build helpers live in `ops/host/windows_build/`; reusable Windows guest templates live in `guest/windows/`. Prefer `storage/tmp/windows-build/<id>/` for new per-run Windows image-build output.
- `images/definitions/*.{img,qcow2,raw}`: local VM image binaries. Metadata sidecars may be tracked; large image binaries stay local unless explicitly excepted with provenance and checksum.
- `vol3/symbols/`: generated Volatility 3 symbols, build logs, and extraction output.
- Operator-installed Volatility 3 and `dwarf2json` executables are intentionally
  outside the repository and are not runtime-generated source-tree files.
- `site/`: MkDocs build output.

See [Generated Runtime Files](GENERATED_RUNTIME_FILES.md) for cleanup rules and safety checks.

## Tests

- `tests/python/`: Python pytest suites for backend APIs, orchestration, storage, forensics, Playground, and host helpers.
- `tests/python/api/`: REST API surface tests (`test_api.py`, `test_api_v1_extended.py`, `test_openapi_contract.py`).
- `tests/python/jobs/`: Job domain tests — repository, state machine, transitions, runtimes (`test_repositories.py`, `test_job_state_support.py`, `test_job_transitions.py`, `test_runtimes.py`).
- `tests/python/host/`: Host-side helper/script tests (`test_host_acl_helper.py`, `test_host_helpers.py`, `test_sudoers_helper_install.py`, `test_network_isolation_helper.py`, `test_libvirt_storage_admin_helper.py`).
- `tests/python/forensics/`: Forensics/capture pipeline tests.
- `tests/python/playground/`: Playground session/VM/network tests.
- `tests/python/integration/`: Cross-cutting integration tests.
- `tests/python/ui/`: FastAPI/Jinja server-rendered route and template tests.
- `tests/python/e2e/`: backend-owned end-to-end pytest coverage.
- `tests/python/host_real/`: opt-in real-hypervisor tests; skipped unless `ASTINUSLAB_REAL_HOST=1` with a working libvirt connection. Excluded from the default suite and default PR CI; run by the scheduled/self-hosted real-host workflow.
- `tests/python/tooling/`: pytest checks for repo scripts and tooling (`astinuslab.sh`, install scripts, docs theme, Playwright config). Excluded from the default `tests/python` run; execute with `.venv/bin/python -m pytest tests/python/tooling -q`.
- `tests/shell/host/`: host acceptance shell tests, including the install/upgrade/rollback matrix.
- `tests/browser/`: Playwright browser smoke/integration/full tests.

### Test ownership

All application tests now live under explicit domain directories. Formerly
cross-cutting root tests are assigned to `api/`, `core/`, `db/`, `forensics/`,
`host/`, `images/`, `integration/`, `jobs/`, `orchestration/`, `playground/`,
`storage/`, `ui/`, `virtualization/`, or `workloads/`; repository and release
checks live under `tooling/`. The root `tests/python/` directory is reserved
for shared `conftest.py`, package markers, and support modules.

## Local tool state

- `.claude/`, `.agents/`, `.hermes/`, `.superpowers/`, `.antigravitycli/` and
  `.worktrees/`: local agent and tooling state, ignored by Git.
- Agent instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) and tool
  ignore files such as `.cursorignore` are personal setup and stay untracked.

## Frontend templates and assets

- `src/astinuslab/web/templates/admin_*.html`: admin/workbench pages such as storage, images, logs, settings, workloads, and dumps.
- `src/astinuslab/web/templates/job_*.html`: Job create/detail pages.
- `src/astinuslab/web/templates/partials/`: HTMX fragments and reusable partials.
- `src/astinuslab/web/templates/playground.html`: Playground front door template.
- `src/astinuslab/web/static/css/`: modular stylesheets loaded by base templates — shared tokens, layout, components, and page-specific styles.
- `src/astinuslab/web/static/css/components.css`: shared `wiz-*` page-shell primitives; page styles may add only scoped page-specific overrides.
- `src/astinuslab/web/static/fonts/`: the two self-hosted brand type families (woff2), served same-origin to satisfy the `font-src 'self'` CSP. Provenance and licensing live in `THIRD_PARTY_NOTICES.md` and `LICENSES/README.md`.
- `src/astinuslab/web/static/playground/`: Playground JavaScript modules.

Preserve public routes, template names, and static URLs during structure work unless a dedicated compatibility plan says otherwise.

### Template ownership

- `src/astinuslab/web/templates/admin_*.html`: admin/workbench pages.
- `src/astinuslab/web/templates/job_*.html`: job pages.
- `src/astinuslab/web/templates/partials/`: HTMX fragments and reusable partials.

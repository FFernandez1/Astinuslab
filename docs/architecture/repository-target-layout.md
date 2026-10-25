# Repository target layout

> **STATUS: HISTORICAL (2026-08-29):** The staged repository migration is complete; current paths are authoritative and this document preserves the approved target record.

This is the approved end-state tree for the staged structure migration. It is not a claim that the current checkout already has these paths. The contributor-facing [Repository Layout](../development/REPOSITORY_LAYOUT.md) continues to describe landed paths until each stage updates it.

```text
AstinusLab/
├── .github/                         CI and contribution policy
├── docs/                            canonical MkDocs source
│   ├── api/
│   ├── architecture/
│   ├── community/
│   ├── development/
│   ├── plans/                       tracked, unbuilt design/implementation history
│   ├── reference/
│   ├── usage/
│   └── user/
├── src/
│   └── astinuslab/                  installable Python distribution
│       ├── api/
│       ├── core/
│       ├── db/
│       ├── forensics/
│       ├── images/
│       ├── jobs/
│       ├── live_session/
│       ├── migrations/              Alembic environment and revisions
│       ├── orchestration/
│       ├── playground/
│       ├── storage/
│       ├── virtualization/
│       ├── web/
│       │   ├── static/
│       │   └── templates/
│       └── workloads/
├── tests/
│   ├── python/
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── e2e/
│   │   ├── forensics/
│   │   ├── host/
│   │   ├── host_real/
│   │   ├── images/
│   │   ├── integration/
│   │   ├── jobs/
│   │   ├── live_session/
│   │   ├── orchestration/
│   │   ├── playground/
│   │   ├── storage/
│   │   ├── tooling/
│   │   ├── ui/
│   │   ├── virtualization/
│   │   └── workloads/
│   ├── browser/
│   │   ├── docs/
│   │   ├── fixtures/
│   │   ├── full/
│   │   ├── helpers/
│   │   ├── integration/
│   │   ├── smoke/
│   │   └── unit/
│   ├── fixtures/                    genuinely cross-suite only
│   └── shell/
│       └── host/
├── ops/
│   ├── deploy/                      install, upgrade, uninstall
│   ├── host/                        privileged/libvirt host helpers
│   ├── images/                      operator-run image provisioning
│   └── systemd/                     service templates
├── tools/
│   ├── dev/                         local development utilities
│   ├── quality/                     repository/code audits
│   └── release/
│       ├── evidence/                collector and validator
│       └── review/                  release readiness/review contracts
├── guest/                            VM-side source and workloads
│   ├── cloud-init/
│   ├── scripts/
│   ├── windows/
│   └── workloads/
├── config/                           operator-facing safe examples
│   └── catalog/
├── images/                           metadata/manifests + ignored operator bytes
│   ├── definitions/
│   └── manifests/
├── vol3/                             visible AstinusLab/third-party boundary
│   ├── config/
│   ├── docs/
│   ├── scripts/
│   └── symbols/                      ignored generated runtime state
├── storage/                          ignored development runtime root
├── LICENSES/
├── .gitattributes
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── LICENSE_COMPATIBILITY_REVIEW.md
├── OVERVIEW.md
├── README.md
├── RELEASE_PLAN.md
├── THIRD_PARTY_NOTICES.md
├── astinuslab.sh
├── astinuslab.toml.example
├── eslint.config.js
├── package-lock.json
├── package.json
├── playwright.config.js
├── playwright.docs.config.js
├── playwright.integration.config.js
├── pyproject.toml
├── requirements-dev.lock
├── requirements.lock
└── uv.lock
```

Tool-specific hidden configuration may remain when it is a real integration interface, but generated agent state is ignored and absent from the source baseline. Generated roots such as `build/`, `site/`, `test-results/`, `graphify-out/`, virtual environments, caches, `node_modules/`, and package metadata remain untracked and are omitted from the tree above.

## Ownership details

### Installable application

`src/astinuslab` owns all Python imported by runtime services. The wheel includes application modules, package-owned migrations, required templates/static assets, and ratified license metadata. It excludes tests, contributor tools, operator data, VM images, generated symbols, and external Volatility/dwarf2json tools.

Stage E implements this boundary without a legacy import facade. Runtime Python,
Alembic resources, and web assets are package-owned; operational resources remain
in their explicit release-tree boundaries. Installed contexts configure code and
data roots independently, so mutable databases never derive from `site-packages`.

Vertical packages preserve the current proven seams. Flat modules are assigned by responsibility during the domain stage; the target tree does not require a speculative split before imports, co-change history, and tests support it.

### Tests

All test code is discoverable under `tests/`, but suite types remain distinct. Python tests follow domains; `web` is exercised by `tests/python/ui`, migrations by `tests/python/integration`, and the other declared package boundaries have matching directories. Backend-owned end-to-end pytest coverage lives in `tests/python/e2e`. Browser projects preserve unit/smoke/integration/full/docs roles; JavaScript `*.test.mjs` files move out of package-owned static assets into the mirrored `tests/browser/unit` tree before web assets enter the wheel. Executable host matrices live under `tests/shell`; shared fixtures are exceptional and documented.

Stage F assigns every formerly deferred cross-cutting test to a domain,
`integration`, `host`, or `tooling` directory. No unowned test files may be
added directly under `tests/python/`.

The migration baseline is 2,900 backend tests, 760 tooling tests, 86.1% application coverage, 26 JavaScript unit tests, 41 documentation browser tests, and 28 UI smoke tests.

### Operations and tools

`ops` runs on or changes the application host and is part of the installed release payload. `ops/images` owns operator-run image provisioning scripts without treating them as privileged host helpers. `tools` supports contributors, quality checks, and release decisions and must never be copied into an operator installation. The Stage D move therefore adds an anchored `/tools` installer exclusion and a matching installed-tree residue assertion in the same commit. This trust/execution split replaces the current file-type-oriented `scripts` bucket.

The deployment implementations live only under `ops/deploy/`. The former root
`deploy/` compatibility wrappers are removed in Stage H; the acceptance matrix
resolves the canonical path for the candidate while still accepting a genuine
prior archive that predates the Stage D move.

### Product resources and operator state

`guest`, `config`, `images`, and `vol3` remain visible top-level product/operator boundaries because their names communicate more than a generic `resources` bucket. Their internal source/runtime rules are mechanically enforced:

- live configuration twins, VM image bytes, symbols, logs, DBs, dumps, keys, and caches are ignored;
- safe examples, guest source, image provenance metadata/manifests, and AstinusLab-owned Volatility integration files are reviewable source;
- Volatility 3 and dwarf2json executables/source stay operator-owned outside the checkout and release.

`storage` remains the development runtime default to avoid a high-risk cosmetic migration. Installed services use configured absolute data roots independent of the Python package/release directory. Stage G verifies this boundary through storage-root agreement, installer permission, release-switch, rollback, and mutable-custody matrices; only the tracked `.gitkeep` placeholder remains in the repository runtime root.

### Documentation and release records

`docs/plans` is the canonical tracked location for durable plans and must not be swallowed by the root `/plans/` ignore rule. It is intentionally excluded from MkDocs publication: these records are contributor history, not supported user documentation. Built pages must not link into this unbuilt tree. Executable release review contracts move to `tools/release/review` in Stage D; private/raw evidence remains in the named external `plans/<date>-*-evidence/` workspace until a tracked summary is committed under `docs/plans/`. Local agent execution reports are untracked after required decisions/references are preserved. The five internal repository-structure pages listed under `not_in_nav` are intentionally English-only; localized contributor pages link to those canonical records through the documentation fallback policy.

## Stage sequence

| Stage | Scope | Merge gate |
| --- | --- | --- |
| A | generated/runtime policy and ignored local agent output | prohibited tracked-file and source-archive tests |
| B | durable docs/plans and release-evidence ownership | strict docs, release dependency suite, ignored/tracked policy, link/path scan |
| C | test topology | collection/coverage and browser/shell parity |
| D | `tools`, `ops`, executable release-review contracts, and path-linked resources | shell/host/installer/release manifest tests |
| E | Python package, migrations, web assets, and license provenance | checkout/editable/wheel/release install contexts plus notice integrity |
| F | vertical domain ownership | focused/full tests, import-boundary gate, task/route/model parity |
| G | runtime/configuration boundary hardening without storage rename | install/upgrade/permissions/rollback matrices |
| H | expired wrappers/fallbacks and root cleanup | no unexplained old paths, migration docs, full gates |

Each stage branches from then-current `main`, uses its own current baseline, and is independently mergeable/revertible. Runtime data is never deleted by a source-layout rollback.

## Deliberately unchanged paths

| Path | Reason |
| --- | --- |
| `.github/` | ecosystem convention and clear CI ownership |
| `docs/` | canonical source/fallback i18n already established |
| `guest/` | distinct guest execution/trust boundary |
| `config/` | recognizable operator configuration contract |
| `images/` | recognizable image/provenance/operator-data boundary |
| `vol3/` | licensing and operator-tool boundary must stay visible |
| `storage/` | development runtime compatibility; installed data is configured absolute |
| root licenses/notices | ecosystem and legal discoverability |
| root build/test configs and locks | tool conventions and single-project ownership |
| `astinuslab.sh` | contributor-facing development entry point |

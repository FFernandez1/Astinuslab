# Repository placement policy

> **STATUS: HISTORICAL (2026-08-29):** The staged repository migration is complete; retain this policy as decision evidence for future additions.

This policy determines where AstinusLab files belong. It prevents structure work from becoming a cosmetic reshuffle and gives future additions the same ownership rules.

## Decision rubric

Score a current and proposed location from 0 to 3 in each dimension:

| Dimension | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| Ownership | none | several implicit owners | shared owner documented | one explicit owner |
| Cohesion | unrelated contents | several responsibilities | related responsibilities | one bounded purpose |
| Discoverability | surprising | tribal knowledge required | documented | conventional and obvious |
| Change coupling | changes with distant files | frequent cross-tree edits | limited coupling | colocated with change partners |
| Dependency direction | circular/implicit | exceptions dominate | mostly directional | mechanically enforceable |
| Packaging/deployment | checkout-only accident | custom copying is implicit | explicit resource rule | installed artifact owns it |
| Test mapping | no clear tests | distant/ambiguous | documented mapping | obvious corresponding tests |
| Runtime safety | source/state mixed | ignores carry safety | boundary documented | separate configured root |

The maximum score is 24. A low score creates a review obligation, not an automatic move. A target is accepted only when it improves ownership/discoverability and has complete consumers, verification, and rollback in the path migration matrix.

## Placement rules

1. Product/domain responsibility outranks file type. Do not create broad `python/`, `yaml/`, or `shell/` source buckets.
2. Execution environment is a hard boundary: application, privileged host, guest VM, developer tool, release tool, browser, and mutable operator state do not share directories casually.
3. Installable application code and required immutable runtime resources belong to the Python distribution or an explicit release resource manifest.
4. Mutable operator data never lives under importable source. Development defaults may be repository-relative only when ignored, contained, and documented.
5. Tests follow the boundary they protect. Cross-cutting tests have an explicit integration/tooling owner rather than accumulating at a root.
6. Public URLs, Celery task names, Alembic revision IDs, systemd unit names, configuration keys, catalog schemas, and artifact formats are contracts independent of source paths.
7. Generated output has one producer, one cleanup rule, and no tracked copy unless it is a reviewed release input or durable evidence summary.
8. Keep conventional root files when ecosystem tools or contributor discovery benefit. A small root is useful; an artificially empty root is not.
9. Preserve licensing visibility. Volatility/dwarf2json operator ownership and source-archive preferred-form guarantees override cosmetic uniformity.
10. Use compatibility wrappers only for supported external commands/paths, give each a removal stage, and never let wrappers become permanent duplicate implementations.

## Ratified placement decisions

Scores are evidence-based comparisons from the baseline inventory. They are directional, not mathematical proof.

| Boundary | Current | Candidate | Risk | Decision |
| --- | ---: | ---: | --- | --- |
| `backend/app` → `src/astinuslab` | 15 | 22 | high: 2,293 import-string matches | Accept; intentional package root and narrower wheel justify staged migration |
| external `frontend` → package `web` | 15 | 22 | high: release/Jinja/npm/Playwright/license coupling | Accept only after JS tests leave static assets, with wheel/release asset and notice tests plus stable public URLs |
| `backend/alembic` → package migrations | 17 | 22 | high: DB upgrade discovery | Accept; wheel-only migration resources and unchanged revision IDs are mandatory |
| split Python/browser/shell test roots → canonical `tests` | 12 | 22 | medium: discovery/import fixtures | Accept; move by suite/domain with count and coverage parity |
| mixed `scripts`/`deploy` → `tools`/`ops` | 11 | 22 | high: installer and privilege paths | Accept; split by execution environment and keep temporary deploy wrappers |
| `guest` → generic `resources/guest` | 21 | 18 | medium: catalog/release references for little gain | Reject; `guest` is already a clear first-class execution boundary |
| `config/catalog` → generic `resources/catalog` | 21 | 18 | medium: operator contract for little gain | Reject; keep recognizable operator configuration root |
| `images` → generic resources/runtime split | 18 | 18 | high: 382 references and operator bytes | Reject path move; strengthen tracked metadata vs ignored binary policy in place |
| `vol3` → generic tools/resources/runtime split | 19 | 17 | high: licensing and operator-tool boundary | Reject root move; preserve visible boundary and classify children |
| `storage` → repository `var` | 20 | 15 | severe: 421 references and mutable data migration | Reject; retain development path and require explicit absolute installed data root |
| generic `plans` + agent reports → docs/release tools/local state | 9 | 22 | medium: release-check dependency | Accept after reference inventory; durable docs, executable review tools, and local output get separate owners |
| tracked `.hermes`/`.superpowers` output → ignored local state | 7 | 23 | low after durable references repaired | Accept; untrack without deleting local contents |
| conventional root build/license/tool configs → subdirectories | 22 | 16 | medium tooling friction, little gain | Reject; keep conventional root files |

## File-addition checklist

Before adding a new file, answer:

- Who owns it and where does it execute?
- Is it immutable source/resource, mutable operator state, generated output, a test, or durable documentation?
- What imports/reads/writes it?
- Does it ship in the wheel, source archive, release install, or none?
- What test proves its placement and lifecycle?
- Does it introduce a new top-level entry or exception to dependency direction?

If those answers do not identify one target directory, clarify the responsibility before committing the file.

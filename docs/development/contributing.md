# Contributing

Use this page for the development workflow. Project community documents remain under the Community section.

## Before Opening a Pull Request

1. Start from an isolated branch/worktree; preserve unrelated active work.
2. Keep changes scoped.
3. Add or update tests when behavior changes.
4. Update documentation when commands, APIs, configuration, or workflows change.
5. Run the checks in the [change-to-check matrix](verification-matrix.md)
   that match the change, recording exact commands, results and unavailable tiers.

## Local Validation

For backend changes:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

For docs changes:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

For host shell helpers:

```bash
shellcheck ops/host/*.sh
```

## Coding Standards

- Python code should follow the existing FastAPI, Pydantic, SQLAlchemy, and repository patterns.
- Shell scripts should use defensive Bash patterns and `set -euo pipefail`.
- Frontend changes should follow the existing Jinja2, HTMX, and vanilla JavaScript structure.
- Do not add broad dependencies unless they are needed by the repository design.

### Error field naming

Three contexts, three names — see [`docs/architecture/adr-error-field-naming.md`](../architecture/adr-error-field-naming.md). Enforced by `tools/quality/audit_error_fields.py` (run as `tests/python/tooling/test_error_field_audit.py`).

## Documentation Standards

- Document commands that exist in the repository.
- Keep usage docs, architecture docs, API docs, and development docs separate.
- Mark unknowns explicitly instead of inventing behavior.
- Preserve existing MkDocs theme features and navigation style.

## Translating Documentation

Docs are localized with the `mkdocs-static-i18n` plugin. English (`en`) is the
single source of truth; supported locales are `en`, `es`, `fr`, and `ca`
(matching the web UI).

- **English is authoritative.** Always edit `page.md` first. Public pages should
  ship localized siblings for every supported locale so language builds do not
  fall back to English content.
- **Add a translation** by creating a sibling file with a locale suffix next to
  the English page: `index.es.md`, `index.fr.md`, `index.ca.md`,
  `usage/getting-started.es.md`, and so on. Keep the same filename, links, and
  heading structure as the English source; product terms (Playground, Jobs,
  Target, Workload, Attacker, Acquisition, Volatility 3) stay untranslated.
- **Section titles** in the left nav are translated via `nav_translations`
  under each locale in `docs/mkdocs.yml`. Add an entry there only when you introduce
  a new nav group or page label.
- **Do not bulk-translate.** Translate a page only when it can be kept in sync
  with its English source; keep internal planning archives outside published
  builds via `exclude_docs` when they are not maintained as public docs.

Build to confirm all locales render:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

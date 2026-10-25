# Contribuir

Fes servir aquesta pàgina per al workflow de desenvolupament. Els documents comunitaris del projecte romanen sota la secció Community.

Consulta la [matriu de verificació](verification-matrix.md), mantinguda en anglès amb fallback per evitar duplicar les instruccions. Aïlla les tasques simultànies i conserva els seus canvis.

## Abans d'obrir una Pull Request

1. Comença des d'una branca neta.
2. Mantén els canvis acotats.
3. Afegeix o actualitza tests quan canviï el comportament.
4. Actualitza la documentació quan canviïn ordres, APIs, configuració o workflows.
5. Executa les ordres de validació local que corresponguin al canvi.

## Validació local

Per a canvis de backend:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Per a canvis de docs:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

Per a helpers shell d'host:

```bash
shellcheck ops/host/*.sh
```

## Estàndards de codi

- El codi Python ha de seguir els patrons existents de FastAPI, Pydantic, SQLAlchemy i repositoris.
- Els scripts shell han de fer servir patrons Bash defensius i `set -euo pipefail`.
- Els canvis de frontend han de seguir l'estructura existent de Jinja2, HTMX i JavaScript vanilla.
- No afegeixis dependències àmplies llevat que les necessiti el disseny del repositori.

### Noms dels camps d'error

Tres contextos, tres noms — vegeu [`docs/architecture/adr-error-field-naming.md`](../architecture/adr-error-field-naming.md). Ho aplica `tools/quality/audit_error_fields.py` (s'executa com `tests/python/tooling/test_error_field_audit.py`).

## Estàndards de documentació

- Documenta ordres que existeixin al repositori.
- Mantén separades les docs d'ús, arquitectura, API i desenvolupament.
- Marca els desconeguts explícitament en lloc d'inventar comportament.
- Preserva les funcions del tema MkDocs i l'estil de navegació existents.

## Traduir la documentació

Les docs es localitzen amb el plugin `mkdocs-static-i18n`. English (`en`) és l'única font de veritat; els locales suportats són `en`, `es`, `fr` i `ca` (com la UI web).

- **English és autoritatiu.** Edita sempre `page.md` primer. Les pàgines públiques han d'incloure siblings localitzats per a cada locale suportat perquè els builds per idioma no caiguin a contingut en English.
- **Afegeix una traducció** creant un fitxer sibling amb sufix de locale al costat de la pàgina English: `index.es.md`, `index.fr.md`, `index.ca.md`, `usage/getting-started.es.md`, etc. Mantén el mateix filename, enllaços i estructura de headings que la font English; els termes de producte (Playground, Jobs, Target, Workload, Attacker, Acquisition, Volatility 3) romanen sense traduir.
- **Els títols de secció** a la navegació esquerra es tradueixen via `nav_translations` sota cada locale a `docs/mkdocs.yml`. Afegeix una entrada només quan introdueixis un nou grup de nav o etiqueta de pàgina.
- **No tradueixis en massa.** Tradueix una pàgina només quan pugui mantenir-se sincronitzada amb la seva font English; mantén els arxius interns de planificació fora dels builds publicats mitjançant `exclude_docs` quan no es mantinguin com a docs públiques.

Construeix per confirmar que tots els locales renderitzen:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

# Estructura del repositori

AstinusLab manté el codi font, els catàlegs, la documentació, els tests i l'estat de runtime local en ubicacions estables. Aquest mapa és només una guia per a contribuïdors; no reubica rutes de runtime.

> L'organització objectiu per etapes està aprovada, però aquesta pàgina continua descrivint els camins que ja s'han integrat. Consulteu l'[estructura objectiu aprovada del repositori](../architecture/repository-target-layout.md) i la seva matriu de migració; no utilitzeu camins futurs fins que se n'integri l'etapa.

## Codi font

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

- `src/astinuslab/`: app FastAPI instal·lable, tasques Celery, models SQLAlchemy, helpers d'orquestració, migracions Alembic pròpies del paquet i recursos web sota `web/`.
- `tests/`: suites Python d'aplicació i tooling, contractes shell i tests de navegador.
- `guest/`: scripts, templates i helpers del guest usats dins de VMs de lab. Els exemples Autounattend de preparació d'imatges Windows viuen a `guest/windows/`.
- `ops/host/`: helpers instal·lats de host per a libvirt, emmagatzematge, ACL i build portable de Windows.
- `ops/images/`: helpers per a operadors de descàrrega d'imatges i metadades de procedència.
- `ops/deploy/`: implementacions canòniques d'instal·lació, configuració de roots privilegiats i desinstal·lació.
- `ops/systemd/`: templates font per als serveis instal·lats d'API, runner i anàlisi.
- `tools/dev/`: helpers de desenvolupament local només per a contribuïdors; mai s'instal·len en una release.
- `tools/quality/`: checks de política de qualitat del repositori invocats directament per CI.
- `tools/release/`: eines versionades d'evidència, validació i revisió de release; formen part de l'arxiu font però no de la instal·lació.

## Configuració i catàlegs

- `astinuslab.toml`: fitxer de configuració local per defecte.
- `config/catalog/`: metadades de catàlegs de targets, atacants, workloads i imatges.
- `images/definitions/*.meta.json`: metadades petites de procedència/catàleg d'imatges VM que es poden revisar i commitejar.

## Documentació

- `docs/`: pàgines font de MkDocs. Taxonomia canònica: `docs/user/` per a workflows humans, `docs/api/` per a docs d'API enfocades en endpoints, `docs/reference/` per a fets estables de configuració/especificació, `docs/development/` per a interns de contribuidor i `docs/community/` per a governança/autors.
- `docs/mkdocs.yml`: navegació, tema i configuració i18n de documentació.

## Runtime local i fitxers generats

Aquestes rutes són estat local de runtime/generat. No les moguis com a part de neteja estructural i no les eliminis mentre hi hagi jobs d'AstinusLab, sessions Playground, workers Celery o servidors de docs actius.

- `storage/`: bases SQLite, bases broker/result de Celery, fitxers PID, logs, dumps, claus SSH, temporals, caché de workloads i estat Playground. `storage/.gitkeep` manté present el directori.
- `build/`: scratch arrel ignorat per a output local/generat de build. Els helpers Windows reutilitzables viuen a `ops/host/windows_build/`; els templates guest Windows reutilitzables viuen a `guest/windows/`. Prefereix `storage/tmp/windows-build/<id>/` per a nou output per execució de build d'imatges Windows.
- `images/definitions/*.{img,qcow2,raw}`: binaris locals d'imatges VM. Els sidecars de metadades es poden rastrejar; els binaris grans romanen locals llevat d'excepció explícita amb procedència i checksum.
- `vol3/symbols/`: símbols Volatility 3 generats, logs de build i sortida d'extracció.
- Els executables Volatility 3 i `dwarf2json` instal·lats per l'operador resten
  fora del repositori i no són fitxers generats de l'arbre font.
- `site/`: sortida de build de MkDocs.

Consulta [Fitxers generats i de runtime](GENERATED_RUNTIME_FILES.md) per a regles de neteja i checks de seguretat.

## Tests

- `tests/python/`: suites pytest de Python per a APIs backend, orquestració, storage, forensics, Playground i helpers de host.
- `tests/python/api/`: tests de superfície REST (`test_api.py`, `test_api_v1_extended.py`, `test_openapi_contract.py`).
- `tests/python/jobs/`: tests del domini Job — repositori, màquina d'estats, transicions i runtimes (`test_repositories.py`, `test_job_state_support.py`, `test_job_transitions.py`, `test_runtimes.py`).
- `tests/python/host/`: tests de helpers/scripts host (`test_host_acl_helper.py`, `test_host_helpers.py`, `test_sudoers_helper_install.py`, `test_network_isolation_helper.py`, `test_libvirt_storage_admin_helper.py`).
- `tests/python/forensics/`: tests de pipeline forense/captura.
- `tests/python/playground/`: tests de sessió/VM/xarxa Playground.
- `tests/python/integration/`: tests d'integració transversals.
- `tests/python/ui/`: tests de rutes FastAPI/Jinja i templates renderitzats del servidor.
- `tests/python/e2e/`: cobertura end-to-end pròpia del backend amb pytest.
- `tests/python/tooling/`: tests pytest per a scripts i tooling del repositori; exclosos del run per defecte de `tests/python`, però executables amb `.venv/bin/python -m pytest tests/python/tooling -q`.
- `tests/shell/host/`: proves shell d'acceptació del host, inclosa la matriu d'instal·lació/actualització/rollback.
- `tests/browser/`: smoke/integration/full tests de navegador amb Playwright.

### Organització dels tests

Tots els tests de l’aplicació pertanyen a carpetes de domini sota `tests/python/`.
L’arrel conté només `conftest.py`, marcadors de paquet i mòduls de suport.
Els tests del repositori viuen a `tooling/` i se seleccionen explícitament.

## Estat local d'eines

- `.claude/`, `.agents/`, `.hermes/`, `.superpowers/`, `.antigravitycli/` i `.worktrees/`: estat local d'agents i eines, ignorat per Git.
- Els fitxers d'instruccions d'agents (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) i els fitxers d'exclusió d'eines com `.cursorignore` són configuració personal i no es versionen.

## Templates i assets frontend

- `src/astinuslab/web/templates/admin_*.html`: pàgines admin/workbench com storage, images, logs, settings, workloads i dumps.
- `src/astinuslab/web/templates/job_*.html`: pàgines de Job.
- `src/astinuslab/web/templates/partials/`: fragments HTMX i parcials reutilitzables.
- `src/astinuslab/web/templates/playground.html`: template front-door de Playground.
- `src/astinuslab/web/static/css/layout.css`: stylesheet global de compatibilitat carregada pels templates base.
- `src/astinuslab/web/static/css/`: tokens, layout, components i estils per pàgina compartits.
- `src/astinuslab/web/static/fonts/`: les dues famílies tipogràfiques de marca autoallotjades (woff2), servides des del mateix origen per complir la CSP `font-src 'self'`.
- `src/astinuslab/web/static/playground/`: mòduls JavaScript de Playground.

Preserva rutes públiques, noms de templates i URLs estàtiques durant treball estructural llevat que un pla de compatibilitat dedicat digui el contrari.

### Propietat de templates

- `src/astinuslab/web/templates/admin_*.html`: pàgines admin/workbench.
- `src/astinuslab/web/templates/job_*.html`: pàgines job.
- `src/astinuslab/web/templates/partials/`: fragments HTMX i parcials reutilitzables.
